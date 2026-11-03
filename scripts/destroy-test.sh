#!/usr/bin/env bash
# Deletes the seller-account test stack made by scripts/deploy-test.sh, and its
# secret. Refuses anything not tagged nlsql:purpose=test.
#
# Usage: [PURGE_DATA=1] scripts/destroy-test.sh
#
# The stack keeps its two DynamoDB tables and two S3 buckets when it is deleted
# (they hold users' data, by design). Their names are read from the stack's
# outputs before it is deleted, and listed afterwards; PURGE_DATA=1 deletes
# them too. The stack's generated ENCRYPTION_KEY secret goes with the stack.
#
# Environment overrides (same defaults as deploy-test.sh):
#   STACK_NAME             default nlsql-ai-employee-test
#   AWS_REGION             default us-east-1
#   SECRET_NAME            default $STACK_NAME/credentials
#   KEEP_SECRET=1          keep the credentials secret
#   FORCE_DELETE_SECRET=1  delete the secret now instead of after 7 days
#   PURGE_DATA=1           empty and delete the kept buckets, and delete the
#                          kept tables, each only if tagged nlsql:purpose=test
set -euo pipefail

[ $# -eq 0 ] || { sed -n '5p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

STACK_NAME="${STACK_NAME:-nlsql-ai-employee-test}"
REGION="${AWS_REGION:-us-east-1}"
SECRET_NAME="${SECRET_NAME:-$STACK_NAME/credentials}"
TEST_TAG_KEY="nlsql:purpose"
TEST_TAG_VALUE="test"
export AWS_REGION="$REGION" AWS_PAGER=""

WORK="$(mktemp -d "${TMPDIR:-/tmp}/nlsql-ai-employee-test.XXXXXX")"
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT

# lookup <var> <absent-regex> <aws arguments...>: sets <var> to the command's
# output, or to the empty string when the resource does not exist (its error
# matches <absent-regex>). Any other failure stops the script.
lookup() {
  local var="$1" absent="$2" out
  shift 2
  if out="$(aws "$@" 2>"$WORK/stderr")"; then
    printf -v "$var" '%s' "$out"
  elif grep -qE "$absent" "$WORK/stderr"; then
    printf -v "$var" '%s' ""
  else
    cat "$WORK/stderr" >&2
    exit 1
  fi
}

stack_tag="" secret_tag="" arn="" tag=""
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Account $ACCOUNT_ID, Region $REGION, stack $STACK_NAME"

lookup stack_tag 'does not exist' cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Tags[?Key=='$TEST_TAG_KEY'].Value | [0]" --output text
tables=()
buckets=()
if [ -z "$stack_tag" ]; then
  echo "Stack $STACK_NAME does not exist"
elif [ "$stack_tag" != "$TEST_TAG_VALUE" ]; then
  echo "ERROR: $STACK_NAME is not tagged $TEST_TAG_KEY=$TEST_TAG_VALUE; refusing to delete it" >&2
  exit 1
else
  # The kept tables and buckets, named only in the outputs of the stack.
  kept="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?contains(['UsersTableName', 'TasksTableName', 'UploadsBucketName', 'ConversationsBucketName'], OutputKey)].[OutputKey, OutputValue]" \
    --output text)"
  while read -r key value; do
    case "$key" in
      *TableName) tables+=("$value") ;;
      *BucketName) buckets+=("$value") ;;
    esac
  done <<<"$kept"
  aws cloudformation delete-stack --stack-name "$STACK_NAME"
  echo "Deleting stack $STACK_NAME (CloudFront makes this take a while)..."
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME"
  echo "Deleted stack $STACK_NAME"
fi

if [ "${KEEP_SECRET:-0}" != 1 ]; then
  lookup secret_tag 'ResourceNotFoundException' secretsmanager describe-secret --secret-id "$SECRET_NAME" \
    --query "Tags[?Key=='$TEST_TAG_KEY'].Value | [0]" --output text
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

for table in ${tables[@]+"${tables[@]}"}; do
  if [ "${PURGE_DATA:-0}" != 1 ]; then
    echo "Kept table $table (retained by the stack). PURGE_DATA=1 deletes it."
    continue
  fi
  lookup arn 'ResourceNotFoundException' dynamodb describe-table --table-name "$table" \
    --query Table.TableArn --output text
  if [ -z "$arn" ]; then
    echo "Table $table does not exist"
    continue
  fi
  lookup tag 'ResourceNotFoundException' dynamodb list-tags-of-resource --resource-arn "$arn" \
    --query "Tags[?Key=='$TEST_TAG_KEY'].Value | [0]" --output text
  if [ "$tag" != "$TEST_TAG_VALUE" ]; then
    echo "Table $table is not tagged $TEST_TAG_KEY=$TEST_TAG_VALUE; left in place"
    continue
  fi
  aws dynamodb delete-table --table-name "$table" >/dev/null
  aws dynamodb wait table-not-exists --table-name "$table"
  echo "Deleted table $table"
done

for bucket in ${buckets[@]+"${buckets[@]}"}; do
  if [ "${PURGE_DATA:-0}" != 1 ]; then
    echo "Kept bucket $bucket (retained by the stack). PURGE_DATA=1 deletes it."
    continue
  fi
  # A bucket without tags answers NoSuchTagSet: it exists, and is not a test bucket.
  lookup tag 'NoSuchBucket|NoSuchTagSet' s3api get-bucket-tagging --bucket "$bucket" \
    --query "TagSet[?Key=='$TEST_TAG_KEY'].Value | [0]" --output text
  if grep -q 'NoSuchBucket' "$WORK/stderr"; then
    echo "Bucket $bucket does not exist"
    continue
  fi
  if [ "$tag" != "$TEST_TAG_VALUE" ]; then
    echo "Bucket $bucket is not tagged $TEST_TAG_KEY=$TEST_TAG_VALUE; left in place"
    continue
  fi
  aws s3 rm "s3://$bucket" --recursive --only-show-errors
  aws s3api delete-bucket --bucket "$bucket"
  echo "Emptied and deleted bucket $bucket"
done

if [ -z "$stack_tag" ] && [ "${PURGE_DATA:-0}" = 1 ]; then
  echo "No stack, so no kept tables or buckets are known: find any by the tag $TEST_TAG_KEY=$TEST_TAG_VALUE."
fi
echo "The private test ECR repository is not touched: delete it separately when done."
