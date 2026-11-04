#!/usr/bin/env bash
# Offline guards for the NLSQL AI Employee image, run by `make push` and
# `make push-test` before anything is tagged or pushed. Needs docker and the
# local image; never AWS or a network (the image runs with --network none).
#
# Usage: scripts/push-guard.sh <local-image> <product-code> [<pubkey-sha256> [<version>]]
#
#   <local-image>    the enforce image built by the application's `make build`
#                    (run there as `make build IMAGE_LOCAL=<local-image>`)
#   <product-code>   the product code every trust anchor must name
#   <pubkey-sha256>  the sha256 of RegisterUsage public key version 1, from the
#                    Makefile's PUBKEY_SHA256. Empty or not a sha256: the
#                    image is refused.
#   <version>        the release being pushed, the Makefile's VERSION; when
#                    given, the org.opencontainers.image.version label must
#                    equal it
#
# Every rule is checked and every failure reported, so one run lists all of
# them. Labels describe the build; the run checks what the image holds.
#
# The image is resolved to its ID once and every check reads that ID. When
# PUSH_GUARD_ID_FILE is set, the ID is written there only if every rule holds,
# and `make push` tags that ID, so a rebuild after the guard is never pushed.
set -euo pipefail

image="${1:?usage: scripts/push-guard.sh <local-image> <product-code> [<pubkey-sha256> [<version>]]}"
code="${2:?usage: scripts/push-guard.sh <local-image> <product-code> [<pubkey-sha256> [<version>]]}"
pinned="${3:-}"
version="${4:-}"

# The only PEM the image may hold; aws_marketplace_verify.PUBKEY_PATH.
pem=/app/marketplace/aws_marketplace_pubkey_v1.pem

failures=()
fail() { failures+=("$1"); }
id_file="${PUSH_GUARD_ID_FILE:-}"
if [ -n "$id_file" ]; then rm -f "$id_file"; fi

# The application's `make build-local` image signs its own entitlement, and
# its test and e2e images (`FROM runtime`, so with every label below) carry
# pytest, the tests and the local stubs. The content rules below refuse them
# too; refusing their names first makes the reason plain.
case "$image" in
  *-local | *-local:*) fail "the name ends in -local: that image trusts a key baked into it and must never be pushed" ;;
  *-e2e | *-e2e:* | *-test | *-test:*) fail "the name ends in -e2e or -test: the application's test images must never be pushed" ;;
esac

if ! raw="$(docker image inspect "$image" 2>/dev/null)" \
    || ! id="$(docker image inspect -f '{{ .Id }}' "$image" 2>/dev/null)" || [ -z "$id" ]; then
  echo "ERROR: no local image $image (build it in the application repository with \`make build IMAGE_LOCAL=$image\`)" >&2
  exit 1
fi
inspect() { docker image inspect -f "$1" "$id"; }

# On the containerd image store a tag can name an index, whose .Os and
# .Architecture are only its default platform; AWS refuses an index.
if printf '%s' "$raw" | grep -Eq '"mediaType": *"application/vnd\.(oci\.image\.index\.v1|docker\.distribution\.manifest\.list\.v2)\+json"'; then
  fail "the image is an index (several platforms or attestations), not a single manifest: build it with --platform linux/amd64 --provenance=false --sbom=false"
fi
# Docker reports that Descriptor only from API 1.48 (Docker 28). On the
# containerd store (or an engine that does not say which store it uses) an
# older engine leaves the rule above nothing to read, so fail closed there; the
# classic overlay2 store cannot hold an index.
drivers="$(docker info -f '{{ json .DriverStatus }}' 2>/dev/null || echo unknown)"
case "$drivers" in
  *io.containerd.snapshotter* | unknown | '')
    printf '%s' "$raw" | grep -q '"Descriptor"' \
      || fail "docker uses the containerd image store but does not report the image's Descriptor, so an index cannot be told apart: use Docker 28 or newer, or the classic image store" ;;
esac

stage="$(inspect '{{ index .Config.Labels "com.nlsql.stage" }}')"
label_key="$(inspect '{{ index .Config.Labels "com.nlsql.pubkey-sha256" }}')"
platform="$(inspect '{{ .Os }}/{{ .Architecture }}')"
user="$(inspect '{{ .Config.User }}')"
ports="$(inspect '{{ json .Config.ExposedPorts }}')"
volumes="$(inspect '{{ json .Config.Volumes }}')"
image_version="$(inspect '{{ index .Config.Labels "org.opencontainers.image.version" }}')"
entrypoint="$(inspect '{{ json .Config.Entrypoint }}')"
cmd="$(inspect '{{ json .Config.Cmd }}')"
# One JSON string per line, so a value cannot spill into the next entry.
env_entries="$(inspect '{{ range .Config.Env }}{{ json . }}{{ "\n" }}{{ end }}')"

