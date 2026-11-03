#!/usr/bin/env bash
# Seller-account test stack for NLSQL AI Employee. Not for buyers.
#
# Usage: GEMINI=on|off scripts/deploy-test.sh <env-file>
#
# <env-file> is a local, gitignored file of application env names (KEY=VALUE),
# never committed. From it this script:
#   1. creates or updates the test Secrets Manager secret (AppPassword, plus
#      GeminiApiKey with GEMINI=on), passing values only through a 0600
#      temporary file;
#   2. deploys a temporary copy of the template whose ImageUri pattern also
#      accepts the private test repository, so tests never spend a tag in the
#      Marketplace repository.
# Running it again updates the same stack, which is how to switch GEMINI. Every
# parameter it manages is passed each time: a key missing from the env file
# gets the template default, not the stack's previous value.
#
# Keys read: connections__serviceConnection__settings__clientId / clientSecret /
# tenantId (or MicrosoftAppId / MicrosoftAppPassword / MicrosoftAppTenantId),
# MODEL_NAME, STREAMING, MODEL_MEMORY_TOKENS, SYSTEM_PROMPT, GEMINI_API_KEY,
# GEMINI_MODEL. Values are never printed.
#
# Environment overrides:
#   GEMINI          on or off; default on when the env file has GEMINI_API_KEY
#   STACK_NAME      default nlsql-ai-employee-test
#   AWS_REGION      default us-east-1 (AWS requires a test task in us-east-1)
#   VERSION         default 1.0.0
#   TEST_TAG        default $VERSION-rc1, the tag `make push-test` pushes
#   IMAGE_URI       default <account>.dkr.ecr.<region>.amazonaws.com/nlsql-ai-employee-test:$TEST_TAG
#   SECRET_NAME     default $STACK_NAME/credentials
#   VPC_ID          default the Region's default VPC
#   SUBNET_IDS      comma-separated; default the default VPC's default-for-AZ subnets
#   LANGUAGE_MODEL  default MODEL_NAME from the env file, else the template default
#   GEMINI_MODEL    default GEMINI_MODEL from the env file, else the template default
#
# The stack, its tables, buckets and secrets, and the credentials secret are
# all tagged nlsql:purpose=test. An existing stack or secret without that tag
# is refused, never updated.
set -euo pipefail

usage() { sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

ENV_FILE="${1:-}"
[ -n "$ENV_FILE" ] || usage
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found" >&2; exit 2; }
GEMINI="${GEMINI:-}"
case "$GEMINI" in ""|on|off) ;; *) echo "ERROR: GEMINI must be on or off, not '$GEMINI'" >&2; exit 2 ;; esac

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO_ROOT/cloudformation/nlsql-ai-employee-ecs-fargate.yaml"
STACK_NAME="${STACK_NAME:-nlsql-ai-employee-test}"
REGION="${AWS_REGION:-us-east-1}"
VERSION="${VERSION:-1.0.0}"
TEST_TAG="${TEST_TAG:-$VERSION-rc1}"
SECRET_NAME="${SECRET_NAME:-$STACK_NAME/credentials}"
TEST_TAG_KEY="nlsql:purpose"
TEST_TAG_VALUE="test"
export AWS_REGION="$REGION" AWS_PAGER=""

# An env file must never be tracked, in this public repository or any other.
command -v git >/dev/null 2>&1 || { echo "ERROR: git is needed to check that $ENV_FILE is not tracked" >&2; exit 1; }
if git -C "$(dirname "$ENV_FILE")" ls-files --error-unmatch -- "$(basename "$ENV_FILE")" >/dev/null 2>&1; then
  echo "ERROR: $ENV_FILE is tracked by git; refusing to read it" >&2
  exit 1
fi

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

# Parse the env file without sourcing it, check every value against the
# template's own constraints, and write the secret JSON, the stack parameters
# and the Gemini switch into the private work directory.
python3 - "$ENV_FILE" "$TEMPLATE" "$WORK" "$GEMINI" "${LANGUAGE_MODEL:-}" "${GEMINI_MODEL:-}" <<'PY'
import json, os, re, sys

env_file, template, work, gemini, model_override, gemini_model_override = sys.argv[1:7]
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

