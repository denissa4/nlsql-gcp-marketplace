# NLSQL — Google Cloud Marketplace packaging.
#
# Prerequisites: docker, gcloud, helm. `make tools` installs mpdev.
#
# Set this before any target that touches the registry:
#   export SERVICE_NAME=<your Marketplace service name, from Producer Portal Overview>
#
# VERSION defaults to the current release and TRACK is derived from it, so the
# two cannot drift. Override VERSION alone to cut a new release:
#   make deployer-image VERSION=1.9.0

SERVICE_NAME ?= SERVICE_NAME

# Marketplace reads this from the image MANIFEST, not the image config. A
# Dockerfile LABEL only reaches the config, which is why `annotate` below uses
# crane. Required on every image since 2025-01-20. See
# https://docs.cloud.google.com/marketplace/docs/partners/migrations/container-image-annotations
ANNOTATION = com.googleapis.cloudmarketplace.product.service.name=services/$(SERVICE_NAME)

# Google requires every image to carry BOTH tags:
#   "All of your app's images must be tagged with the release track and the current
#    version. For example, if you're releasing version 2.0.5 on the 2.0 release
#    track, all the images must be tagged with 2.0 and 2.0.5."
# https://docs.cloud.google.com/marketplace/docs/partners/kubernetes/create-app-package
VERSION ?= 1.8.0
# Release track = the MAJOR.MINOR prefix of VERSION, derived so the two cannot drift.
TRACK   := $(basename $(VERSION))

# The Marketplace registry for this listing. This is an Artifact Registry
# repository: project `nlsql-public`, repository `nlsql`. It is the "app folder"
# in Marketplace terms, fixed by Producer Portal showing the deployer at
# $(REGISTRY)/deployer.
AR_PROJECT := nlsql-public
REGISTRY  := us-docker.pkg.dev/$(AR_PROJECT)/nlsql

# The app is a CHILD image of that repository, not the repository root: Artifact
# Registry serves no image at a repository root, only child images.
APP_IMAGE := $(REGISTRY)/nlsql
DEPLOYER  := $(REGISTRY)/deployer

# The Cloud Marketplace metering agent (ubbagent), which the chart runs as a
# sidecar for usage reporting. Marketplace resolves every image in the listing
# against $(REGISTRY), so the agent is published here rather than pulled from
# Google at deploy time - and, like every other image, needs both tags and the
# annotation in its manifest.
#
# It is REBUILT from upstream source (ubbagent/Dockerfile), not copied. Google's
# published image is built on go1.26.5 and on an Alpine predating openssl
# 3.5.8-r0; Marketplace scans OUR copy, so those CVEs are ours to clear, and
# there is nothing newer to copy - `latest` and `0.2.13` are the same digest.
UBB_IMAGE  := $(REGISTRY)/ubbagent
# Google's own build of the same release tag. Never shipped; kept as the
# reference to diff the rebuild against - see `make ubbagent-compare`.
UBB_SOURCE ?= gcr.io/cloud-marketplace-tools/metering/ubbagent:0.2.13

# Every image this listing publishes. Anything added here is promoted, annotated
# and checked by the targets below.
ALL_IMAGES := $(APP_IMAGE) $(DEPLOYER) $(UBB_IMAGE)

# The image built by the NLSQL application repository, before it is promoted into
# the Marketplace registry with the required annotation and tags.
#
# Defaults to :latest because that is the ONLY tag currently published on
# .../nlsql/nlsql - there is no 1.2 or 1.2.0 tag on the app image yet. Marketplace
# requires both, which is exactly what `promote-image` creates from this source.
# Override to promote a specific digest instead:
#   make promote-image SOURCE_IMAGE=$(APP_IMAGE)@sha256:...
SOURCE_IMAGE ?= $(APP_IMAGE):latest

