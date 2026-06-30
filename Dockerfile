FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git git-lfs patch curl wget ca-certificates \
        build-essential pkg-config cmake \
        procps tree unzip zip jq ripgrep fd-find \
        iputils-ping dnsutils iproute2 traceroute socat \
        openssh-client telnet rsync \
        docker-cli docker-buildx docker-compose \
        golang-go rustc cargo openjdk-21-jdk-headless \
        sqlite3 postgresql-client \
        nodejs npm \
    && git lfs install --system \
    && mkdir -p /etc/ssh \
    && ssh-keyscan github.com >> /etc/ssh/ssh_known_hosts \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g pnpm yarn

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium

COPY app ./app

EXPOSE 8080

CMD ["python", "-m", "app.server"]
