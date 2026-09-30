#!/usr/bin/env bash
# Deletes the seller-account test stack made by scripts/deploy-test.sh, and its
# secret. Refuses anything not tagged nlsql:purpose=test.
#
# Usage: scripts/destroy-test.sh [env-file]
#
# The env file is optional and only read for AwsS3BucketName, so that bucket
# is reported and never deleted: it may hold real documents.
#
# Environment overrides (same defaults as deploy-test.sh):
#   STACK_NAME           default nlsql-ai-agent-test
#   AWS_REGION           default us-east-1
#   SECRET_NAME          default $STACK_NAME/credentials
#   KEEP_SECRET=1        keep the secret
#   FORCE_DELETE_SECRET=1  delete the secret now instead of after 7 days
#   PURGE_BUCKET=1       empty and delete the bucket the stack created (it is
#                        retained on stack deletion by design)
set -euo pipefail

ENV_FILE="${1:-}"
STACK_NAME="${STACK_NAME:-nlsql-ai-agent-test}"
REGION="${AWS_REGION:-us-east-1}"
SECRET_NAME="${SECRET_NAME:-$STACK_NAME/credentials}"
TEST_TAG_KEY="nlsql:purpose"
TEST_TAG_VALUE="test"
export AWS_REGION="$REGION" AWS_PAGER=""

ENV_BUCKET=""
if [ -n "$ENV_FILE" ]; then
  [ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found" >&2; exit 2; }
  ENV_BUCKET="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}AwsS3BucketName=//p' "$ENV_FILE" \
    | tail -n 1 | tr -d "\"'\r")"
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Account $ACCOUNT_ID, Region $REGION, stack $STACK_NAME"

stack_tag="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Tags[?Key=='$TEST_TAG_KEY'].Value | [0]" --output text 2>/dev/null || true)"
created_bucket=""
if [ -z "$stack_tag" ]; then
  echo "Stack $STACK_NAME does not exist"
elif [ "$stack_tag" != "$TEST_TAG_VALUE" ]; then
  echo "ERROR: $STACK_NAME is not tagged $TEST_TAG_KEY=$TEST_TAG_VALUE; refusing to delete it" >&2
  exit 1
else
  existing="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Parameters[?ParameterKey=='ExistingDocumentsBucket'].ParameterValue | [0]" --output text)"
  if [ -z "$existing" ] || [ "$existing" = None ]; then
    created_bucket="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
      --query "Stacks[0].Outputs[?OutputKey=='DocumentsBucket'].OutputValue | [0]" --output text)"
    [ "$created_bucket" != None ] || created_bucket=""
  fi
  aws cloudformation delete-stack --stack-name "$STACK_NAME"
  echo "Deleting stack $STACK_NAME (CloudFront makes this take a while)..."
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME"
  echo "Deleted stack $STACK_NAME"
fi

if [ "${KEEP_SECRET:-0}" != 1 ]; then
  secret_tag="$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" \
    --query "Tags[?Key=='$TEST_TAG_KEY'].Value | [0]" --output text 2>/dev/null || true)"
  if [ -z "$secret_tag" ]; then
    echo "Secret $SECRET_NAME does not exist"
  elif [ "$secret_tag" != "$TEST_TAG_VALUE" ]; then
    echo "Secret $SECRET_NAME is not tagged $TEST_TAG_KEY=$TEST_TAG_VALUE; left in place"
  elif [ "${FORCE_DELETE_SECRET:-0}" = 1 ]; then
    aws secretsmanager delete-secret --secret-id "$SECRET_NAME" --force-delete-without-recovery >/dev/null
    echo "Deleted secret $SECRET_NAME"
  else
    aws secretsmanager delete-secret --secret-id "$SECRET_NAME" --recovery-window-in-days 7 >/dev/null
    echo "Scheduled secret $SECRET_NAME for deletion in 7 days"
  fi
fi

if [ -n "$created_bucket" ]; then
  if [ "${PURGE_BUCKET:-0}" = 1 ]; then
    aws s3 rm "s3://$created_bucket" --recursive
    aws s3api delete-bucket --bucket "$created_bucket"
    echo "Emptied and deleted bucket $created_bucket"
  else
    echo "Kept bucket $created_bucket (retained by the stack). PURGE_BUCKET=1 deletes it."
  fi
fi
if [ -n "$ENV_BUCKET" ]; then
  echo "Bucket $ENV_BUCKET from the env file was not touched."
fi
echo "The test ECR repository, if any, is not touched: delete it separately when done."