# Prefer the mpdev that `make tools` dropped in ./bin, fall back to one on PATH.
MPDEV := $(shell test -x ./bin/mpdev && echo ./bin/mpdev || echo mpdev)

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: check-vars
check-vars:
	@test "$(SERVICE_NAME)" != "SERVICE_NAME" \
	  || (echo "ERROR: set SERVICE_NAME (Producer Portal > Overview)" && exit 1)

.PHONY: tools
tools: check-docker-auth ## Install mpdev into ./bin
	mkdir -p bin
	docker run --rm gcr.io/cloud-marketplace-tools/k8s/dev cat /scripts/dev > bin/mpdev
	chmod +x bin/mpdev
	@echo "installed bin/mpdev — add ./bin to PATH"

.PHONY: configure
configure: ## Substitute the Marketplace placeholders with your real values
	@test -n "$(PARTNER_ID)" || (echo "ERROR: set PARTNER_ID (from Producer Portal)" && exit 1)
	@test -n "$(LISTING_URL)" || (echo "ERROR: set LISTING_URL (the published listing page)" && exit 1)
	@# Only the literal placeholders are rewritten. perl -i is portable across
	@# macOS and GNU userland.
	grep -rl --include='*.yaml' --include='*.md' 'LISTING_URL' . \
	  | xargs perl -pi -e 's{LISTING_URL}{$(LISTING_URL)}g'
	grep -rl --include='*.yaml' '"PARTNER_ID"' . \
	  | xargs perl -pi -e 's{"PARTNER_ID"}{"$(PARTNER_ID)"}g'
	@echo "Placeholders substituted. Review the diff, then run: make lint schema-check"

.PHONY: lint
lint: ## Lint and render the chart with representative values
	helm lint chart/nlsql \
	  --set nlsql.StaticEndPoint=https://nlsql.example.com/ \
	  --set database.DataSource=db.example.com \
	  --set database.DbName=analytics \
	  --set database.DbUser=nlsql_ro \
	  --set credentials.ApiToken=dummy \
	  --set credentials.DbPassword=dummy

.PHONY: no-secrets
no-secrets: ## Fail if any credential-shaped string is committed
	@! grep -rniE '(ApiToken|AppPassword|DbPassword)[":= ]+[A-Za-z0-9!@#$$%^&*_-]{8,}' \
	    --include='*.yaml' --include='*.yml' --include='*.md' . \
	    | grep -viE '(dummy|example|REPLACE|<|\$$|required|type:|title:|description:|MASKED)' \
	  || (echo "ERROR: possible credential committed" && exit 1)
	@echo "no credential-shaped strings found"

# Some macOS python3 builds ship without PyYAML; pick one that has it.
PYTHON := $(shell for p in python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do \
	  command -v $$p >/dev/null 2>&1 && $$p -c 'import yaml' >/dev/null 2>&1 && echo $$p && break; done)

.PHONY: schema-lint
schema-lint: ## Validate schema.yaml against Marketplace v2 rules (offline, no Docker)
	@test -n "$(PYTHON)" || (echo "ERROR: no python3 with PyYAML found. Run: pip3 install pyyaml" && exit 1)
	@$(PYTHON) scripts/validate-schema.py schema.yaml chart/nlsql/values.yaml chart/nlsql/Chart.yaml \
	  --overlay apptest/deployer/schema.yaml
	@# apptest/deployer/schema.yaml is an OVERLAY merged into the main schema at
	@# verify time, not a standalone schema, so it is only checked for valid YAML.
	@$(PYTHON) -c "import yaml,sys; d=yaml.safe_load(open('apptest/deployer/schema.yaml')); \
	  sys.exit('apptest schema has no properties block') if not d.get('properties') else \
	  print('validating apptest/deployer/schema.yaml (overlay)\n  %d added propert(y|ies)\n  OK' % len(d['properties']))"

