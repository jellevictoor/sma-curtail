# Pinned base image: 3.13.5-slim (Bookworm, Oct 2025). Multi-arch (amd64/arm64).
# Don't float to :3.13 — silent jumps to a fresh patch can ship CVEs and break determinism.
FROM python:3.13.5-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Pinned uv: avoids "0.5" floating to a newer patch on rebuild.
COPY --from=ghcr.io/astral-sh/uv:0.5.31 /uv /uvx /usr/local/bin/

# Non-root runtime user.
RUN groupadd --system --gid 1000 app \
 && useradd  --system --uid 1000 --gid app --home /app --shell /sbin/nologin app

WORKDIR /app

# Dependency layer — cached until pyproject or lockfile changes.
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# App code.
COPY --chown=app:app src/ src/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
USER app

EXPOSE 8080 8980

# Curl-less healthcheck via stdlib so the slim image stays slim.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
p=os.environ.get('WEB_PORT','8080'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=3).status == 200 else 1)"

CMD ["sma-web"]
