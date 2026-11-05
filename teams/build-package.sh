#!/usr/bin/env bash
# Builds the Microsoft Teams app package for one NLSQL AI Employee stack.
#
# Usage: teams/build-package.sh --stack <name> [--region <region>] [options]
#        teams/build-package.sh --app-id <guid> --domain <d...cloudfront.net> [options]
#
# It fills in teams/manifest.template.json and zips the result with the two
# icons. Each value comes from the first of these that has it:
#   bot app ID     --app-id; the env var connections__serviceConnection__settings__clientId
#                  or MicrosoftAppId; the stack's MicrosoftAppId parameter
#   domain         --domain; the stack's CloudFrontDomain output
#   Browser-Use    --browser-view / --no-browser-view; with --stack, whether
#   help tab       the stack has the BrowserViewUrl output (BrowserAgent
#                  Enabled; before template 1.0.2, GeminiFeatures Enabled);
#                  otherwise added
# With --stack the script reads the stack once (aws cloudformation
# describe-stacks); without it, it makes no AWS call at all.
#
# Options:
#   --version x.y.z   the Teams app's version (default: the template's). Raise
#                     it for every re-upload of the same app
#   --out-dir <dir>   where manifest.json and the zip go (default: teams/)
#
# Writes manifest.json and nlsql-ai-employee-teams.zip. Both are gitignored,
# because they carry a real bot ID. Needs bash, jq or python3, and zip or
# python3; AWS CloudShell has all of them.
set -euo pipefail

usage() { sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
die() { echo "ERROR: $*" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/manifest.template.json"
ZIP_NAME="nlsql-ai-employee-teams.zip"

STACK="" REGION="" VERSION="" OUT_DIR="$HERE" BROWSER_VIEW=""
APP_ID="${connections__serviceConnection__settings__clientId:-${MicrosoftAppId:-}}"
DOMAIN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --stack)           STACK="${2:-}"; shift 2 || usage ;;
    --region)          REGION="${2:-}"; shift 2 || usage ;;
    --app-id)          APP_ID="${2:-}"; shift 2 || usage ;;
    --domain)          DOMAIN="${2:-}"; shift 2 || usage ;;
    --browser-view)    BROWSER_VIEW=yes; shift ;;
    --no-browser-view) BROWSER_VIEW=no; shift ;;
    --version)         VERSION="${2:-}"; shift 2 || usage ;;
    --out-dir)         OUT_DIR="${2:-}"; shift 2 || usage ;;
    -h|--help)         usage ;;
    *)                 echo "ERROR: unknown argument: $1" >&2; usage ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }
have jq || have python3 || die "needs jq or python3"
for f in "$TEMPLATE" "$HERE/color.png" "$HERE/outline.png"; do
  [ -f "$f" ] || die "run it from a full checkout: $HERE needs manifest.template.json, color.png and outline.png"
done