.PHONY: check-docker-auth
check-docker-auth:
	@docker pull --quiet gcr.io/cloud-marketplace-tools/k8s/deployer_helm/onbuild >/dev/null 2>&1 \
	  || (echo "ERROR: cannot pull Google's deployer base image."; \
	      echo "  Google migrated cloud-marketplace-tools to Artifact Registry and"; \
	      echo "  anonymous pulls are denied. Authenticate first:"; \
	      echo "    gcloud auth login"; \
	      echo "    gcloud auth configure-docker gcr.io"; \
	      exit 1)

.PHONY: schema-check
schema-check: check-docker-auth ## Prove Producer Portal can extract the schema (needs Docker + gcloud auth)
	docker build --quiet --platform linux/amd64 --tag nlsql-deployer:local --file deployer/Dockerfile . >/dev/null
	@echo "--- /data/schema.yaml is byte-identical to source schema.yaml ---"
	@docker run --rm --entrypoint cat nlsql-deployer:local /data/schema.yaml \
	  | diff - schema.yaml && echo "identical"
	@echo "--- /data and /data-test contents ---"
	@docker run --rm --entrypoint find nlsql-deployer:local /data /data-test -maxdepth 2
	@echo "--- schema parses inside the deployer image ---"
	@docker run --rm --entrypoint python nlsql-deployer:local -c \
	  "import yaml,sys; \
	   d=yaml.safe_load(open('/data/schema.yaml')); \
	   x=d['x-google-marketplace']; \
	   print('OK: schemaVersion=%s publishedVersion=%s properties=%d images=%s' % \
	     (x['schemaVersion'], x['publishedVersion'], len(d['properties']), list(x['images'])))"

.PHONY: promote-image
promote-image: check-vars ## Promote the app image and push both tags (run `annotate` after)
	docker pull $(SOURCE_IMAGE)
	printf 'FROM $(SOURCE_IMAGE)\nLABEL com.googleapis.cloudmarketplace.product.service.name="services/$(SERVICE_NAME)"\n' \
	  | docker build --platform linux/amd64 --tag $(APP_IMAGE):$(VERSION) --tag $(APP_IMAGE):$(TRACK) -
	docker push $(APP_IMAGE):$(VERSION)
	@# Annotate immediately: a pushed tag must never be left without it, or the
	@# portal can pin a digest that fails verification.
	crane mutate $(APP_IMAGE):$(VERSION) --annotation "$(ANNOTATION)" -t $(APP_IMAGE):$(VERSION)
	crane tag $(APP_IMAGE):$(VERSION) $(TRACK)

.PHONY: ubbagent-image
ubbagent-image: check-vars ## Rebuild the metering agent from upstream source and push both tags
	@# On Cloud Build for the same reason as the deployer: it compiles ubbagent
	@# from upstream source with a patched Go toolchain, and Marketplace only
	@# accepts linux/amd64.
	gcloud builds submit --project=$(AR_PROJECT) --config=cloudbuild.yaml \
	  --substitutions=_IMAGE=$(UBB_IMAGE):$(VERSION),_DOCKERFILE=ubbagent/Dockerfile .
	crane mutate $(UBB_IMAGE):$(VERSION) --annotation "$(ANNOTATION)" -t $(UBB_IMAGE):$(VERSION)
	crane tag $(UBB_IMAGE):$(VERSION) $(TRACK)

.PHONY: ubbagent-compare
ubbagent-compare: ## Diff the rebuilt agent against Google's published image
	@# Equivalence check: the flag set and entrypoint must match Google's build
	@# exactly, and only the Go toolchain version should differ - that difference
	@# IS the fix.
	@set -e; for img in $(UBB_SOURCE) $(UBB_IMAGE):$(VERSION); do \
	  echo "--- $$img ---"; \
	  docker run --rm --platform linux/amd64 --entrypoint /usr/local/bin/ubbagent \
	    $$img --help 2>&1 | grep -oE '^ +-[a-z_]+' | sort | tr -d ' ' | tr '\n' ' '; echo; \
	  crane config $$img | $(PYTHON) -c \
	    "import json,sys; c=json.load(sys.stdin)['config']; \
	     print('  cmd=%s user=%r' % (c.get('Cmd'), c.get('User')))"; \
	done

