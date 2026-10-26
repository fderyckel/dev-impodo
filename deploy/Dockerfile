# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

FROM ${PYTHON_IMAGE} AS builder

ARG UV_VERSION=0.13.4
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/impodo/venv
WORKDIR /build

RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable --no-cache


FROM ${PYTHON_IMAGE} AS runtime

ARG IMPODO_UID=10001
ARG IMPODO_GID=10001

ENV HOME=/nonexistent \
    IMPODO_ARTIFACT_ROOT=/var/lib/impodo/artifacts \
    IMPODO_RUNTIME_ROOT=/var/lib/impodo/runtime \
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
RUN chmod -R a-w /opt/impodo

VOLUME ["/var/lib/impodo/artifacts"]
USER 10001:10001
WORKDIR /opt/impodo

ENTRYPOINT ["impodo-server"]
CMD ["web"]

