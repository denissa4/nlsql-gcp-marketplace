# The Cloud Marketplace metering agent (ubbagent), rebuilt from upstream source.
#
# Build from the repository root, not from this directory:
#   docker build --platform linux/amd64 --tag "$REGISTRY/ubbagent:$TAG" \
#     --file ubbagent/Dockerfile .
#
# Nothing is read from the build context - every byte comes from the pinned
# upstream commit below. The context is passed only so that one cloudbuild.yaml
# can serve this and deployer/Dockerfile alike.
# ---------------------------------------------------------------------------
# Why this is no longer `crane copy`. Google publishes the agent at
# gcr.io/cloud-marketplace-tools/metering/ubbagent and release 1.5.0 simply
# re-tagged it. Marketplace scans OUR copy, so its CVEs are ours - 24 with fixes
# available:
#
#   CVE-2026-63073, CVE-2026-75803 (+7)   openssl 3.5.7-r0 -> 3.5.8-r0
#   CVE-2026-39821 (+7)                   GO_STDLIB 1.26.5 -> 1.26.6
#   CVE-2026-84304, CVE-2026-84445 (+2)   google.golang.org/grpc 1.81.1 -> 1.83.1
#
# There is nothing newer to copy: `latest` and `0.2.13` are the same digest,
# built 2026-07-15. So rebuild the same release tag with a patched toolchain on
# a freshly upgraded Alpine - exactly what deployer/Dockerfile does for helm and
# kubectl, for the same Go stdlib CVE.

ARG GO_VERSION=1.26.8
ARG UBBAGENT_VERSION=0.2.13
# The commit that tag pointed at when this was written. Pinning both means a
# moved tag fails the build instead of silently changing what we ship - and the
# binary AND docker/ubbagent-start both come from this one commit.
ARG UBBAGENT_COMMIT=34c7f07dbf5a43b4253f5efb3426b912d2404d35
# grpc is a module dependency, so a newer compiler does not fix it. Bumped
# explicitly rather than with `go get -u`, which would move the whole graph.
#
# Note the moving target: the 1.5.0 scan gave 1.83.1 as the fix for
# CVE-2026-84304, but once built on 1.83.1 the scanner reported CVE-2026-84445
# as fixed only in 1.83.2. Re-run `make scan` after every bump rather than
# trusting the fixed-version from the previous report.
ARG GRPC_VERSION=v1.83.2

FROM golang:${GO_VERSION} AS build
ARG GO_VERSION
ARG UBBAGENT_VERSION
ARG UBBAGENT_COMMIT
ARG GRPC_VERSION

# ubbagent's tags are not semver ("0.2.13", no leading v), so the Go module
# proxy publishes no versions for this module and `go install ...@0.2.13` would
# resolve to an opaque pseudo-version - and would not give us
# docker/ubbagent-start either. Clone the tag, the way the kubectl build does.
RUN git clone --depth 1 --branch "${UBBAGENT_VERSION}" \
      https://github.com/GoogleCloudPlatform/ubbagent /src \
 && cd /src \
 && test "$(git rev-parse HEAD)" = "${UBBAGENT_COMMIT}" \
    || { echo "ERROR: tag ${UBBAGENT_VERSION} now resolves to $(git rev-parse HEAD), expected ${UBBAGENT_COMMIT}" >&2; exit 1; }

WORKDIR /src

# Upstream's `make build` is `go install $(IMPORT_PATH)` behind a GOPATH symlink
# shim (.GOPATH/.ok) inherited from a pre-modules Makefile template; the shim
# only exists to land the binary in ./bin and to let `go list ./...` follow the
# symlink for the test and cover targets. ubbagent has an ordinary go.mod, so a
# module-aware `go build` compiles the identical package graph. `make setup`
# installs goimports and gocovmerge, which only `make format` and `make cover`
# use. Upstream's VERSION_FLAGS sets -X main.BuildTime and main.go declares no
# BuildTime, so the linker drops it: there is no version string to reproduce.
#
# GOTOOLCHAIN=local is the point of this stage. Without it, a `toolchain`
# directive in a future go.mod could pull a different toolchain and quietly undo
# the CVE fix. go.mod asks for `go 1.26.5`, which 1.26.8 satisfies, so nothing is
# downloaded today - this only guarantees it stays that way.
#
# CGO_ENABLED=0 gives a static binary and the pure-Go resolver: no libc, no NSS.
RUN GOTOOLCHAIN=local go get google.golang.org/grpc@${GRPC_VERSION} \
 && GOTOOLCHAIN=local go mod tidy

