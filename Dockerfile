# 中转站倍率监测 - 容器镜像
# 多阶段：先用基础镜像装依赖，再复制代码，保持镜像精简
FROM python:3.11-slim

# 设置时区（调度器用 Asia/Shanghai）
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=5000

WORKDIR /app

# 系统依赖（tzdata 用于时区；curl 用于 healthcheck）
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 Docker 层缓存：依赖不变则不重装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再复制业务代码
COPY app.py config_helper.py db.py monitor.py notifiers.py qapi_client.py ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY config.example.yaml ./

# 数据持久化目录（config.yaml 与 data.db 通过 volume 挂载，避免随容器销毁）
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# 暴露端口（HOST/PORT 由环境变量控制，默认 0.0.0.0:5000）
EXPOSE 5000

# 健康检查：调用免鉴权的 /api/health
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/api/health || exit 1

# 启动
CMD ["python", "app.py"]
