# 세션별 샌드박스 이미지(설계 3.3절). 빌드: bash infra/sandbox-image/build.sh
FROM python:3.12-slim-bookworm

ARG NODE_MAJOR=22

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl wget ca-certificates gnupg jq ripgrep unzip zip tar gzip less procps \
        build-essential libffi-dev libssl-dev sqlite3 \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /usr/local/bin/

# 자주 쓰는 패키지는 홈 볼륨이 아니라 이미지 계층에 둬서 스냅샷을 작게 유지한다.
RUN pip install --no-cache-dir \
        requests httpx pandas numpy openpyxl python-docx python-pptx pypdf \
        matplotlib beautifulsoup4 lxml pyyaml

# sandboxd는 root 소유의 별도 venv에 둔다. 에이전트의 pip install이 sandboxd 의존성을 깨지 못하게.
COPY apps/sandboxd /tmp/sandboxd
RUN cd /tmp/sandboxd \
    && UV_PROJECT_ENVIRONMENT=/opt/sandboxd/venv UV_PYTHON_DOWNLOADS=never \
        uv sync --frozen --no-dev --no-editable --no-cache \
    && rm -rf /tmp/sandboxd

RUN groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/agent --shell /bin/bash agent
# 세션 홈 볼륨이 처음 붙을 때 이 내용이 볼륨으로 복사된다.
COPY --chown=1000:1000 infra/sandbox-image/home-skeleton/ /home/agent/
RUN find /home/agent -name .gitkeep -delete

ENV HOME=/home/agent \
    PIP_USER=1 \
    PYTHONUSERBASE=/home/agent/.local \
    NPM_CONFIG_PREFIX=/home/agent/.npm-global \
    PATH=/home/agent/.local/bin:/home/agent/.npm-global/bin:$PATH

USER 1000:1000
WORKDIR /home/agent
EXPOSE 7000
# tini는 sandbox-manager가 컨테이너를 만들 때 init=True로 붙인다.
ENTRYPOINT ["/opt/sandboxd/venv/bin/python", "-m", "sandboxd"]
