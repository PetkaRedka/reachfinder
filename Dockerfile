# Образ со всеми пакетными менеджерами, нужными reachfinder для шести экосистем.
# Сборка:  docker build -t reachfinder .
# Запуск:  docker run --rm -v "$PWD:/src" reachfinder reach --project /src ...
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/go/bin:${PATH}"

# Базовые утилиты, Python, JVM, Maven и toolchain для C/C++.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates clang cmake curl git make python3 python3-pip \
        python3-venv openjdk-21-jdk-headless maven unzip xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (NodeSource) и SCIP-индексёры для JS/TS и Python.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @sourcegraph/scip-typescript @sourcegraph/scip-python \
    && rm -rf /var/lib/apt/lists/*

# Go (официальный tarball).
RUN curl -fsSL https://go.dev/dl/go1.23.4.linux-amd64.tar.gz -o /tmp/go.tgz \
    && tar -C /usr/local -xzf /tmp/go.tgz \
    && rm /tmp/go.tgz

# scip-go — индексёр для точного резолва вызовов (SCIP).
RUN /usr/local/go/bin/go install github.com/scip-code/scip-go/cmd/scip-go@latest
ENV PATH="/root/go/bin:${PATH}"

# .NET SDK (репозиторий Microsoft) и SCIP-индексёр для C#.
RUN curl -fsSL https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb -o /tmp/ms.deb \
    && dpkg -i /tmp/ms.deb && rm /tmp/ms.deb \
    && apt-get update && apt-get install -y --no-install-recommends dotnet-sdk-8.0 \
    && dotnet tool install --global scip-dotnet --add-source https://api.nuget.org/v3/index.json \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.dotnet/tools:${PATH}"

# Java-индексация использует javac + scip-javac; плагин разрешается Maven при первом
# запуске (отдельный scip-java не нужен).

# scip-clang — SCIP-индексёр для C/C++ (требуется compile_commands.json).
RUN curl -fsSL -o /usr/local/bin/scip-clang \
        https://github.com/sourcegraph/scip-clang/releases/download/v0.4.0/scip-clang-x86_64-linux \
    && chmod +x /usr/local/bin/scip-clang

# uv и Conan.
RUN python3 -m pip install --no-cache-dir --break-system-packages uv conan

WORKDIR /app
COPY . /app
RUN python3 -m pip install --no-cache-dir --break-system-packages .

# Грамматики tree-sitter для офлайн-парсинга. Пакет скачивает их лениво с GitHub
# и в контейнере упирается в 60-секундный таймаут, поэтому кэшируем бандл для
# установленной версии заранее (парсинг нужен всем языкам).
RUN apt-get update && apt-get install -y --no-install-recommends zstd \
    && TS_VERSION="$(python3 -c "import importlib.metadata as m; print(m.version('tree-sitter-language-pack'))")" \
    && mkdir -p "/root/.cache/tree-sitter-language-pack/v${TS_VERSION}/libs" \
    && curl -fsSL --retry 10 --retry-delay 3 --retry-all-errors --max-time 1800 \
        -o /tmp/parsers.tar.zst \
        "https://github.com/xberg-io/tree-sitter-language-pack/releases/download/v${TS_VERSION}/parsers-linux-x86_64.tar.zst" \
    && tar --zstd -xf /tmp/parsers.tar.zst -C "/root/.cache/tree-sitter-language-pack/v${TS_VERSION}/libs" \
    && chown -R 0:0 /root/.cache/tree-sitter-language-pack \
    && chmod -R 700 /root/.cache/tree-sitter-language-pack \
    && rm -f /tmp/parsers.tar.zst \
    && apt-get purge -y zstd && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["reachfinder"]
