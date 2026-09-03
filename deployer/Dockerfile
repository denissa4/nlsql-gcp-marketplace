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
