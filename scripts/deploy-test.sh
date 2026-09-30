#!/usr/bin/env bash
# Seller-account test stack for NLSQL AI Agent. Not for buyers.
#
# Usage: scripts/deploy-test.sh <env-file> [DocsAndSql|DocsOnly]
#
# <env-file> is a local, gitignored file of application env names (KEY=VALUE),
# never committed. From it this script:
#   1. creates or updates the test Secrets Manager secret (AppPassword, plus
#      ApiToken and DbPassword in DocsAndSql), passing values only through a
#      0600 temporary file;
#   2. uses AwsS3BucketName as ExistingDocumentsBucket, creating the bucket if
#      it does not exist (unset: the stack creates one);
#   3. deploys a temporary copy of the template whose ImageUri pattern also
#      accepts a private test repository, so tests never spend a tag in the
#      Marketplace repository.
# Running it again updates the same stack, which is how to switch modes.
#
# Keys read: connections__serviceConnection__settings__clientId / clientSecret /
# tenantId (or MicrosoftAppId / MicrosoftAppPassword / MicrosoftAppTenantId),
# ApiToken, DbPassword, DatabaseType, DataSource, DbName, DbPort, DbSchema,
# DbUser, Warehouse, LanguageModelName, LanguageModelContext, Streaming,
# AwsS3BucketName. Values are never printed.
#
# Environment overrides:
#   STACK_NAME      default nlsql-ai-agent-test
#   AWS_REGION      default us-east-1 (AWS requires a test task in us-east-1)
#   VERSION         default 1.0.0
#   IMAGE_URI       default <account>.dkr.ecr.<region>.amazonaws.com/nlsql-ai-agent-test:$VERSION
#   SECRET_NAME     default $STACK_NAME/credentials
#   VPC_ID          default the Region's default VPC
#   SUBNET_IDS      comma-separated; default the default VPC's default-for-AZ subnets
#   LANGUAGE_MODEL  default LanguageModelName from the env file, else the template default
#   DOCS_DIR        optional local folder synced to the documents bucket after deploy
set -euo pipefail

usage() { sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

ENV_FILE="${1:-}"
MODE="${2:-DocsAndSql}"
[ -n "$ENV_FILE" ] || usage
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found" >&2; exit 2; }
case "$MODE" in DocsAndSql|DocsOnly) ;; *) usage ;; esac

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO_ROOT/cloudformation/nlsql-ai-agent-ecs-fargate.yaml"
STACK_NAME="${STACK_NAME:-nlsql-ai-agent-test}"
REGION="${AWS_REGION:-us-east-1}"
VERSION="${VERSION:-1.0.0}"
SECRET_NAME="${SECRET_NAME:-$STACK_NAME/credentials}"
TEST_TAG_KEY="nlsql:purpose"
TEST_TAG_VALUE="test"
export AWS_REGION="$REGION" AWS_PAGER=""

# An env file inside this public repository must never be tracked.
if git -C "$REPO_ROOT" ls-files --error-unmatch "$ENV_FILE" >/dev/null 2>&1; then
  echo "ERROR: $ENV_FILE is tracked by git; refusing to read it" >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE_URI="${IMAGE_URI:-$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/nlsql-ai-agent-test:$VERSION}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/nlsql-ai-agent-test.XXXXXX")"
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT

# Parse the env file without sourcing it, and write the secret JSON, the stack
# parameters and the documents bucket name into the private work directory.
python3 - "$ENV_FILE" "$MODE" "$WORK" "${LANGUAGE_MODEL:-}" <<'PY'
import json, os, sys

env_file, mode, work, model_override = sys.argv[1:5]
env = {}
with open(env_file, encoding="utf-8") as fh:
    for raw in fh:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key.strip()] = value

def first(*names):
    for name in names:
        if env.get(name):
            return env[name]
    return ""

app_id = first("connections__serviceConnection__settings__clientId", "MicrosoftAppId")
app_secret = first("connections__serviceConnection__settings__clientSecret", "MicrosoftAppPassword")
tenant_id = first("connections__serviceConnection__settings__tenantId", "MicrosoftAppTenantId")

missing = [n for n, v in (("clientId", app_id), ("clientSecret", app_secret), ("tenantId", tenant_id)) if not v]
secret = {}
secret["AppPassword"] = app_secret
params = {
    "Mode": mode,
    "MicrosoftAppId": app_id,
    "MicrosoftAppTenantId": tenant_id,
}
if mode == "DocsAndSql":
    for name in ("ApiToken", "DbPassword", "DatabaseType", "DataSource", "DbName", "DbUser"):
        if not env.get(name):
            missing.append(name)
    secret["ApiToken"] = env.get("ApiToken", "")
    secret["DbPassword"] = env.get("DbPassword", "")
    engines = {"mssql": "MSSQL", "mysql": "MySQL", "postgresql": "PostgreSQL", "postgres": "PostgreSQL",
               "redshift": "Redshift", "snowflake": "Snowflake"}
    db_type = env.get("DatabaseType", "")
    if db_type and db_type.lower() not in engines:
        sys.exit(f"ERROR: DatabaseType {db_type!r} is not one the template offers")
    params["DatabaseType"] = engines.get(db_type.lower(), "")
    for name in ("DataSource", "DbName", "DbPort", "DbSchema", "DbUser", "Warehouse"):
        params[name] = env.get(name, "")
if missing:
    sys.exit("ERROR: the env file lacks " + ", ".join(missing))

model = model_override or env.get("LanguageModelName", "")
if model:
    params["LanguageModel"] = model
if env.get("LanguageModelContext"):
    params["LanguageModelContext"] = env["LanguageModelContext"]