[ "$stage" = enforce ] || fail "the com.nlsql.stage label is '$stage', not enforce"
# The template pins X86_64, and Marketplace scans the amd64 image.
[ "$platform" = linux/amd64 ] || fail "the platform is $platform, not linux/amd64"
# Fargate cannot grant NET_BIND_SERVICE, and AWS rejects root containers.
case "$user" in
  10001 | 10001:10001) ;;
  *) fail "the image runs as user '$user', not 10001" ;;
esac
[ "$ports" = '{"8080/tcp":{}}' ] || fail "the image exposes $ports, not exactly 8080/tcp"
# A VOLUME becomes a separate mount in every container, which the Fargate task
# never needs and which could hide files from the content scan below.
case "$volumes" in
  null | '{}' | '') ;;
  *) fail "the image declares VOLUME $volumes; the task needs none" ;;
esac
# What the application's `make inspect` pins: tini runs startup.sh, which
# starts supervisord. The test and e2e images keep every label above but run
# pytest, and an image that runs something else is not the one scanned.
[ "$entrypoint" = '["/usr/bin/tini","--"]' ] \
  || fail "the entrypoint is $entrypoint, not [\"/usr/bin/tini\",\"--\"]"
[ "$cmd" = '["/app/startup.sh"]' ] || fail "the command is $cmd, not [\"/app/startup.sh\"]"
# No secret and no AWS credential is ever baked into the image's environment
# (the names of the application's `make inspect`, in any case): the task
# definition passes secrets by ValueFrom, and the task role is the only
# credential. Only names are reported, never a value.
env_names="$(printf '%s\n' "$env_entries" | sed -n 's/^"\([^=]*\)=.*$/\1/p')"
secret_env="$(printf '%s\n' "$env_names" \
  | grep -iE '^AWS_|(^|_)(SECRET|PASSWORD|PASSWD|TOKEN|KEY|CREDENTIALS?|PRIVATE)(_|$)' | sort -u | tr '\n' ' ' | sed 's/ $//' || true)"
[ -z "$secret_env" ] || fail "the image sets $secret_env in its environment: secrets come from the task definition, never the image"
placeholder_env="$(printf '%s\n' "$env_entries" | grep -i change-me-in-production \
  | sed 's/^"\([^=]*\)=.*$/\1/' | sort -u | tr '\n' ' ' | sed 's/ $//' || true)"
[ -z "$placeholder_env" ] || fail "the environment variable(s) $placeholder_env hold 'change-me-in-production'"
if [ -n "$version" ] && [ "$image_version" != "$version" ]; then
  fail "the org.opencontainers.image.version label is '$image_version', not VERSION $version"
fi

