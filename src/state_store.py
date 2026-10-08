"""按会话分桶、写穿 SQLite 的运行时状态存储。

**解决什么问题**

断点续跑的挂起状态（pending_state）、任务计划（plan）、项目根目录原先都只放在
模块级字典里：服务一重启就全没了——用户点「确认」会提示找不到挂起状态，
Planner 生成的计划也凭空消失。这里让它们同时落到 SQLite。

**行为**

- **读**：先查内存；未命中则从库里懒加载（可选 TTL，过期视为不存在并顺手删掉）。
- **写**：同时更新内存与库；写库失败只记日志并降级为纯内存，**不阻塞主流程**。
- **删**：两边都删，避免「删了内存又被库里的旧值复活」。

**为什么不是简单的「每次读库」**：路径校验（``is_path_permitted``）这类热路径
会频繁读 project_root，每次都打库没必要；懒加载缓存一次即可。
"""

from __future__ import annotations

import json
import logging
import threading
import time

logger = logging.getLogger(__name__)


class SessionStateStore:
    """一类会话级状态的存储（内部按 session_key 分桶）。"""

    def __init__(self, kind: str, ttl_seconds: float | None = None):
        self.kind = kind
        self.ttl = ttl_seconds
        self._mem: dict[str, object] = {}
        self._loaded: set[str] = set()  # 已确认「库里也没有」的 key，避免反复查库
        self._lock = threading.Lock()

    # ---------- 内部 ----------

    @staticmethod
    def _db():
        import db

        return db

    def _load_from_db(self, key: str):
        try:
            row = self._db().get_runtime_state(key, self.kind)
        except Exception as e:  # 表还没建 / 库不可用 → 降级为纯内存
            logger.debug("读取持久化状态失败（kind=%s）：%s", self.kind, e)
            return None

        if not row:
            return None

        payload, updated_at = row
        if self.ttl is not None and (time.time() - float(updated_at)) > self.ttl:
            # 过期：当作不存在，并清理掉，避免重启后又翻出一周前的旧确认
            try:
                self._db().delete_runtime_state(key, self.kind)
            except Exception:
                pass
            return None

        try:
            return json.loads(payload)
        except Exception:
            logger.warning("持久化状态不是合法 JSON（kind=%s, key=%s）", self.kind, key)
            return None

    # ---------- 对外 ----------

    def get(self, key: str, default=None):
        with self._lock:
            if key in self._mem:
                return self._mem[key]
            if key in self._loaded:
                return default

            value = self._load_from_db(key)
            if value is None:
                self._loaded.add(key)
                return default
            self._mem[key] = value
            return value

    def set(self, key: str, value) -> None:
        with self._lock:
            self._mem[key] = value
            self._loaded.add(key)
        try:
            self._db().upsert_runtime_state(
                key, self.kind, json.dumps(value, ensure_ascii=False), time.time()
            )
        except Exception as e:
            logger.warning("写入持久化状态失败（kind=%s）：%s", self.kind, e)

    def pop(self, key: str) -> bool:
        with self._lock:
            existed = key in self._mem
            self._mem.pop(key, None)
            self._loaded.add(key)
        try:
            self._db().delete_runtime_state(key, self.kind)
        except Exception as e:
            logger.warning("删除持久化状态失败（kind=%s）：%s", self.kind, e)
        return existed

    def clear_memory(self) -> None:
        """只清内存缓存，保留持久化数据。

        用于模拟「进程重启」：下次读取会重新从库里加载，
        这正是断点续跑要跨重启生效的关键路径。
        """
        with self._lock:
            self._mem.clear()
            self._loaded.clear()

    def purge_all(self) -> None:
        """内存与持久化全部清空（测试 / 维护用）。"""
        with self._lock:
            self._mem.clear()
            self._loaded.clear()
        try:
            self._db().delete_runtime_state_kind(self.kind)
        except Exception:
            pass