if env.get("Streaming"):
    params["Streaming"] = env["Streaming"].lower()

for key, value in params.items():
    if "\n" in value or "\r" in value:
        sys.exit(f"ERROR: {key} contains a newline")

old_umask = os.umask(0o077)
with open(os.path.join(work, "secret.json"), "w", encoding="utf-8") as fh:
    json.dump(secret, fh)
os.umask(old_umask)
with open(os.path.join(work, "params"), "w", encoding="utf-8") as fh:
    for key, value in params.items():
        fh.write(f"{key}={value}\n")
with open(os.path.join(work, "bucket"), "w", encoding="utf-8") as fh:
    fh.write(env.get("AwsS3BucketName", ""))
PY

# The temporary template: identical except that ImageUri also accepts
# <account>.dkr.ecr.us-east-1.amazonaws.com/nlsql-ai-agent-test:<tag>.
python3 - "$TEMPLATE" "$WORK/template.yaml" <<'PY'
import sys

src, dst = sys.argv[1:3]
marketplace = r"709825985650\.dkr\.ecr\.us-east-1\.amazonaws\.com/nlsql/ai-agent:[0-9]+\.[0-9]+\.[0-9]+"
test_repo = r"[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/nlsql-ai-agent-test:.+"
old = f"AllowedPattern: '^{marketplace}$'"
new = f"AllowedPattern: '^({marketplace}|{test_repo})$'"
text = open(src, encoding="utf-8").read()
if text.count(old) != 1:
    sys.exit("ERROR: the ImageUri AllowedPattern in the template is not the expected one")
open(dst, "w", encoding="utf-8").write(text.replace(old, new))
PY

echo "Account $ACCOUNT_ID, Region $REGION, stack $STACK_NAME, mode $MODE"
echo "Image $IMAGE_URI"

# 1. The credentials secret.
secret_state="$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" \
  --query '[ARN, DeletedDate]' --output text 2>/dev/null || true)"
if [ -z "$secret_state" ]; then
  aws secretsmanager create-secret --name "$SECRET_NAME" \
    --description "NLSQL AI Agent seller test stack $STACK_NAME" \
    --tags "Key=$TEST_TAG_KEY,Value=$TEST_TAG_VALUE" \
    --secret-string "file://$WORK/secret.json" >/dev/null
  echo "Created secret $SECRET_NAME"
else
  case "$secret_state" in
    *None) ;;
    *) aws secretsmanager restore-secret --secret-id "$SECRET_NAME" >/dev/null
       echo "Restored secret $SECRET_NAME from scheduled deletion" ;;
  esac
  aws secretsmanager put-secret-value --secret-id "$SECRET_NAME" \
    --secret-string "file://$WORK/secret.json" >/dev/null
  echo "Updated secret $SECRET_NAME"
fi
rm -f "$WORK/secret.json"
SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --query ARN --output text)"

# 2. The documents bucket named in the env file, if any.
BUCKET="$(cat "$WORK/bucket")"
if [ -n "$BUCKET" ]; then
  if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    if [ "$REGION" = us-east-1 ]; then
      aws s3api create-bucket --bucket "$BUCKET" >/dev/null
    else
      aws s3api create-bucket --bucket "$BUCKET" \
        --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    fi
    aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    aws s3api put-bucket-tagging --bucket "$BUCKET" \
      --tagging "TagSet=[{Key=$TEST_TAG_KEY,Value=$TEST_TAG_VALUE}]"
    echo "Created bucket $BUCKET"
  fi
fi

# 3. Network: the default VPC unless told otherwise.
VPC_ID="${VPC_ID:-$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)}"
[ -n "$VPC_ID" ] && [ "$VPC_ID" != None ] || { echo "ERROR: no default VPC; set VPC_ID and SUBNET_IDS" >&2; exit 1; }
SUBNET_IDS="${SUBNET_IDS:-$(aws ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" Name=default-for-az,Values=true \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',')}"
case "$SUBNET_IDS" in *,*) ;; *) echo "ERROR: need two or more subnets; set SUBNET_IDS" >&2; exit 1 ;; esac

params=(
  "VpcId=$VPC_ID"
  "SubnetIds=$SUBNET_IDS"
  "ImageUri=$IMAGE_URI"
  "CredentialsSecretArn=$SECRET_ARN"
  "ExistingDocumentsBucket=$BUCKET"
)
while IFS= read -r line; do params+=("$line"); done < "$WORK/params"

# 4. Deploy.
if ! aws cloudformation deploy \
    --stack-name "$STACK_NAME" \
    --template-file "$WORK/template.yaml" \
    --capabilities CAPABILITY_IAM \
    --no-fail-on-empty-changeset \
    --tags "$TEST_TAG_KEY=$TEST_TAG_VALUE" \
    --parameter-overrides "${params[@]}"; then
  echo "Deployment failed. Most recent failures:" >&2
  aws cloudformation describe-stack-events --stack-name "$STACK_NAME" --max-items 40 \
    --query "StackEvents[?contains(ResourceStatus, 'FAILED')].[LogicalResourceId, ResourceStatusReason]" \
    --output table >&2 || true
  exit 1
fi

aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey, OutputValue]' --output table

# 5. Optional sample documents, then a new task so they are indexed.
if [ -n "${DOCS_DIR:-}" ]; then
  output() {
    aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
      --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
  }
  docs_bucket="$(output DocumentsBucket)"
  aws s3 sync "$DOCS_DIR" "s3://$docs_bucket/"
  aws ecs update-service --cluster "$(output ClusterName)" --service "$(output ServiceName)" \
    --force-new-deployment >/dev/null
  echo "Synced $DOCS_DIR to $docs_bucket and started a new deployment"
fi
