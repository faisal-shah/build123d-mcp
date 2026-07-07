FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update \
    && export DEBIAN_FRONTEND=noninteractive \
    && apt-get install -y --no-install-recommends \
        libegl1 \
        libgl1 \
        libsm6 \
        libxext6 \
        libxrender1 \
        xvfb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV UV_LINK_MODE=copy \
    UV_PYTHON=3.12
