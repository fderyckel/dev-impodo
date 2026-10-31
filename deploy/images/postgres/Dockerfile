# syntax=docker/dockerfile:1.7

ARG GO_IMAGE=golang:1.27.3-alpine3.23@sha256:8a5046497cda39860b832ffdbe8dd66018a0f05bb01352f533a36e7e2ced66a0
ARG POSTGRES_IMAGE=postgres:18.6-bookworm@sha256:5c4783a8cc3f0074912164f94dfa1be4e043438f0f707cf968d7af9a0d31a2c8

FROM --platform=$BUILDPLATFORM ${GO_IMAGE} AS compiler
ARG TARGETOS
ARG TARGETARCH
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
ADD --checksum=sha256:cd9719b775dbfedae53923c9b0dc792b66d42c51e0b36652ed6f747fbadc0164 \
    https://github.com/tianon/gosu/archive/refs/tags/1.19.tar.gz /tmp/gosu.tar.gz
RUN mkdir /source && tar -xzf /tmp/gosu.tar.gz --strip-components=1 -C /source
WORKDIR /source
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    case "${TARGETARCH}" in amd64|arm64) ;; *) exit 1 ;; esac \
    && GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" \
       go build -mod=readonly -trimpath -buildvcs=false -ldflags='-s -w' -o /out/gosu . \
    && go version -m /out/gosu

FROM ${POSTGRES_IMAGE} AS runtime
COPY --from=compiler --chmod=0755 /out/gosu /usr/local/bin/gosu
COPY --from=compiler /source/LICENSE /usr/local/share/licenses/impodo-gosu/LICENSE
RUN gosu --version && test "$(gosu nobody id -u)" = "65534"
LABEL org.opencontainers.image.title="Impodo PostgreSQL rehearsal image" \
      org.opencontainers.image.version="18.6-impodo1" \
      io.impodo.gosu.version="1.19" \
      io.impodo.go.version="1.27.3"
