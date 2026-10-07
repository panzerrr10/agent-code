FROM python:3.12-slim

# 换 Debian apt 源为中科大镜像（更稳定，且支持 HTTP）
RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources

# 安装常用工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    vim \
    && rm -rf /var/lib/apt/lists/*

# 装 Python 库（用国内 pip 源）
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
    requests \
    numpy \
    pandas \
    flask \
    pytest

WORKDIR /workspace

CMD ["sleep", "infinity"]