.PHONY: deployer-image
deployer-image: check-vars ## Build the deployer and push both tags (run `annotate` after)
	@# The LABEL below is not sufficient on its own - it lands in the image config,
	@# while Marketplace reads the manifest. `make annotate` does the real work.
	@# Built on Cloud Build: the image compiles helm and Kubernetes from source
	@# with a patched Go toolchain, which is impractical to emulate locally.
	gcloud builds submit --project=$(AR_PROJECT) --config=cloudbuild.yaml \
	  --substitutions=_IMAGE=$(DEPLOYER):$(VERSION),_DOCKERFILE=deployer/Dockerfile .
	crane mutate $(DEPLOYER):$(VERSION) --annotation "$(ANNOTATION)" -t $(DEPLOYER):$(VERSION)
	crane tag $(DEPLOYER):$(VERSION) $(TRACK)

.PHONY: annotate
annotate: check-vars ## Write the Marketplace annotation into every image manifest
	@# crane rewrites the manifest in place without re-uploading layers. This
	@# changes the digest, so the track tag is re-pointed at the new one and the
	@# release must be re-selected in Producer Portal afterwards.
	@set -e; for img in $(ALL_IMAGES); do \
	  crane mutate $$img:$(VERSION) --annotation "$(ANNOTATION)" -t $$img:$(VERSION); \
	  crane tag $$img:$(VERSION) $(TRACK); \
	done

.PHONY: check-annotations
check-annotations: ## Fail unless every published tag carries the annotation in its manifest
	@set -e; for img in $(ALL_IMAGES); do \
	  for tag in $(VERSION) $(TRACK); do \
	    if crane manifest $$img:$$tag 2>/dev/null \
	         | tr -d ' ' | grep -q '"com.googleapis.cloudmarketplace.product.service.name":"services/$(SERVICE_NAME)"'; then \
	      echo "  ok       $$img:$$tag"; \
	    else \
	      echo "  BAD      $$img:$$tag  (annotation absent, tag missing, or a DIFFERENT service name)"; \
	      crane manifest $$img:$$tag 2>/dev/null | tr -d ' ' \
	        | grep -o '"com.googleapis.cloudmarketplace.product.service.name":"[^"]*"' \
	        | sed 's/^/           found: /' || true; \
	      exit 1; \
	    fi; \
	  done; \
	done