RUN GOTOOLCHAIN=local CGO_ENABLED=0 \
      go build -trimpath -o /out/ubbagent . \
 && go version -m /out/ubbagent | grep -q "go${GO_VERSION}" \
 && go version -m /out/ubbagent | grep -q "google.golang.org/grpc.*${GRPC_VERSION}" \
 && /out/ubbagent --help 2>&1 | grep -q "local-port"

# ---------------------------------------------------------------------------
FROM alpine:3.23
ARG UBBAGENT_VERSION
ARG UBBAGENT_COMMIT

# `apk upgrade` is what clears the openssl CVEs: the v3.23 branch carries
# openssl 3.5.8-r0, and libcrypto3/libssl3 - the subpackages a scanner reports
# as "openssl" - come up with it. They are in the base image only because
# apk-tools links libcrypto to verify package signatures; nothing this image
# RUNS needs openssl. Note what is deliberately NOT installed:
#
#   * `ca-certificates`, which upstream installs, hard-depends on
#     so:libcrypto.so.3. `ca-certificates-bundle` has no dependencies and ships
#     the /etc/ssl/certs/ca-certificates.crt that a CGO_ENABLED=0 Go binary
#     reads - all servicecontrol.googleapis.com needs.
#   * `gettext`, which upstream installs as a virtual package purely to copy
#     /usr/bin/envsubst out of it and delete it again. Alpine ships a
#     `gettext-envsubst` subpackage (envsubst + libintl, nothing else).
#
# The version gate turns a regression into a failed build rather than a failed
# listing review. It uses `set -eu` rather than an && chain with a trailing ||,
# because the || would catch a failed apk fetch too and report it as a stale
# package version - a confusing thing to debug.
RUN set -eu; \
    apk --no-cache upgrade; \
    apk --no-cache add ca-certificates-bundle gettext-envsubst; \
    v="$(apk info -v libcrypto3 | sed 's/^libcrypto3-//')"; \
    echo "openssl (libcrypto3): $v"; \
    if [ "$(apk version -t "$v" 3.5.8-r0)" = "<" ]; then \
      echo "ERROR: libcrypto3 $v is older than 3.5.8-r0" >&2; \
      exit 1; \
    fi

COPY --from=build /out/ubbagent /usr/local/bin/ubbagent

# The entrypoint is upstream's own script, taken from the same commit as the
# binary so the two cannot drift. It reads AGENT_CONFIG_FILE, maps
# AGENT_STATE_DIR to --state-dir or --no-state and AGENT_LOCAL_PORT to
# --local-port or --no-http, expands $AGENT_ENCODED_KEY / $AGENT_CONSUMER_ID in
# the config with envsubst, and execs the agent so it is PID 1 and gets SIGTERM.
# chart/nlsql depends on every one of those - see templates/deployment.yaml and
# templates/ubbagent-configmap.yaml.
COPY --from=build /src/docker/ubbagent-start /usr/local/bin/ubbagent-start

# Fail here rather than in a customer's cluster.
RUN test -x /usr/local/bin/ubbagent-start \
 && command -v envsubst \
 && /usr/local/bin/ubbagent --help 2>&1 | grep -q "local-port"

LABEL org.opencontainers.image.title="Cloud Marketplace metering agent (ubbagent)" \
      org.opencontainers.image.source="https://github.com/GoogleCloudPlatform/ubbagent" \
      org.opencontainers.image.version="${UBBAGENT_VERSION}" \
      org.opencontainers.image.revision="${UBBAGENT_COMMIT}"

# The same command Google's image runs. The chart sets no command/args, so this
# is the process.
CMD ["/usr/local/bin/ubbagent-start"]
