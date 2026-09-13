# station-ai 的容器镜像 —— 服务器部署用（docker compose up -d --build）。
#
# 新手视角（Java 朋友版）：Dockerfile ≈ 一份"打包脚本"，把 app + 运行环境一起封成镜像。
# 基础镜像特意选 python:3.11-slim 而不是更全的版本：本项目所有依赖
# （langchain/langgraph、pillow、img2pdf、pypdf、reportlab、openpyxl）都有现成的
# Linux wheel，**不需要装任何系统包、不需要编译器**，所以镜像可以很小。
#
# ★ 本机开发**不用**这个文件 —— 本机直接 `station-web` 跑 127.0.0.1:8001 就行。
FROM python:3.11-slim

# 时区设成北京时间：容器默认 UTC，日志和产物文件名里的日期会差 8 小时（跨天时更难查）。
# PYTHONUNBUFFERED=1 → 日志实时刷出来（否则 docker logs 要等缓冲满才看得到）。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ★ README.md 必须一起拷：pyproject.toml 里 `readme = "README.md"`，
#   setuptools 打包时要读它，缺了会直接构建失败。
COPY pyproject.toml README.md ./
COPY src ./src
COPY skills ./skills

# 可选：pip 换源。大陆的机器从 pypi.org 拉几百个包会慢到离谱，用
# `--build-arg PIP_INDEX=<镜像地址>` 换掉（docker-compose.yml 里已经填了腾讯云的）。
# 不传就用默认 PyPI —— 本机 / 海外构建不受影响。
ARG PIP_INDEX=""

# -e . ：按 pyproject 声明的依赖装本项目。
# ★ src/ 必须留在镜像里（不能只留装好的包）：DATA_DIR 是"仓库根/data/station"，
#   而仓库根是 config.py 用 __file__ 往上找两层算出来的 —— 源码不在 /app/src，
#   数据目录就会跑到别处去。
RUN if [ -n "$PIP_INDEX" ]; then pip config set global.index-url "$PIP_INDEX"; fi \
 && pip install --no-cache-dir -e .

# data/ 是**唯一的持久状态**（SQLite、用户上传的照片、产物文件）。
# 这里只是声明，真正挂载见 docker-compose.yml —— ★ 忘了挂 = 容器一删档案全没。
VOLUME ["/app/data"]

EXPOSE 8443

# 入口就是 console script（= pyproject 的 [project.scripts] station-web → server:main）。
# 地址/端口/TLS 全走环境变量，见 docker-compose.yml。
CMD ["station-web"]