# Prints the value of one stack parameter (kind Parameters) or output (kind
# Outputs) from describe-stacks JSON on stdin; empty when absent.
stack_value() {
  if have jq; then
    jq -r --arg kind "$1" --arg key "$2" \
      '.Stacks[0][$kind] // [] | map(select((.ParameterKey // .OutputKey) == $key))
       | (.[0].ParameterValue // .[0].OutputValue // "")'
  else
    python3 -I -c '
import json, sys
kind, key = sys.argv[1], sys.argv[2]
for item in json.load(sys.stdin)["Stacks"][0].get(kind, []):
    if item.get("ParameterKey", item.get("OutputKey")) == key:
        print(item.get("ParameterValue", item.get("OutputValue", "")))
        break' "$1" "$2"
  fi
}

BROWSER_VIEW_URL=""
if [ -n "$STACK" ]; then
  have aws || die "--stack needs the AWS CLI (or give --app-id and --domain)"
  stack_json="$(aws cloudformation describe-stacks --stack-name "$STACK" \
    ${REGION:+--region "$REGION"} --output json)" || die "cannot read stack $STACK"
  [ -n "$APP_ID" ] || APP_ID="$(stack_value Parameters MicrosoftAppId <<<"$stack_json")"
  [ -n "$DOMAIN" ] || DOMAIN="$(stack_value Outputs CloudFrontDomain <<<"$stack_json")"
  BROWSER_VIEW_URL="$(stack_value Outputs BrowserViewUrl <<<"$stack_json")"
  if [ -z "$BROWSER_VIEW" ]; then
    if [ -n "$BROWSER_VIEW_URL" ]; then BROWSER_VIEW=yes; else BROWSER_VIEW=no; fi
  fi
elif [ -n "$REGION" ]; then
  die "--region needs --stack"
fi
[ -n "$BROWSER_VIEW" ] || BROWSER_VIEW=yes

[ -n "$APP_ID" ] || die "no bot app ID: give --stack, or --app-id <guid>"
[ -n "$DOMAIN" ] || die "no CloudFront domain: give --stack, or --domain <d...cloudfront.net>"
[[ "$APP_ID" =~ ^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$ ]] \
  || die "the bot app ID must be a GUID (the Azure Bot's Microsoft App ID): $APP_ID"
[[ "$DOMAIN" =~ ^d[a-z0-9]+\.cloudfront\.net$ ]] \
  || die "the domain must be the stack's CloudFrontDomain output, like d111111abcdef8.cloudfront.net (no https://, no path): $DOMAIN"
if [ -n "$VERSION" ]; then
  [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "--version must be x.y.z: $VERSION"
fi
# The tab's contentUrl is the viewer page the stack publishes as BrowserViewUrl.
if [ -n "$BROWSER_VIEW_URL" ] && [ "$BROWSER_VIEW_URL" != "https://$DOMAIN/vnc/" ]; then
  die "the stack's BrowserViewUrl ($BROWSER_VIEW_URL) is not https://$DOMAIN/vnc/; this template does not match the stack"
fi
TAB=false
[ "$BROWSER_VIEW" = yes ] && TAB=true

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
MANIFEST="$OUT_DIR/manifest.json"
ZIP="$OUT_DIR/$ZIP_NAME"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Placeholders replaced in every string; without the tab, staticTabs is
# dropped (the bot's chat stays the app's only tab).
if have jq; then
  jq --arg id "$APP_ID" --arg domain "$DOMAIN" --arg version "$VERSION" --argjson tab "$TAB" '
    walk(if type == "string"
         then gsub("<MicrosoftAppId>"; $id) | gsub("<CloudFrontDomain>"; $domain)
         else . end)
    | if $version != "" then .version = $version else . end
    | if $tab then . else del(.staticTabs) end' "$TEMPLATE" >"$work/manifest.json"
else
  python3 -I - "$TEMPLATE" "$APP_ID" "$DOMAIN" "$VERSION" "$TAB" >"$work/manifest.json" <<'EOF'
import json, sys
template, app_id, domain, version, tab = sys.argv[1:]

def fill(node):
    if isinstance(node, dict):
        return {k: fill(v) for k, v in node.items()}
    if isinstance(node, list):
        return [fill(v) for v in node]
    if isinstance(node, str):
        return node.replace("<MicrosoftAppId>", app_id).replace("<CloudFrontDomain>", domain)
    return node

with open(template, encoding="utf-8") as f:
    manifest = fill(json.load(f))
if version:
    manifest["version"] = version
if tab != "true":
    manifest.pop("staticTabs", None)
json.dump(manifest, sys.stdout, indent=2)
print()
EOF
fi
rendered="$(<"$work/manifest.json")"
[[ "$rendered" =~ \"version\":\ *\"([0-9]+\.[0-9]+\.[0-9]+)\" ]] || die "the manifest has no version"
APP_VERSION="${BASH_REMATCH[1]}"
if [[ "$rendered" == *"<MicrosoftAppId>"* || "$rendered" == *"<CloudFrontDomain>"* ]]; then
  die "a placeholder is left in the manifest"
fi
cp "$HERE/color.png" "$HERE/outline.png" "$work/"

rm -f "$ZIP"
if have zip; then
  (cd "$work" && zip -q -X "$ZIP" manifest.json color.png outline.png)
else
  python3 -I - "$work" "$ZIP" <<'EOF'
import os, sys, zipfile
src, dest = sys.argv[1:]
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
    for name in ("manifest.json", "color.png", "outline.png"):
        z.write(os.path.join(src, name), name)
EOF
fi
cp "$work/manifest.json" "$MANIFEST"

echo "  bot app ID         $APP_ID"
echo "  validDomains       $DOMAIN"
if [ "$TAB" = true ]; then
  echo "  Browser-Use tab    https://$DOMAIN/vnc/ (a help page; the live browser opens from the chat)"
else
  echo "  Browser-Use tab    left out (no live browser view: BrowserAgent Disabled, or GeminiFeatures Disabled before 1.0.2)"
fi
echo "  version            $APP_VERSION"
echo "  wrote $MANIFEST"
echo "  wrote $ZIP"
echo "Upload the zip in Teams (Apps, Manage your apps, Upload an app), or give it to a Teams administrator."
