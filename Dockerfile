# Образ со всеми пакетными менеджерами, нужными reachfinder для шести экосистем.
# Сборка:  docker build -t reachfinder .
# Запуск:  docker run --rm -v "$PWD:/src" reachfinder reach --project /src ...
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/go/bin:${PATH}"

# Базовые утилиты, Python, JVM и Maven.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git python3 python3-pip python3-venv \
        openjdk-21-jdk-headless maven unzip xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (NodeSource).
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Go (официальный tarball).
RUN curl -fsSL https://go.dev/dl/go1.23.4.linux-amd64.tar.gz -o /tmp/go.tgz \
    && tar -C /usr/local -xzf /tmp/go.tgz \
    && rm /tmp/go.tgz

# .NET SDK (репозиторий Microsoft).
RUN curl -fsSL https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb -o /tmp/ms.deb \
    && dpkg -i /tmp/ms.deb && rm /tmp/ms.deb \
    && apt-get update && apt-get install -y --no-install-recommends dotnet-sdk-8.0 \
    && rm -rf /var/lib/apt/lists/*

# uv и Conan.
RUN python3 -m pip install --no-cache-dir uv conan

WORKDIR /app
COPY . /app
RUN python3 -m pip install --no-cache-dir .

ENTRYPOINT ["reachfinder"]
