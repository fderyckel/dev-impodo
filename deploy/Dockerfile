# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

FROM ${PYTHON_IMAGE} AS builder

ARG UV_VERSION=0.13.4
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/impodo/venv
WORKDIR /build

RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable --no-cache \
    && chmod -R a-w /opt/impodo/venv


FROM ${PYTHON_IMAGE} AS runtime

ARG IMPODO_UID=10001
ARG IMPODO_GID=10001

ENV HOME=/nonexistent \
    PATH=/opt/impodo/venv/bin:${PATH} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/var/tmp/impodo

RUN groupadd --gid "${IMPODO_GID}" impodo \
    && useradd --uid "${IMPODO_UID}" --gid "${IMPODO_GID}" \
        --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin impodo \
    && install -d -o "${IMPODO_UID}" -g "${IMPODO_GID}" -m 0750 \
        /var/lib/impodo/artifacts \
        /var/lib/impodo/runtime \
        /var/tmp/impodo

COPY --from=builder --chown=0:0 /opt/impodo/venv /opt/impodo/venv

USER 10001:10001
WORKDIR /opt/impodo

ENTRYPOINT ["impodo-server"]
CMD ["web"]
