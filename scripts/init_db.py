#!/usr/bin/env python3
"""初始化 MySQL 表结构（幂等）。

用法::

    export AGENT_DB_URL='mysql+pymysql://agent:密码@127.0.0.1:3306/agent_code?charset=utf8mb4'
    python scripts/init_db.py

也可以只配分项变量（MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE），
脚本会用 ``config.mysql_url()`` 拼出连接串。

重复执行是安全的：``create_all`` 只建不存在的表。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def main() -> int:
    import db
    from persistence import resolve_db_url

    url = resolve_db_url()
    if not url.startswith("mysql"):
        print(f"⚠️ 当前连接串不是 MySQL：{url}")
        print("   请设置 AGENT_DB_URL，或设置 MYSQL_* 分项变量。")
        return 2

    print(f"连接：{url.split('@')[-1]}")
    db.init_db()

    from persistence import get_engine
    from sqlalchemy import inspect

    tables = sorted(inspect(get_engine()).get_table_names())
    print(f"✅ 表已就绪（{len(tables)} 张）：{', '.join(tables)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
