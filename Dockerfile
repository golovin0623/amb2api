# 基础镜像
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
WORKDIR /app
ARG PIP_INDEX_URL=https://pypi.org/simple

# 单一事实源: 依赖从 pyproject.toml [project.dependencies] 读取 (含版本约束),
# 而不是在此手写清单 —— 避免 Dockerfile 与 pyproject 漂移导致"本地测试过、
# 镜像里 ImportError". 用标准库 tomllib (Python 3.11+), 无需额外装包.
#
# 先只 COPY pyproject.toml, 让"装依赖"这层在仅改业务代码时仍命中构建缓存;
# 业务代码改动只会让后面的 `COPY . .` 那层失效, 不必重装依赖.
COPY pyproject.toml ./
RUN python -m pip install --upgrade pip && \
    python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt && \
    python -m pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /tmp/requirements.txt

COPY . .
EXPOSE 7861
CMD ["python", "web.py"]
