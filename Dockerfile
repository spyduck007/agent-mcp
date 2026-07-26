FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV COMMAND_HOME=/root
ENV TOOL_ROOT=/opt/agent-tools
ENV PATH=/opt/agent-tools/bin:/opt/agent-tools/venv/bin:/root/.local/bin:/root/.cargo/bin:/root/go/bin:/opt/mcp-venv/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git git-lfs gh patch curl wget ca-certificates sudo \
        build-essential pkg-config cmake ninja-build autoconf automake libtool \
        procps tree unzip zip p7zip-full jq ripgrep fd-find file less vim-tiny nano tmux \
        iputils-ping dnsutils iproute2 traceroute socat netcat-openbsd tcpdump tshark \
        openssh-client telnet rsync fuse3 libfuse2 libcap2-bin strace ltrace gdb \
        docker-cli docker-buildx docker-compose \
        golang-go rustc cargo openjdk-21-jdk-headless \
        sqlite3 postgresql-client redis-tools \
        nodejs npm ruby-full php-cli composer \
        qemu-user qemu-user-static qemu-system-x86 qemu-utils \
        python3-venv pipx \
    && git lfs install --system \
    && git config --system --add safe.directory '*' \
    && mkdir -p /etc/ssh /opt/agent-tools/bin /opt/agent-tools/venv /root/.config /root/.cache /root/.local/bin \
    && ssh-keyscan github.com >> /etc/ssh/ssh_known_hosts \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g pnpm@10 yarn
RUN python -m pip install --no-cache-dir uv

COPY requirements.txt .
RUN python -m venv /opt/mcp-venv \
    && /opt/mcp-venv/bin/python -m pip install --no-cache-dir --upgrade pip \
    && /opt/mcp-venv/bin/python -m pip install --no-cache-dir -r requirements.txt
RUN /opt/mcp-venv/bin/python -m playwright install --with-deps chromium

COPY app ./app
COPY scripts/container-entrypoint.sh /usr/local/bin/agent-mcp-entrypoint
RUN chmod 755 /usr/local/bin/agent-mcp-entrypoint

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/agent-mcp-entrypoint"]
CMD ["/opt/mcp-venv/bin/python", "-m", "app.server"]
