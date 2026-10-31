---
audience: developer
document_type: reference
status: current
---

# Supporting image builds

These builds repair the helper and proxy dependencies used by the local
rehearsal while preserving PostgreSQL `18.6` and Caddy `2.11.7`. They are
repository-owned derivatives of official images. Build and scan them before
starting the [local trial](../README.md).

## Build inputs

| Build | Inputs and result |
| --- | --- |
| [PostgreSQL Dockerfile](postgres/Dockerfile) | The pinned official `18.6-bookworm` base receives `gosu` 1.19 built with pinned Go 1.27.3. The source archive has an explicit SHA-256 checksum. PostgreSQL, its entry point, UID, and data layout come from the same upstream base. |
| [Caddy Dockerfile](caddy/Dockerfile) | The pinned official `2.11.7-alpine` base receives Caddy 2.11.7 built with pinned Go 1.27.3, `go-jose/v4` 4.1.6, and `x/net` 0.60.0. Brotli is pinned to 1.2.1-r0 and curl/libcurl to 8.23.0-r0. |
| [Caddy module lock](caddy/go.mod) and [checksums](caddy/go.sum) | They record the complete resolved module graph. Builds verify module checksums, disable automatic Go toolchain downloads, and refuse module-file changes during compilation. The small main package imports Caddy's standard modules and embedded time-zone data. |

Both compilers build static binaries for `linux/amd64` or `linux/arm64`.
Each image retains its rebuilt component's license and the Go license. The
SPDX inventory supports the separate release license review.

Compose uses `impodo-postgres:18.6-impodo1` for PostgreSQL and the secret
provisioner, and `impodo-caddy:2.11.7-impodo1` for the proxy. These tags name
local build outputs. Their `pull_policy: never` requires a local build. Record
actual image IDs with each rehearsal; registry publication with immutable
output digests remains a release task.

## Build and scan

From the repository root:

```sh
docker compose --env-file deploy/examples/local-rehearsal.env -f deploy/compose.yaml build --pull postgres proxy
docker image inspect impodo-postgres:18.6-impodo1 impodo-caddy:2.11.7-impodo1 --format '{{.Os}}/{{.Architecture}} {{.Id}}'
```

The Linux workflow builds these same images before its live repository and
runtime tests. Its supporting-image matrix builds each candidate, collects
every critical and high finding, and rejects fixable findings at those
severities. It retains the scan JSON and, after a passing scan, maximum-mode
build provenance and an SPDX SBOM. It publishes no image and needs no registry
credential.

The rejection command retains the existing policy:

```sh
docker run --rm \
  --volume /var/run/docker.sock:/var/run/docker.sock \
  ghcr.io/aquasecurity/trivy:0.76.1@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c \
  image --docker-host unix:///var/run/docker.sock --scanners vuln \
  --exit-code 1 --ignore-unfixed --severity CRITICAL,HIGH \
  impodo-caddy:2.11.7-impodo1
```

Run it again for `impodo-postgres:18.6-impodo1`. A passing result does not
dispose of unfixed findings or qualify a supported host.

## Maintain the builds

Update a compiler or base through its explicit version and image digest.
Update `gosu` through its source version and archive checksum. Resolve a Caddy
dependency change with the pinned Go toolchain and review both lock files.
Keep the Caddy and PostgreSQL application versions stable during a helper-only
repair. A version change needs its own compatibility decision.

Bump the local output revision when accepted inputs change. Build and scan
both architectures, verify the required Caddy modules and non-root behavior,
run the Linux-owned suite on a disposable database, and repeat migration,
browser, restart, and persistence checks. Keep the security policy unchanged
and retain full findings for release review.
