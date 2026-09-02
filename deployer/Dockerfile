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
FROM gcr.io/cloud-marketplace-tools/k8s/deployer_helm/onbuild

# The base does NOT package the integration test. `mpdev verify` reads it from
# /data-test in this same image, so pack it the same way the base packs /data:
# the chart directory is renamed to `chart` inside the tarball.
COPY apptest/deployer/chart/nlsql-tester /tmp/test/chart
RUN cd /tmp/test \
 && tar -czf nlsql-tester.tar.gz chart \
 && mkdir -p /data-test/chart \
 && mv nlsql-tester.tar.gz /data-test/chart/ \
 && rm -rf /tmp/test

COPY apptest/deployer/schema.yaml /data-test/schema.yaml
