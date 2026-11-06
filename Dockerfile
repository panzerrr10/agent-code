FROM python:3.12-slim

# 换 Debian apt 源为中科大镜像（更稳定，且支持 HTTP）
RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources

# 安装常用工具 + **编译工具链**
#
# 为什么必须有 build-essential / JDK：沙箱是「执行类」工具唯一能落地的地方，
# 没有编译器的话，多语言支持就只剩「读」——
# Agent 能看懂 Java/C 代码、能改，但**编译不了、跑不了**，等于半残。
# 实测过的真实场景：让它构建一个 C 项目，它先 `make` → not found，
# 再 `gcc` → not found，最后只能退回静态检查，诚实地说「无法给出构建实证」。
#
#   构建 C / C++  →  build-essential（gcc / g++ / make / libc-dev）
#   构建 Java     →  default-jdk-headless（javac / java）
#   Maven 项目    →  maven（能解析依赖；不需要可以删掉）
#
# 体积代价：镜像从 ~620MB 涨到 ~1.5GB。不需要 Java 的话把 default-jdk-headless
# 和 maven 两行删掉即可。
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    vim \
    build-essential \
    default-jdk-headless \
    maven \
    && rm -rf /var/lib/apt/lists/*

# Maven 换阿里云镜像。
#
# 不加这一步的话，`mvn` 在国内从 Maven Central 拉插件**慢到不可用** ——
# 实测第一次 `mvn compile` 跑了 400 秒还没下完，必然撞上沙箱超时，
# 模型只能退回 `javac` 单文件编译，等于 Maven 白装。
#
# 写在**全局** settings（$MAVEN_HOME/conf）而不是用户目录，这样：
#   - 容器里 HOME=/tmp，用户级 settings 会被宿主挂进来的 ~/.m2 覆盖；
#   - 全局配置对所有用户生效，且用户自己的 ~/.m2/settings.xml 仍然可以覆盖它。
RUN printf '%s\n' \
    '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">' \
    '  <mirrors>' \
    '    <mirror>' \
    '      <id>aliyun</id>' \
    '      <name>Aliyun Public</name>' \
    '      <url>https://maven.aliyun.com/repository/public</url>' \
    '      <mirrorOf>central</mirrorOf>' \
    '    </mirror>' \
    '  </mirrors>' \
    '</settings>' \
    > /usr/share/maven/conf/settings.xml

# 装 Python 库（用国内 pip 源）
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
    requests \
    numpy \
    pandas \
    flask \
    pytest

# 容器里以非 root 运行（见 sandbox.py 的 --user），HOME 被指到 /tmp。
# Maven 的 ~/.m2 与 pip 的 ~/.cache 都由 sandbox.py 从宿主挂进来（持久缓存），
# 所以这里不需要额外准备。
WORKDIR /workspace

CMD ["sleep", "infinity"]
