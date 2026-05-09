# 基础镜像
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
WORKDIR /app
COPY . .
ARG PIP_INDEX_URL=https://pypi.org/simple
# docker-cli + compose plugin: /deploy-hook 自部署需要 (容器内执行 docker
# compose pull/up, 通过挂载的 /var/run/docker.sock 走 host daemon).
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
RUN python -m pip install --upgrade pip && \
    python -m pip install --no-cache-dir -i "$PIP_INDEX_URL" fastapi hypercorn redis toml aiofiles 'httpx[socks]' python-dotenv motor asyncpg
EXPOSE 7861
CMD ["python", "web.py"]
