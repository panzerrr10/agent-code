# 给 MCP 插件用的沙箱镜像：在 agent-sandbox 基础上加 Node.js。
#
# 为什么需要单独一个：默认的 agent-sandbox 只有 python + git，
# 而多数 MCP server 是 npm 包（npx 启动），在容器里会因为找不到 node 而失败。
#
# 构建：
#   docker build -f scripts/mcp-sandbox.Dockerfile -t agent-mcp-sandbox .
#
# 然后在插件的「沙箱镜像」里填 agent-mcp-sandbox。
FROM agent-sandbox

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
 && rm -rf /var/lib/apt/lists/*
USER 1000