text = open(template, encoding="utf-8").read()

def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value

def parameter(name):
    """Default, AllowedValues and MaxLength of a template parameter."""
    match = re.search(rf"^  {re.escape(name)}:\n((?:(?:    .*)?\n)*)", text, re.M)
    if not match:
        sys.exit(f"ERROR: the template has no parameter {name}")
    block = match.group(1)
    found = re.search(r"^    Default: (.*)$", block, re.M)
    default = unquote(found.group(1)) if found else ""
    allowed = None
    found = re.search(r"^    AllowedValues:[ \t]*(\[[^\]]*\])", block, re.M)
    if found:
        allowed = [unquote(v) for v in found.group(1).strip("[]").split(",") if v.strip()]
    elif re.search(r"^    AllowedValues:[ \t]*$", block, re.M):
        allowed = []
        for line in block.split("    AllowedValues:", 1)[1].split("\n")[1:]:
            if not line.startswith("      - "):
                break
            allowed.append(unquote(line[len("      - "):]))
    found = re.search(r"^    MaxLength: ([0-9]+)$", block, re.M)
    return default, allowed, int(found.group(1)) if found else None

def first(*names):
    for name in names:
        if env.get(name):
            return env[name]
    return ""

app_id = first("connections__serviceConnection__settings__clientId", "MicrosoftAppId")
app_secret = first("connections__serviceConnection__settings__clientSecret", "MicrosoftAppPassword")
tenant_id = first("connections__serviceConnection__settings__tenantId", "MicrosoftAppTenantId")
gemini_key = env.get("GEMINI_API_KEY", "")
gemini = gemini or ("on" if gemini_key else "off")

missing = [n for n, v in (("clientId", app_id), ("clientSecret", app_secret), ("tenantId", tenant_id)) if not v]
if gemini == "on" and not gemini_key:
    missing.append("GEMINI_API_KEY (GEMINI=on)")
if missing:
    sys.exit("ERROR: the env file lacks " + ", ".join(missing))

secret = {}
secret["AppPassword"] = app_secret
if gemini == "on":
    secret["GeminiApiKey"] = gemini_key

params = {"MicrosoftAppId": app_id, "MicrosoftAppTenantId": tenant_id}
# Template parameter: (value from the override or the env file, its source).
chosen = {
    "LanguageModel": (model_override or env.get("MODEL_NAME", ""), "LANGUAGE_MODEL or MODEL_NAME"),
    "Streaming": (env.get("STREAMING", "").lower(), "STREAMING"),
    "ConversationMemoryTokens": (env.get("MODEL_MEMORY_TOKENS", ""), "MODEL_MEMORY_TOKENS"),
    "AdditionalInstructions": (env.get("SYSTEM_PROMPT", ""), "SYSTEM_PROMPT"),
    "GeminiModel": (gemini_model_override or env.get("GEMINI_MODEL", ""), "GEMINI_MODEL"),
}
for name, (value, source) in chosen.items():
    default, allowed, max_length = parameter(name)
    value = value or default
    if allowed is not None and value not in allowed:
        sys.exit(f"ERROR: {source} {value!r} is not one of the template's {name} values: " + ", ".join(allowed))
    if max_length is not None and len(value) > max_length:
        sys.exit(f"ERROR: {source} is longer than the template's {max_length} characters")
    params[name] = value
params["GeminiFeatures"] = "Enabled" if gemini == "on" else "Disabled"

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
with open(os.path.join(work, "gemini"), "w", encoding="utf-8") as fh:
    fh.write(gemini)
PY
GEMINI="$(cat "$WORK/gemini")"

# The temporary template: identical except that ImageUri also accepts
# <account>.dkr.ecr.us-east-1.amazonaws.com/nlsql-ai-employee-test:<tag>.
# `make check-vars` checks that `marketplace` is still the template's pattern.
python3 - "$TEMPLATE" "$WORK/template.yaml" <<'PY'
import sys

