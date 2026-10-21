# Google Cloud Marketplace deployer image for NLSQL.
#
# Build from the repository root, not from this directory:
#   docker build --platform linux/amd64 --tag "$REGISTRY/deployer:$TAG" \
#     --file deployer/Dockerfile .
#
# The `onbuild` base picks up, from the build context root:
#   chart/       -> packaged into /data/chart/chart.tar.gz
#   schema.yaml  -> /data/schema.yaml
#
# Note it then runs `envsubst` over /data/schema.yaml with ONLY $REGISTRY and
# $TAG in the environment, so any other $VARIABLE in schema.yaml would be
# replaced with an empty string. (Regex anchors like `$'` are not variable
# references and survive untouched.)
# ---------------------------------------------------------------------------
# Google's deployer base ships helm and kubectl built with go1.26.5, which
# carries CVE-2026-39821 (and several other Go stdlib CVEs). Cloud Marketplace
# scans OUR image, so we are responsible for them, but there is no newer base:
# `latest` and `public-image-13.0.10` are the same digest. Upstream release
# binaries do not help either - helm v4.2.4 and kubectl v1.36.4 are themselves
# built with go1.26.5.
#
# So rebuild both from their exact upstream tags with a patched toolchain. Same
# source, same versions, newer stdlib. Keep kubectl on 1.36.x: the wrapper at
# /usr/bin/kubectl picks a client by server version and kubectl only supports a
# one-minor skew, so jumping to 1.37 would break against the 1.35 clusters
# Marketplace verifies on.
# 1.26.9 for release 1.10.0: 1.26.8 carries CVE-2026-56857 and CVE-2026-78663
# (both critical) plus CVE-2026-56866/78659/78660/78667/78669/94439/94440/94444/
# 94447/97031/97032, all fixed in the 1.26.9 stdlib.
FROM golang:1.26.9 AS gobuild
# v4.3.0 rather than v4.2.4: helm vendors oras.land/oras-go/v2, and v4.2.4 pins
# 2.6.1 which carries CVE-2026-50163 (high). v4.3.0 pins 2.6.2. There is no way
# to override a dependency through `go install pkg@version`, so the helm release
# is the lever.
ARG HELM_VERSION=v4.3.0
# Two clients, because the base now ships only v1.37 and the wrapper needs a
# candidate it will actually pick - see the COPY block in the final stage.
ARG KUBECTL_VERSION=v1.36.4
ARG KUBECTL_DEFAULT_VERSION=v1.37.1

RUN CGO_ENABLED=0 go install \
      -ldflags "-X helm.sh/helm/v4/internal/version.version=${HELM_VERSION}" \
      helm.sh/helm/v4/cmd/helm@${HELM_VERSION}

RUN git clone --depth 1 --branch ${KUBECTL_VERSION} \
      https://github.com/kubernetes/kubernetes /src \
 && cd /src \
 && CGO_ENABLED=0 go build -o /go/bin/kubectl \
      -ldflags "-X k8s.io/component-base/version.gitVersion=${KUBECTL_VERSION} \
                -X k8s.io/client-go/pkg/version.gitVersion=${KUBECTL_VERSION}" \
      ./cmd/kubectl

RUN git clone --depth 1 --branch ${KUBECTL_DEFAULT_VERSION} \
      https://github.com/kubernetes/kubernetes /src-default \
 && cd /src-default \
 && CGO_ENABLED=0 go build -o /go/bin/kubectl-default \
      -ldflags "-X k8s.io/component-base/version.gitVersion=${KUBECTL_DEFAULT_VERSION} \
                -X k8s.io/client-go/pkg/version.gitVersion=${KUBECTL_DEFAULT_VERSION}" \
      ./cmd/kubectl

# ---------------------------------------------------------------------------
FROM gcr.io/cloud-marketplace-tools/k8s/deployer_helm/onbuild

# Google's base is Ubuntu 22.04 and is a point-in-time snapshot, so its OS
# packages drift out of date exactly the way the app image's did. Marketplace
# scans the deployer too - that is why helm and kubectl are rebuilt above - and
# an un-upgraded base carried 42 findings with fixes available (glibc, perl,
# openssl, libssh, util-linux, python3.10, pip, curl). One upgrade clears them.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Replace the vulnerable Go binaries. /usr/bin/kubectl is a bash wrapper and is
# left alone.
#
# Read that wrapper before changing these paths. It builds its candidates as
# "$KUBECTL_BASE_LOCATION/${major}.${minor}/kubectl" - with NO `v` prefix - from
# the server version, and falls back to /opt/kubectl/default/kubectl. The base
# image's only client directory is `v1.37`, WITH the prefix, so no candidate ever
# matches and every invocation resolves through `default` to v1.37.
#
# Up to 1.9.0 this file copied the rebuilt client to /opt/kubectl/v1.36/kubectl,
# which the wrapper can never select, so the rebuild had no effect and the base's
# own v1.37 kubectl - carrying the Go 1.26.8 CVEs - is what actually ran. Hence
# two copies now:
#
#   v1.37/kubectl  overwrites the base's binary, which `default` points at and
#                  which the wrapper also runs just to read the server version.
#   1.36/kubectl   the wrapper's own naming, so it is genuinely selectable. It
#                  covers 1.35, 1.36 and 1.37 servers within kubectl's supported
#                  one-minor skew - including the 1.35 clusters Marketplace
#                  verifies on, which a v1.37 client is two minors away from.
COPY --from=gobuild /go/bin/helm            /usr/local/bin/helm
COPY --from=gobuild /go/bin/kubectl-default /opt/kubectl/v1.37/kubectl
COPY --from=gobuild /go/bin/kubectl         /opt/kubectl/1.36/kubectl

# The base does NOT package the integration test. `mpdev verify` overlays the
# test chart ONTO the main chart, matching them up by tarball basename (see
# /bin/create_manifests.sh and /usr/bin/overlay_test_files.py in this image), so
# the test archive must also be called chart.tar.gz with `chart` as its top-level
# directory. The overlay adds the tester Pod and merges its values.yaml in.
COPY apptest/deployer/chart/nlsql /tmp/test/chart
RUN cd /tmp/test \
 && tar -czf chart.tar.gz chart \
 && mkdir -p /data-test/chart \
 && mv chart.tar.gz /data-test/chart/ \
 && rm -rf /tmp/test

# Merged into /data/schema.yaml in test mode by overlay_test_schema.py.
COPY apptest/deployer/schema.yaml /data-test/schema.yaml

# The deployer defaults both waits to 300s (see /bin/deploy_with_tests.sh). The
# NLSQL image is ~750MB, so on a cold node the pull alone can eat most of that
# before the app or the tester Pod has started. Raise both; they bound how long
# `mpdev verify` and Marketplace's own verification will wait.
ENV WAIT_FOR_READY_TIMEOUT=900
ENV TESTER_TIMEOUT=900
