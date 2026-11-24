#!/usr/bin/env python3
"""把旧 SQLite（data/chat.db）里的历史数据迁到 MySQL。

用法::

    export AGENT_DB_URL='mysql+pymysql://agent:密码@127.0.0.1:3306/agent_code?charset=utf8mb4'
    python scripts/migrate_sqlite_to_mysql.py [--sqlite data/chat.db] [--dry-run]

行为：
- **只读**旧库（以只读方式打开，不修改、不删除）
- 逐表复制：workspaces → sessions → messages → documents → memories → run_changes
- 保留原始 id（外键关系才不会乱），复制完把自增计数器推到 max(id)
- 幂等：目标表里已存在的同 id 行会跳过，重复执行不会产生重复数据

迁完确认无误后，旧文件可以自行删除；应用不会再写它。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# 表名 → 列顺序（与旧 SQLite schema 一致）
TABLES: dict[str, list[str]] = {
    "workspaces": ["id", "name", "path", "created_at", "last_used_at", "open_files"],
    "sessions": [
        "id", "title", "created_at", "updated_at", "workspace_id", "permission_level",
    ],
    "messages": ["id", "session_id", "role", "content", "created_at"],
    "documents": ["id", "filename", "chunk_count", "created_at"],
    "memories": ["id", "content", "created_at"],
    "run_changes": [
        "id", "session_id", "run_index", "path", "change_type", "tool", "diff",
        "added", "removed", "truncated", "created_at",
    ],
}

# 复制顺序（先父表后子表）
ORDER = ["workspaces", "sessions", "messages", "documents", "memories", "run_changes"]


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", default=os.path.join(ROOT, "data", "chat.db"))
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    args = parser.parse_args()

    if not os.path.exists(args.sqlite):
        print(f"找不到旧库：{args.sqlite}")
        return 2

    from sqlalchemy import text

    from persistence import get_engine, get_session

    engine = get_engine()
    if not str(engine.url).startswith("mysql"):
        print("目标库不是 MySQL，拒绝执行（避免自己迁到自己）")
        return 2

    src = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    print(f"源：{args.sqlite}")
    print(f"目标：{str(engine.url).split('@')[-1]}\n")

    total = 0
    for table in ORDER:
        cols = _sqlite_columns(src, table)
        if not cols:
            print(f"- {table}: 旧库没有这张表，跳过")
            continue

        use_cols = [c for c in TABLES[table] if c in cols]
        rows = src.execute(f"SELECT {', '.join(use_cols)} FROM {table} ORDER BY id").fetchall()
        print(f"- {table}: {len(rows)} 行")

        if args.dry_run or not rows:
            continue

        placeholders = ", ".join(f":{c}" for c in use_cols)
        col_sql = ", ".join(use_cols)
        # INSERT IGNORE：同 id 已存在就跳过，保证可重复执行
        stmt = text(f"INSERT IGNORE INTO {table} ({col_sql}) VALUES ({placeholders})")

        with get_session() as dst:
            for row in rows:
                dst.execute(stmt, {c: row[c] for c in use_cols})
            # 自增计数器推到 max(id)，避免后续新记录撞主键
            dst.execute(text(f"ALTER TABLE {table} AUTO_INCREMENT = 1"))
        total += len(rows)

    # 清掉旧库里的 runtime_state（断点状态有时效性，跨库迁移没有意义）
    print("- runtime_state: 跳过（断点状态有 24h TTL，迁移无意义）")

    src.close()
    if args.dry_run:
        print("\n[dry-run] 未写入任何数据")
    else:
        print(f"\n✅ 迁移完成，共写入 {total} 行（已存在的行被跳过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