src, dst = sys.argv[1:3]
marketplace = r"709825985650\.dkr\.ecr\.us-east-1\.amazonaws\.com/nlsql/ai-employee:[0-9]+\.[0-9]+\.[0-9]+"
test_repo = r"[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/nlsql-ai-employee-test:.+"
old = f"AllowedPattern: '^{marketplace}$'"
new = f"AllowedPattern: '^({marketplace}|{test_repo})$'"
text = open(src, encoding="utf-8").read()
if text.count(old) != 1:
    sys.exit("ERROR: the ImageUri AllowedPattern in the template is not the expected one")
open(dst, "w", encoding="utf-8").write(text.replace(old, new))
PY

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE_URI="${IMAGE_URI:-$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/nlsql-ai-employee-test:$TEST_TAG}"
case "$IMAGE_URI" in
  */nlsql-ai-employee-test:*) ;;
  *) echo "ERROR: IMAGE_URI must be in the private nlsql-ai-employee-test repository, never the Marketplace one" >&2
     exit 1 ;;
esac

echo "Account $ACCOUNT_ID, Region $REGION, stack $STACK_NAME, Gemini $GEMINI"
echo "Image $IMAGE_URI"

# 1. Only a test stack is ever updated.
stack_tag=""
lookup stack_tag 'does not exist' cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Tags[?Key=='$TEST_TAG_KEY'].Value | [0]" --output text
if [ -n "$stack_tag" ] && [ "$stack_tag" != "$TEST_TAG_VALUE" ]; then
  echo "ERROR: $STACK_NAME exists and is not tagged $TEST_TAG_KEY=$TEST_TAG_VALUE; refusing to update it" >&2
  exit 1
fi

# 2. The credentials secret, likewise only when it is a test secret.
secret_state=""
lookup secret_state 'ResourceNotFoundException' secretsmanager describe-secret --secret-id "$SECRET_NAME" \
  --query "[DeletedDate, Tags[?Key=='$TEST_TAG_KEY'].Value | [0]]" --output text
if [ -z "$secret_state" ]; then
  aws secretsmanager create-secret --name "$SECRET_NAME" \
    --description "NLSQL AI Employee seller test stack $STACK_NAME" \
    --tags "Key=$TEST_TAG_KEY,Value=$TEST_TAG_VALUE" \
    --secret-string "file://$WORK/secret.json" >/dev/null
  echo "Created secret $SECRET_NAME"
else
  read -r deleted secret_tag <<<"$secret_state"
  if [ "$secret_tag" != "$TEST_TAG_VALUE" ]; then
    echo "ERROR: secret $SECRET_NAME is not tagged $TEST_TAG_KEY=$TEST_TAG_VALUE; refusing to change it" >&2
    exit 1
  fi
  if [ "$deleted" != None ]; then
    aws secretsmanager restore-secret --secret-id "$SECRET_NAME" >/dev/null
    echo "Restored secret $SECRET_NAME from scheduled deletion"
  fi
  aws secretsmanager put-secret-value --secret-id "$SECRET_NAME" \
    --secret-string "file://$WORK/secret.json" >/dev/null
  echo "Updated secret $SECRET_NAME"
fi
rm -f "$WORK/secret.json"
SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --query ARN --output text)"

# 3. Network: the default VPC unless told otherwise.
VPC_ID="${VPC_ID:-$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)}"
if [ -z "$VPC_ID" ] || [ "$VPC_ID" = None ]; then
  echo "ERROR: no default VPC; set VPC_ID and SUBNET_IDS" >&2
  exit 1
fi
SUBNET_IDS="${SUBNET_IDS:-$(aws ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" Name=default-for-az,Values=true \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',')}"
case "$SUBNET_IDS" in *,*) ;; *) echo "ERROR: need two or more subnets; set SUBNET_IDS" >&2; exit 1 ;; esac

params=(
  "VpcId=$VPC_ID"
  "SubnetIds=$SUBNET_IDS"
  "ImageUri=$IMAGE_URI"
  "CredentialsSecretArn=$SECRET_ARN"
)
while IFS= read -r line; do params+=("$line"); done < "$WORK/params"

# 4. Deploy. The stack's tag propagates to its tables, buckets and secrets.
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
echo "For a real Teams test: set the Azure Bot's messaging endpoint to MessagingEndpoint, and add"
echo "OAuthRedirectUri as a Web redirect URI of the bot's app registration in Microsoft Entra."
