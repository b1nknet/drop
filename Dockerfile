FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml uv.lock .python-version LICENSE ./
RUN uv sync --locked --no-dev --no-cache \
    && mkdir /data \
    && chown 1000:1000 /data
COPY app ./app

USER 1000:1000
EXPOSE 8000
# 세션/로그인 제한은 프로세스 메모리에 보관하므로 단일 worker로 실행합니다.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--no-access-log"]
