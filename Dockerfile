FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8

# tmux + ttyd are the only hard dependencies; the server itself is stdlib-only Python.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tmux ttyd python3 git curl ca-certificates openssh-client nano less procps \
    && rm -rf /var/lib/apt/lists/*

# Optional: bake AI coding agents into the image.
#   docker compose build --build-arg INSTALL_AGENTS=1
ARG INSTALL_AGENTS=0
RUN if [ "$INSTALL_AGENTS" = "1" ]; then \
        curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
        && apt-get install -y --no-install-recommends nodejs \
        && npm install -g @anthropic-ai/claude-code @openai/codex opencode-ai \
        && npm cache clean --force && rm -rf /var/lib/apt/lists/*; \
    fi

# Unprivileged user "thumb" (uid 1000); ubuntu:24.04 ships "ubuntu" with that uid.
RUN userdel -r ubuntu 2>/dev/null || true \
    && useradd -m -u 1000 -s /bin/bash thumb

COPY server /app/server
COPY deploy/tmux.conf /app/deploy/tmux.conf

ENV THUMBMUX_HOST=0.0.0.0 \
    THUMBMUX_PORT=7682 \
    THUMBMUX_WORKDIR=/home/thumb/workspace \
    THUMBMUX_DATA_DIR=/home/thumb/.thumbmux \
    THUMBMUX_TMUX_CONF=/app/deploy/tmux.conf

USER thumb
WORKDIR /home/thumb
RUN mkdir -p /home/thumb/workspace
EXPOSE 7682
HEALTHCHECK --interval=30s --timeout=3s CMD curl -fs http://127.0.0.1:7682/healthz || exit 1
CMD ["python3", "/app/server/app.py"]
