-- agent-code 的 MySQL 初始化脚本（用 root 执行一次）
--
--   mysql -uroot -p < scripts/init_mysql.sql
--
-- 表结构由应用启动时自动创建（SQLAlchemy create_all，幂等），
-- 这个脚本只负责「建库 + 建账号 + 授权」。

CREATE DATABASE IF NOT EXISTS agent_code
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

-- 应用专用账号：只给 agent_code 库的增删改查权限，不给全局权限。
-- 密码请在执行前改掉，并同步写进 .env 的 MYSQL_PASSWORD。
CREATE USER IF NOT EXISTS 'agent'@'%' IDENTIFIED BY 'change-me-please';

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, ALTER
  ON agent_code.* TO 'agent'@'%';

FLUSH PRIVILEGES;

-- 验证：
--   mysql -uagent -p -e "USE agent_code; SHOW TABLES;"