# Run as root so that no file is hidden from the scan by its permissions.
# Every line the probe prints starts with its kind. Its $(...) expand in the
# image, not here. Env files and private keys are looked for in the whole
# image, and the walks cross mounts, so a volume or bind mount hides
# nothing; only the bot's lockfile-built node_modules is skipped by the name
# walk. CA bundles (certifi's cacert.pem) are public, so outside /app a .pem
# is reported only when it holds a private key: a line that begins a PEM
# private-key block, which libraries that merely name the marker never have.
# shellcheck disable=SC2016
probe='
pem='"$pem"'
echo "pem $(sha256sum "$pem" 2>/dev/null | cut -d" " -f1)"
cd /app && python3 -c "
from marketplace import aws_marketplace as m, aws_marketplace_verify as v
print(\"placeholder\", v.PUBKEY_SHA256_PLACEHOLDER)
print(\"python\", m.BAKED_PRODUCT_CODE, \"|\", *sorted(m.TRUSTED_PRODUCT_CODES), \"|\", *sorted(v.TRUSTED_PRODUCT_CODES))
" 2>&1 | sed "/^placeholder \|^python /!s/^/error python: /"
echo "node $(node -p "JSON.stringify([...require(process.argv[1]).TRUSTED_PRODUCT_CODES].sort())" /app/bot/entitlement.js 2>&1 | tr "\n" " ")"
for f in /usr/sbin/sshd /usr/bin/sshd /sbin/sshd; do if [ -e "$f" ]; then echo "sshd $f"; fi; done
if command -v sshd >/dev/null 2>&1; then echo "sshd $(command -v sshd)"; fi
grep -rli change-me-in-production /app | sed "s/^/placeholder-secret /"
for p in /opt/test-site /opt/local-test /app/tests /app/docker/local /app/docs /app/pytest.ini; do
  if [ -e "$p" ] || [ -L "$p" ]; then echo "test $p"; fi
done
skip="( -path /proc -o -path /sys -o -path /dev -o -path /app/bot/node_modules ) -prune -o"
find / $skip \( -name entitlement_stub.py -o -name fake_ecs_metadata.py -o -name live_view_hook.py \) -print 2>/dev/null \
  | sed "s/^/test /"
find / $skip -iname ".env*" -print 2>/dev/null | sed "s/^/file /"
find /app $skip -type f \( -iname "*.pem" -o -iname "*.key" \) -print 2>/dev/null | sed "s/^/file /"
find / \( -path /proc -o -path /sys -o -path /dev \) -prune -o -type f -print0 2>/dev/null \
  | xargs -0 -r grep -lIE "^-----BEGIN ([A-Z0-9]+ )*PRIVATE KEY-----" 2>/dev/null | sed "s/^/file /"
echo done
'
# --pull never: a missing or foreign-platform image must not be fetched.
if ! out="$(docker run --rm --network none --pull never --platform linux/amd64 --user 0:0 \
    -e PYTHONDONTWRITEBYTECODE=1 --entrypoint sh "$id" -c "$probe" 2>&1)" \
    || ! printf '%s\n' "$out" | grep -qx 'done'; then
  fail "the image could not be run offline to inspect its contents: $(printf '%s' "$out" | tail -n 3 | tr '\n' ' ')"
fi
field() { printf '%s\n' "$out" | sed -n "s/^$1 //p"; }
# The lines of field $1 (after the grep -v filter $2, when given), deduplicated
# and on one line.
listed() { field "$1" | { if [ -n "${2:-}" ]; then grep -vxF "$2" || true; else cat; fi; } \
  | sort -u | tr '\n' ' ' | sed 's/ $//'; }

pem_sha="$(field pem | head -n 1)"
placeholder="$(field placeholder | head -n 1)"

# The public key: the label must name the baked PEM, which must be the
# product's key, never the placeholder that every build accepts.
if [ -z "$pem_sha" ]; then
  fail "the image holds no $pem"
elif [ "$label_key" != "$pem_sha" ]; then
  fail "the com.nlsql.pubkey-sha256 label ($label_key) is not the sha256 of $pem ($pem_sha)"
fi
if ! [[ "$placeholder" =~ ^[0-9a-f]{64}$ ]]; then
  fail "the image's marketplace/aws_marketplace_verify.py defines no PUBKEY_SHA256_PLACEHOLDER"
elif [ "$pem_sha" = "$placeholder" ] || [ "$label_key" = "$placeholder" ]; then
  fail "the image bakes the PLACEHOLDER public key (PUBKEY_SHA256_PLACEHOLDER $placeholder), which no task can run with: bake RegisterUsage public key version 1 from the product entity first"
fi
if ! [[ "$pinned" =~ ^[0-9a-f]{64}$ ]]; then
  fail "PUBKEY_SHA256 in the Makefile is '$pinned', not a sha256: obtain RegisterUsage public key version 1 from the product entity first"
elif [ "$pem_sha" != "$pinned" ]; then
  fail "the image bakes public key $pem_sha, not PUBKEY_SHA256 $pinned"
fi

# The trusted product code, read by import from all three places that check it:
# the entitlement program, the verifier and the bot. REFUSED_PRODUCT_CODES names
# the other products on purpose, so this compares the trusted sets exactly.
python_codes="$(field python | head -n 1)"
node_codes="$(field node | sed 's/ $//' | head -n 1)"
[ "$python_codes" = "$code | $code | $code" ] \
  || fail "marketplace/aws_marketplace.py and aws_marketplace_verify.py bake and trust '$python_codes', not exactly $code"
[ "$node_codes" = "[\"$code\"]" ] \
  || fail "bot/entitlement.js trusts '$node_codes', not exactly $code"
errors="$(field 'error python:')"
[ -z "$errors" ] || fail "importing the marketplace modules failed: $(printf '%s' "$errors" | tail -n 1)"

sshd="$(listed sshd)"
[ -z "$sshd" ] || fail "the image holds an sshd: $sshd"
# Placeholder secrets are recognised by their sha256, so the word never ships.
secrets="$(listed placeholder-secret)"
[ -z "$secrets" ] || fail "'change-me-in-production' is in $secrets"
stray="$(listed file "$pem")"
[ -z "$stray" ] || fail "the image holds env, key or PEM files other than $pem: $stray"
test_files="$(listed test)"
[ -z "$test_files" ] || fail "the image holds the application's test-only files (a test, e2e or local image): $test_files"

if [ "${#failures[@]}" -gt 0 ]; then
  for f in "${failures[@]}"; do echo "ERROR: $image: $f" >&2; done
  echo "ERROR: $image refused (${#failures[@]} problem(s)); nothing was pushed" >&2
  exit 1
fi
if [ -n "$id_file" ]; then printf '%s\n' "$id" > "$id_file"; fi
echo "  $image ($id): stage=enforce, linux/amd64, tini /app/startup.sh, uid 10001, 8080/tcp only, no volumes, no secret ENV, no test files, ${version:+version $version, }product $code, public key $pem_sha"
