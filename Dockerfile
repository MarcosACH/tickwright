# Runs the tickwright CLI. The engine behaves exactly as it does outside a container.
# Config comes from TICKWRIGHT_* variables at run time. Nothing secret is built in.
#
# Base images are pinned by digest, the same way ci.yml pins actions. Dependabot
# bumps the tag and the digest together.

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.13-slim-bookworm@sha256:a881a81282cca8c57f97014867fa651def1b3799030cc6b1cff45ed55ba8ef36 AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm@sha256:a881a81282cca8c57f97014867fa651def1b3799030cc6b1cff45ed55ba8ef36
# The venv scripts point at /app/.venv, so it keeps the same path here.
COPY --from=build /app/.venv /app/.venv
RUN apt-get update \
    && apt-get install --yes --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10001 --no-create-home tickwright \
    && mkdir /data && chown tickwright /data
# A user mounts their strategy code at /strategies and names it by import path (ADR-0061).
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONPATH=/strategies
# The SQLite store is written relative to the working directory.
WORKDIR /data
USER tickwright
# tini is PID 1 and passes every signal to the engine. The kernel drops a signal sent to a
# PID 1 with no handler, and the engine has none until it has imported and built (#604).
# The `tickwright` command holds SIGUSR1 and SIGUSR2 until then, so keep it here (#615).
ENTRYPOINT ["tini", "--", "tickwright"]
