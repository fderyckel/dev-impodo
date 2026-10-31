# syntax=docker/dockerfile:1.7

ARG GO_IMAGE=golang:1.27.3-alpine3.23@sha256:8a5046497cda39860b832ffdbe8dd66018a0f05bb01352f533a36e7e2ced66a0
ARG CADDY_IMAGE=caddy:2.11.7-alpine@sha256:e70b164da81ef698b3ad19af799ea6086ce6d9e48ed4079eb9fe26f6fbf14ee5

FROM --platform=$BUILDPLATFORM ${GO_IMAGE} AS compiler
ARG TARGETOS
ARG TARGETARCH
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
WORKDIR /source
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download && go mod verify
COPY main.go ./
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    case "${TARGETARCH}" in amd64|arm64) ;; *) exit 1 ;; esac \
    && GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" \
       go build -mod=readonly -trimpath -buildvcs=false -ldflags='-s -w' -o /out/caddy . \
    && go version -m /out/caddy \
    && mkdir /licenses \
    && cp /go/pkg/mod/github.com/caddyserver/caddy/v2@v2.11.7/LICENSE /licenses/CADDY-LICENSE

FROM ${CADDY_IMAGE} AS runtime
RUN apk add --no-cache \
    'brotli-libs=1.2.1-r0' 'curl=8.23.0-r0' 'libcurl=8.23.0-r0'
COPY --from=compiler --chmod=0755 /out/caddy /usr/bin/caddy
COPY --from=compiler /licenses/CADDY-LICENSE /usr/share/licenses/impodo-caddy/LICENSE
RUN caddy version
LABEL org.opencontainers.image.title="Impodo Caddy rehearsal image" \
      org.opencontainers.image.version="2.11.7-impodo1" \
      io.impodo.go.version="1.27.3"