.PHONY: scan
scan: ## Fail if any published image has a fixable CRITICAL/HIGH vulnerability
	@# Reads Artifact Analysis, which is already enabled on $(AR_PROJECT) and is
	@# the same scanner Cloud Marketplace reports against - so this is the mail
	@# from Google's security team, a week early.
	@#
	@# Only findings with a fix available count. That is exactly the criterion in
	@# their notice ("update the affected packages"), and it is the only kind we
	@# can act on: an unfixed CVE has no version to move to.
	@#
	@# `discovery_summary` is what says whether a scan happened; a clean image
	@# has NO package_vulnerability_summary at all, so that field's absence must
	@# never be read as "not scanned yet" - it usually means the opposite.
	@set -e; rc=0; for img in $(ALL_IMAGES); do \
	  echo "--- $$img:$(VERSION) ---"; \
	  gcloud artifacts docker images describe $$img:$(VERSION) \
	    --show-all-metadata --format=json 2>/dev/null \
	  | $(PYTHON) -c "import json,sys; \
	      raw=sys.stdin.read().strip(); \
	      d=json.loads(raw) if raw else {}; \
	      disc=[o.get('discovery',{}) for o in (d.get('discovery_summary') or {}).get('discovery',[])]; \
	      st=[x.get('analysisStatus') for x in disc]; \
	      sys.exit(9) if not st else None; \
	      sys.exit(8) if not any(x=='FINISHED_SUCCESS' for x in st) else None; \
	      v=(d.get('package_vulnerability_summary') or {}).get('vulnerabilities') or {}; \
	      rows={(s,o.get('noteName','').split('/')[-1],p.get('affectedPackage'), \
	             (p.get('affectedVersion') or {}).get('fullName'), \
	             (p.get('fixedVersion') or {}).get('fullName')) \
	            for s,items in v.items() for o in items \
	            if (o.get('vulnerability') or {}).get('fixAvailable') \
	            for p in (o.get('vulnerability') or {}).get('packageIssue',[])}; \
	      [print('  %-8s %-18s %-26s %s -> %s' % r) for r in sorted(rows)]; \
	      bad=[r for r in rows if r[0] in ('CRITICAL','HIGH')]; \
	      print('  %d fixable (%d critical/high)' % (len(rows), len(bad))); \
	      sys.exit(1 if bad else 0)" \
	  || case $$? in \
	       9) echo "  NOT SCANNED - no discovery record. Tag not pushed, or Artifact"; \
	          echo "               Analysis has not picked the digest up yet."; rc=1; nodata=1;; \
	       8) echo "  SCAN DID NOT SUCCEED - see the discovery occurrence for this digest"; \
	          rc=1; nodata=1;; \
	       *) rc=1; found=1;; \
	     esac; \
	done; \
	test $$rc -eq 0 || { echo ""; \
	  test -z "$$found"  || echo "FAIL: fixable CRITICAL/HIGH findings remain - Marketplace will reject this"; \
	  test -z "$$nodata" || echo "FAIL: some images have no usable scan result - see above"; \
	  exit 1; }
	@echo "no fixable critical/high vulnerabilities in any published image"

.PHONY: check-tags
check-tags: ## List what is actually published, to confirm against Producer Portal
	@for img in $(ALL_IMAGES); do \
	  echo "--- $$img ---"; \
	  gcloud artifacts docker images list $$img --include-tags; \
	done

.PHONY: digest
digest: ## Print the immutable digest to quote in README.md
	@gcloud artifacts docker images describe $(APP_IMAGE):$(VERSION) \
	  --format='value(image_summary.digest)'

.PHONY: doctor
doctor: ## Check the local mpdev environment
	$(MPDEV) doctor

.PHONY: install
install: check-vars ## Install into the current kubectl context via mpdev
	kubectl create namespace nlsql-test --dry-run=client -o yaml | kubectl apply -f -
	$(MPDEV) install \
	  --deployer=$(DEPLOYER):$(VERSION) \
	  --parameters='{"name": "nlsql-1", "namespace": "nlsql-test"}'

# Values fed to `mpdev verify`. Verification runs unattended, so every required
# schema property that has no default must be supplied here - the harness only
# fills in name, namespace and a fake REPORTING_SECRET by itself.
#
# The database is deliberately unreachable: NLSQL's /health returns 200 without a
# live database, so verification exercises the packaging rather than needing real
# credentials in CI.
VERIFY_PARAMETERS ?= { \
  "nlsql.StaticEndPoint": "http://nlsql-verify.example.com/", \
  "credentials.ApiToken": "verify-not-a-real-token", \
  "database.DatabaseType": "MSSQL", \
  "database.DataSource": "nowhere.invalid", \
  "database.DbName": "verify", \
  "database.DbPort": "1433", \
  "database.DbUser": "verify", \
  "credentials.DbPassword": "verify-not-a-real-password" }

.PHONY: verify
verify: check-vars ## Run the full Marketplace verification (install, test, uninstall)
	$(MPDEV) verify --deployer=$(DEPLOYER):$(VERSION) \
	  --parameters='$(VERIFY_PARAMETERS)'

.PHONY: clean
clean: ## Remove a local test install
	-kubectl delete application nlsql-1 --namespace nlsql-test
	-kubectl delete namespace nlsql-test

.PHONY: all
all: lint no-secrets schema-lint schema-check promote-image ubbagent-image deployer-image scan verify ## Full release pipeline
