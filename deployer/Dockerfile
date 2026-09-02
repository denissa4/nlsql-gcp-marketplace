# Google Cloud Marketplace deployer image for NLSQL.
#
# The `onbuild` base picks up, from the build context (the repository root):
#   chart/            -> packaged into /data/chart/*.tar.gz
#   schema.yaml       -> /data/schema.yaml
#   apptest/deployer/ -> /data-test/, used by `mpdev verify`
#
# Build from the repository root, not from this directory:
#   docker build --tag "$REGISTRY/deployer:$TAG" --file deployer/Dockerfile .
FROM gcr.io/cloud-marketplace-tools/k8s/deployer_helm/onbuild
