# api, runner, sandbox-manager가 같이 쓰는 이미지. 어떤 프로세스를 띄울지는 compose의 command로 정한다.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /srv/api

# 의존성 계층을 먼저 만들어 소스만 바뀔 때는 재설치하지 않는다.
COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY apps/api/app ./app

# storage 볼륨이 처음 붙을 때 이 디렉터리의 소유자가 복사되므로 미리 만들어 둔다.
RUN useradd --system --uid 10001 --home-dir /srv/api --no-create-home app \
    && mkdir -p /data \
    && chown app:app /data
USER app

EXPOSE 5001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5001"]
