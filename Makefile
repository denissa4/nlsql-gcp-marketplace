# NLSQL AI Employee — AWS Marketplace (ECS container product) packaging.
#
# The image is built from the application repository (denissa4/ai-employee,
# branch aws-marketplace-offer, `make build` there); this repo holds the
# deployment template and listing texts, and publishes the image to the AWS
# Marketplace ECR repository.

# From the AWS Marketplace Management Portal, after "Add repositories".
# <sellerName>/<repositoryName>, both created by the portal and permanent.
ECR_REGISTRY ?= 709825985650.dkr.ecr.us-east-1.amazonaws.com
ECR_REPO     ?= nlsql/ai-employee
AWS_REGION   ?= us-east-1
VERSION      ?= 1.0.2

# The locally built enforce image: in the application checkout, run
# `make build IMAGE_LOCAL=ai-employee-aws:$(VERSION)` (its own default name is
# ai-employee:$(VERSION), which a stale ai-employee-aws image must not shadow).
LOCAL_IMAGE  ?= ai-employee-aws:$(VERSION)
# Where listing links point. Always a commit, never a branch: the repository's
# default branch is the Google Cloud packaging.
GITHUB_REPO  ?= denissa4/nlsql-gcp-marketplace
# push-test: a private repository in the seller account, never the Marketplace
# one, so a test never spends a Marketplace tag.
TEST_REPO    ?= nlsql-ai-employee-test
TEST_TAG     ?= $(VERSION)-rc1

# Seller-account test stack (scripts/deploy-test.sh, scripts/e2e-test.py,
# scripts/destroy-test.sh), deployed from TEST_TAG in the private repository
# above. GEMINI=on or off; empty means on when the env file has GEMINI_API_KEY.
# BROWSER_AGENT=Enabled (default) or Disabled sets the template's BrowserAgent.
# LANGUAGE_MODEL and GEMINI_MODEL override the env file's MODEL_NAME and
# GEMINI_MODEL. E2E_ARGS goes to scripts/e2e-test.py as is: --skip-schedule,
# --restart-probe, --ws-idle <seconds>, --keep-capture. A Teams sign-in link
# goes in LOGIN_URL='<url>', never in E2E_ARGS.
ENV_FILE       ?=
GEMINI         ?=
BROWSER_AGENT  ?=
STACK_NAME     ?= nlsql-ai-employee-test
LANGUAGE_MODEL ?=
GEMINI_MODEL   ?=
DISABLE_ROLLBACK ?=
PURGE_DATA     ?=
E2E_ARGS       ?=

# Checkout of the application on branch aws-marketplace-offer, to compare the
# baked product code and key, the model list and the env contract. Any
# checkout found there must be on that branch. The default is skipped when
# absent, except by `push`; an APP_REPO given on the command line must exist.
APP_REPO     ?= ../ai-employee-aws
APP_BRANCH   := aws-marketplace-offer

IMAGE       := $(ECR_REGISTRY)/$(ECR_REPO)
REGISTRY_ID := $(firstword $(subst ., ,$(ECR_REGISTRY)))
# What check-pushed inspects; push-test passes its own.
PUSHED_REF  ?= $(IMAGE):$(VERSION)
# The image ID push-guard accepted, which push and push-test tag (inside .git,
# so it is never committed or scanned).
GUARDED_ID  := $(or $(shell git rev-parse --git-path nlsql-push-guard-image-id 2>/dev/null),.push-guard-image-id)
TEMPLATE    := cloudformation/nlsql-ai-employee-ecs-fargate.yaml
GUIDE       := docs/aws-deployment-guide.md

LISTING_USAGE    := listing/usage-instructions.txt
LISTING_DELIVERY := listing/delivery-option-description.txt
LISTING_NOTES    := listing/release-notes/$(VERSION).md
# Every version's notes: a published version keeps its own
LISTING_ALL_NOTES := $(sort $(wildcard listing/release-notes/*.md))
LISTING_PRODUCT  := listing/product

# Trust anchors baked into the image. `push` refuses anything else; `override`
# keeps a command-line value from changing what the guards expect.
override PRODUCT_CODE  := f5jcvx1rl6wb66fhzy5qycpnr
# sha256 of RegisterUsage public key version 1, from the product entity
# (DescribeEntity: SignatureVerificationKeys, PublicKeyVersion 1), over the
# PEM exactly as the app bakes it (marketplace/aws_marketplace_pubkey_v1.pem,
# app commit B11). push-guard refuses any other key, the placeholder included.
override PUBKEY_SHA256 := 68f9c9b44a3bf144a3292718879681231ab20719386792c8aa2559c82f86d2c1

# cfn-lint, which also supplies template-invariants' YAML loader: from PATH
# when python3 can import it, else installed in PYTHON_IMAGE. With an image
# that already has this version, CFN_LINT_DOCKER_FLAGS=--network none runs it
# offline.
CFN_LINT_VERSION      := 1.57.2
PYTHON_IMAGE          ?= python:3.12.14-slim
CFN_LINT_DOCKER_FLAGS ?=

# --app-repo when the application checkout is there, or was asked for.
APP_ARG = $(if $(or $(wildcard $(APP_REPO)/tests/unit/test_bedrock_models.py),$(filter-out file,$(origin APP_REPO))),--app-repo $(APP_REPO))

# Runs the command line $(1) where cfn-lint is installed. In the container the
# repository is /src and APP_REPO is /app-repo, both read-only.
with-cfn-lint = if python3 -c 'import cfnlint' >/dev/null 2>&1; then $(1); \
	elif command -v docker >/dev/null 2>&1; then \
	  docker run --rm $(CFN_LINT_DOCKER_FLAGS) -e PYTHONDONTWRITEBYTECODE=1 -v "$(CURDIR)":/src:ro -w /src \
	    $(if $(APP_ARG),-v "$(abspath $(APP_REPO))":/app-repo:ro) $(PYTHON_IMAGE) \
	    sh -c 'pip install --quiet --disable-pip-version-check --root-user-action=ignore cfn-lint==$(CFN_LINT_VERSION) \
	      && $(subst $(APP_REPO),/app-repo,$(1))'; \
	else echo "ERROR: install cfn-lint $(CFN_LINT_VERSION), or docker" && exit 1; fi

.PHONY: help
help:
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

.PHONY: check-vars
check-vars: ## Template, listing, guide and application agree on the image, product code and messages
	@test "$(ECR_REPO)" != "ECR_REPO" \
	  || (echo "ERROR: set ECR_REPO to the repository created in the Marketplace portal" && exit 1)
	@# The template's ImageUri default is what buyers launch with, so it must
	@# name the tag this Makefile pushes.
	@grep -qF 'Default: $(IMAGE):$(VERSION)' $(TEMPLATE) \
	  || (echo "ERROR: ImageUri default in $(TEMPLATE) does not match $(IMAGE):$(VERSION)" && exit 1)
	@grep -qF 'AWS_MARKETPLACE_PRODUCT_CODE, Value: $(PRODUCT_CODE)}' $(TEMPLATE) \
	  || (echo "ERROR: AWS_MARKETPLACE_PRODUCT_CODE in $(TEMPLATE) is not $(PRODUCT_CODE)" && exit 1)
	@if [ -d "$(APP_REPO)" ] || [ "$(origin APP_REPO)" != file ] || [ "$(APP_REQUIRED)" = 1 ]; then \
	  branch=$$(git -C "$(APP_REPO)" rev-parse --abbrev-ref HEAD 2>/dev/null); \
	  test "$$branch" = $(APP_BRANCH) \
	    || { echo "ERROR: $(APP_REPO) is not on branch $(APP_BRANCH) ($${branch:-not a git checkout})"; exit 1; }; \
	  grep -qxF 'BAKED_PRODUCT_CODE = "$(PRODUCT_CODE)"' "$(APP_REPO)/marketplace/aws_marketplace.py" 2>/dev/null \
	    || { echo "ERROR: $(APP_REPO) does not bake product code $(PRODUCT_CODE) in marketplace/aws_marketplace.py"; exit 1; }; \
	  grep -qxF 'TRUSTED_PRODUCT_CODES = frozenset({"$(PRODUCT_CODE)"})' "$(APP_REPO)/marketplace/aws_marketplace_verify.py" 2>/dev/null \
	    || { echo "ERROR: $(APP_REPO) does not trust exactly $(PRODUCT_CODE) in marketplace/aws_marketplace_verify.py"; exit 1; }; \
	  grep -qxF "const TRUSTED_PRODUCT_CODES = new Set(['$(PRODUCT_CODE)']);" "$(APP_REPO)/bot/entitlement.js" 2>/dev/null \
	    || { echo "ERROR: $(APP_REPO) does not trust exactly $(PRODUCT_CODE) in bot/entitlement.js"; exit 1; }; \
	  key=$$(shasum -a 256 "$(APP_REPO)/marketplace/aws_marketplace_pubkey_v1.pem" 2>/dev/null \
	    || sha256sum "$(APP_REPO)/marketplace/aws_marketplace_pubkey_v1.pem"); key=$${key%% *}; \
	  placeholder=$$(sed -n 's/^PUBKEY_SHA256_PLACEHOLDER = "\([0-9a-f]\{64\}\)"$$/\1/p' "$(APP_REPO)/marketplace/aws_marketplace_verify.py"); \
	  test -n "$$placeholder" \
	    || { echo "ERROR: no PUBKEY_SHA256_PLACEHOLDER in $(APP_REPO)/marketplace/aws_marketplace_verify.py"; exit 1; }; \
	  if [ -n "$(PUBKEY_SHA256)" ]; then \
	    test "$$key" = "$(PUBKEY_SHA256)" \
	      || { echo "ERROR: $(APP_REPO) bakes public key $$key, not PUBKEY_SHA256 $(PUBKEY_SHA256)"; exit 1; }; \
	    test "$$key" != "$$placeholder" \
	      || { echo "ERROR: PUBKEY_SHA256 is the application's placeholder key"; exit 1; }; \
	    echo "  $(APP_REPO) ($(APP_BRANCH)) bakes the same product code and public key"; \
	  else \
	    echo "  $(APP_REPO) ($(APP_BRANCH)) bakes the same product code; PUBKEY_SHA256 is not set yet, so push refuses"; \
	  fi; \
	else \
	  echo "  (no application checkout at $(APP_REPO); baked product code not compared)"; \
	fi
	@test -f $(LISTING_NOTES) || (echo "ERROR: $(LISTING_NOTES) is missing" && exit 1)
	@grep -qF '$(IMAGE):$(VERSION)' $(LISTING_USAGE) \
	  || (echo "ERROR: $(LISTING_USAGE) does not name $(IMAGE):$(VERSION)" && exit 1)
	@head -n 1 $(LISTING_USAGE) | grep -qxF 'NLSQL AI Employee $(VERSION)' \
	  || (echo "ERROR: $(LISTING_USAGE) does not start with 'NLSQL AI Employee $(VERSION)'" && exit 1)
	@grep -qF '$(IMAGE):$(VERSION)' $(GUIDE) \
	  || (echo "ERROR: $(GUIDE) does not name $(IMAGE):$(VERSION)" && exit 1)
	@grep -qF 'Applies to NLSQL AI Employee **$(VERSION)**' $(GUIDE) \
	  || (echo "ERROR: $(GUIDE) does not say it applies to $(VERSION)" && exit 1)
	@# deploy-test.sh rewrites the ImageUri pattern by exact string, and
	@# defaults to this version.
	@pattern=$$(sed -n 's/^marketplace = r"\(.*\)"$$/\1/p' scripts/deploy-test.sh); \
	  test -n "$$pattern" && grep -qF "AllowedPattern: '^$$pattern\$$'" $(TEMPLATE) \
	  || (echo "ERROR: the ImageUri pattern in scripts/deploy-test.sh is not the one in $(TEMPLATE)" && exit 1)
	@grep -qF 'VERSION="$${VERSION:-$(VERSION)}"' scripts/deploy-test.sh \
	  || (echo "ERROR: VERSION's default in scripts/deploy-test.sh is not $(VERSION)" && exit 1)
	@$(MAKE) --no-print-directory guide-check
	@echo "  template, listing and guide agree on $(IMAGE):$(VERSION), product $(PRODUCT_CODE)"

.PHONY: guide-check
guide-check: ## Offline: the guide's Regions, models, parameters, outputs and links; with the app, its quoted messages
	@$(if $(APP_ARG),test -d "$(APP_REPO)" || { echo "ERROR: no application checkout at $(APP_REPO)"; exit 1; };,echo \
	    "  (no application checkout at $(APP_REPO); quoted messages not compared)";) \
	  python3 scripts/guide-check.py --guide $(GUIDE) --template $(TEMPLATE) $(APP_ARG)

.PHONY: template-invariants
template-invariants: ## Offline: env names, path allowlist, key schema, Regions, size, ENCRYPTION_KEY pin, models
	@$(if $(APP_ARG),test -d "$(APP_REPO)" || { echo "ERROR: no application checkout at $(APP_REPO)"; exit 1; };,echo \
	    "  (no application checkout at $(APP_REPO); model list and env contract not compared)";) \
	  $(call with-cfn-lint,python3 scripts/template-invariants.py --template $(TEMPLATE) $(APP_ARG))

.PHONY: test-scripts
test-scripts: ## Offline unit tests: template invariants, guide and listing checks (each fails on a planted defect), teams/build-package.sh, push-guard, the test-stack scripts, e2e-test.py
	@$(call with-cfn-lint,$(if $(APP_ARG),APP_REPO=$(APP_REPO) )python3 -m unittest discover -s scripts -p "test_*.py")

.PHONY: validate
validate: template-invariants ## Validate the template with CloudFormation (needs AWS credentials) and cfn-lint
	aws cloudformation validate-template \
	  --template-body file://$(TEMPLATE) --region $(AWS_REGION) >/dev/null
	@echo "  CloudFormation accepts the template"
	@$(call with-cfn-lint,cfn-lint $(TEMPLATE))
	@echo "  cfn-lint passed"

.PHONY: push-guard
push-guard: ## Offline: refuse $(LOCAL_IMAGE) unless it is the enforce build (tini, startup.sh, no test files, no secret ENV) with the product's key and code, uid 10001, 8080 only, no VOLUME, version $(VERSION)
	@# Only the enforce stage with the product's own key may ever reach buyers;
	@# the local-test image signs its own entitlement, and the test and e2e
	@# images carry pytest and the stubs: none of them may ever be pushed.
	@PUSH_GUARD_ID_FILE="$(GUARDED_ID)" scripts/push-guard.sh "$(LOCAL_IMAGE)" "$(PRODUCT_CODE)" "$(PUBKEY_SHA256)" "$(VERSION)"

.PHONY: check-tags
check-tags: ## Fail if $(VERSION) already exists in the Marketplace repository
	@# A tag submitted in a version cannot be replaced, so never overwrite one.
	@out=$$(aws ecr describe-images --registry-id $(REGISTRY_ID) --repository-name $(ECR_REPO) \
	    --image-ids imageTag=$(VERSION) --region $(AWS_REGION) --output text 2>&1); rc=$$?; \
	  if [ $$rc -eq 0 ]; then echo "ERROR: $(IMAGE):$(VERSION) already exists; bump VERSION" && exit 1; fi; \
	  case "$$out" in *ImageNotFoundException*) echo "  $(VERSION) is not yet in $(ECR_REPO)";; \
	    *) echo "ERROR: could not check $(ECR_REPO): $$out" && exit 1;; esac

.PHONY: login
login: ## Authenticate docker against the Marketplace ECR registry
	aws ecr get-login-password --region $(AWS_REGION) \
	  | docker login --username AWS --password-stdin $(ECR_REGISTRY)

.PHONY: push
# A release compares the image's anchors with the application checkout too.
push: APP_REQUIRED := 1
push: check-vars template-invariants push-guard ## Guard, tag and push $(LOCAL_IMAGE) to Marketplace ECR (cannot be undone)
	@$(MAKE) --no-print-directory check-tags
	@$(MAKE) --no-print-directory login
	@# Tag the image ID the guard accepted, not the name, which a rebuild may
	@# have moved since.
	@# One chained line, so that a failed tag never pushes an older local tag,
	@# not even under `make -i`.
	id=$$(cat "$(GUARDED_ID)") && test -n "$$id" && docker tag "$$id" $(IMAGE):$(VERSION) \
	  && docker push $(IMAGE):$(VERSION)
	@$(MAKE) --no-print-directory check-pushed
	@# An accepted ID is good for one push only; the next needs a fresh guard.
	@rm -f "$(GUARDED_ID)"

.PHONY: push-test
push-test: push-guard ## Guard, tag and push $(LOCAL_IMAGE) to the seller's private $(TEST_REPO):$(TEST_TAG)
	@# The same image guards as push, minus the template default: a test image
	@# never goes to the Marketplace repository.
	@account=$$(aws sts get-caller-identity --query Account --output text) || exit 1; \
	  registry=$$account.dkr.ecr.$(AWS_REGION).amazonaws.com; \
	  if ! aws ecr describe-repositories --repository-names $(TEST_REPO) --region $(AWS_REGION) >/dev/null 2>&1; then \
	    aws ecr create-repository --repository-name $(TEST_REPO) --region $(AWS_REGION) \
	      --image-tag-mutability IMMUTABLE --tags Key=nlsql:purpose,Value=test >/dev/null || exit 1; \
	    echo "  created the private repository $(TEST_REPO)"; \
	  fi; \
	  aws ecr get-login-password --region $(AWS_REGION) \
	    | docker login --username AWS --password-stdin $$registry || exit 1; \
	  id=$$(cat "$(GUARDED_ID)") && test -n "$$id" \
	    && docker tag "$$id" $$registry/$(TEST_REPO):$(TEST_TAG) \
	    && docker push $$registry/$(TEST_REPO):$(TEST_TAG) \
	    && $(MAKE) --no-print-directory check-pushed PUSHED_REF=$$registry/$(TEST_REPO):$(TEST_TAG) \
	    && rm -f "$(GUARDED_ID)"

.PHONY: check-pushed
check-pushed: ## Fail unless the pushed $(VERSION) (or PUSHED_REF=<ref>) is one image manifest, not an index
	@# AWS refuses an image index (several platforms, or build attestations).
	@raw=$$(docker buildx imagetools inspect --raw $(PUSHED_REF)) || exit 1; \
	  if echo "$$raw" | grep -Eq '"manifests"|manifest\.list|image\.index'; then \
	    echo "ERROR: $(PUSHED_REF) is an index (several platforms or attestations), not a single manifest"; exit 1; \
	  fi; \
	  echo "$$raw" | grep -Eq '"mediaType": *"application/vnd\.(docker\.distribution\.manifest\.v2|oci\.image\.manifest\.v1)\+json"' \
	    || { echo "ERROR: $(PUSHED_REF) is not a Docker v2 or OCI image manifest"; exit 1; }; \
	  echo "  $(PUSHED_REF): single image manifest, no index, no attestations"

.PHONY: digest
digest: ## Print the digest of the pushed $(VERSION)
	@aws ecr describe-images --registry-id $(REGISTRY_ID) --repository-name $(ECR_REPO) \
	  --image-ids imageTag=$(VERSION) --region $(AWS_REGION) \
	  --query 'imageDetails[0].imageDigest' --output text

.PHONY: template-url
template-url: ## Print the commit-pinned template and guide URLs for the listing
	@test -z "$$(git status --porcelain -- $(TEMPLATE) docs listing teams)" \
	  || (echo "ERROR: commit the template, docs, listing texts and teams/ first" && exit 1)
	@commit=$$(git rev-parse HEAD); \
	  test -n "$$(git branch -r --contains $$commit 2>/dev/null)" \
	    || (echo "ERROR: $$commit is not on any pushed branch; push it first" && exit 1); \
	  url="https://raw.githubusercontent.com/$(GITHUB_REPO)/$$commit/$(TEMPLATE)"; \
	  test $${#url} -lt 256 || (echo "ERROR: template URL is $${#url} characters (limit 255)" && exit 1); \
	  echo "Deployment template: $$url"; \
	  echo "Deployment guide:    https://github.com/$(GITHUB_REPO)/blob/$$commit/$(GUIDE)"; \
	  echo "Replace <commit> in $(LISTING_USAGE) with $$commit before pasting it."

.PHONY: listing-check
listing-check: ## Enforce Marketplace length and character limits on listing texts, and their Regions
	@python3 scripts/listing-check.py \
	  $(LISTING_USAGE):4000 $(LISTING_DELIVERY):999 $(addsuffix :29999,$(LISTING_ALL_NOTES)) \
	  $(LISTING_PRODUCT)/title.txt:72 $(LISTING_PRODUCT)/short-description.txt:1000 \
	  $(LISTING_PRODUCT)/long-description.txt:5000 $(LISTING_PRODUCT)/support-description.txt:2000 \
	  $(LISTING_PRODUCT)/highlight-1.txt:250 $(LISTING_PRODUCT)/highlight-2.txt:250 \
	  $(LISTING_PRODUCT)/highlight-3.txt:250 $(LISTING_PRODUCT)/keywords.txt:keywords \
	  $(LISTING_USAGE):regions=$(TEMPLATE)

# Credential-shaped strings, and AWS account IDs other than the Marketplace
# registry's and documentation placeholders, must never reach this public
# repository: not in the working tree, and not anywhere in the branch's history
# (`git log -p`), which stays public after a file is fixed. Every match is
# judged on its own, so an allowed value never hides another on the same line.
# The names may hold their value directly (NAME=v, "NAME": "v", NAME: v) or
# through a value key, the way ECS, CloudFormation and the CLI pair them:
# {Name: NAME, Value: v}, {"name": "NAME", "value": "v"}, name=NAME,value=v,
# ParameterKey=NAME,ParameterValue=v, in either order, and the two-line YAML
# and JSON block forms (SECRET_PAIR, matched on each line joined to the next).
# ValueFrom: is a reference, not a value, so it never matches.
# Placeholders (<...>, ${...}, $VAR, {{resolve:...}}) cannot match; SAMPLE_VALUES
# are the dummy values the published history already carries, and code that
# names a secret without holding one. The names include the application's own
# secrets (DL_SECRET_<tool> and DIRECT_LINE_SECRET among them); the shapes
# include Google API keys and Direct Line secrets (two 84-character base64url
# segments joined by a dot), which common scanners miss.
SECRET_NAMES  := (ApiToken|AppPassword|DbPassword|clientSecret|client_secret|LanguageModelApiKey|EmbeddingModelApiKey|GraphModelApiKey|HF_TOKEN|HUGGING_FACE_HUB_TOKEN|SasToken|AzureStorageSasToken|AwsSecretAccessKey|aws_secret_access_key|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|private_key|MicrosoftAppPassword|DIRECT_LINE_SECRET|DL_SECRET_[A-Za-z0-9_]+|ENCRYPTION_KEY|INTERNAL_AUTH_SECRET|VNC_SECRET_KEY|GEMINI_API_KEY|GeminiApiKey|MODEL_API_KEY|GRAPH_API_SECRET|NLSQL_API_KEY|USER_STORAGE_SAS_TOKEN)
SECRET_CHARS  := [A-Za-z0-9+/!@%^&*_~.-]{8,}
VALUE_KEY     := ["'\'']?([Vv]alue|ParameterValue)["'\'']?[[:blank:]]*[:=][[:blank:]]*["'\'']?
NAME_KEY      := ["'\'']?([Nn]ame|ParameterKey)["'\'']?[[:blank:]]*[:=][[:blank:]]*["'\'']?
SECRET_VALUE  := $(SECRET_NAMES)(["'\'':= ]+|["'\'']?,[[:blank:]]*$(VALUE_KEY))$(SECRET_CHARS)|$(VALUE_KEY)$(SECRET_CHARS)["'\'']?,[[:blank:]]*$(NAME_KEY)$(SECRET_NAMES)
# Over "<line> ::: <next line>": a name line then its value line, or the reverse.
LINE_START    := ::: [-+ ]?[[:blank:]]*(-[[:blank:]]*)?
SECRET_PAIR   := $(NAME_KEY)$(SECRET_NAMES)["'\'']?,?[[:blank:]]*$(LINE_START)$(VALUE_KEY)$(SECRET_CHARS)|$(VALUE_KEY)$(SECRET_CHARS)["'\'']?,?[[:blank:]]*$(LINE_START)$(NAME_KEY)$(SECRET_NAMES)["'\'']?,?[[:blank:]]*$$
SECRET_SHAPES := AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|[A-Za-z0-9_~.-]{3}[0-9]Q~[A-Za-z0-9_~.-]{31,34}|hf_[A-Za-z0-9]{30,}|gh[pousr]_[A-Za-z0-9]{36}|xox[abprs]-[A-Za-z0-9-]{10,}|sv=20[0-9]{2}-[0-9]{2}-[0-9]{2}&.*sig=|AccountKey=[A-Za-z0-9+/=]{40,}|AIza[0-9A-Za-z_-]{35}|[A-Za-z0-9_-]{84}\.[A-Za-z0-9_-]{84}
SAMPLE_VALUES := "(YOUR_[A-Z_]+|verify-not-a-real-[a-z]+)$$|:client_secret": client_secret$$|:GeminiApiKey": GEMINI_CONDITION$$
ALLOWED_IDS   := 709825985650|000000000000|111122223333

.PHONY: no-secrets
no-secrets: ## Fail if a credential or account ID is committed, now or in history (this repo is public)
	@files=$$(git ls-files -co --exclude-standard | grep -vE '\.(png|jpg|ico)$$' \
	    | while read -r f; do if [ -f "$$f" ]; then echo "$$f"; fi; done); \
	  scan() { grep -onIE "$$1" $$files; \
	    git log -p --no-color --no-ext-diff | grep -aonE "$$1" | sed 's/^/history:/'; }; \
	  pairs() { awk 'FNR == 1 { p = "" } { print FILENAME ":" FNR ":" p " ::: " $$0; p = $$0 }' $$files; \
	    git log -p --no-color --no-ext-diff \
	      | awk '{ print "history:" NR ":" p " ::: " $$0; p = $$0 }'; }; \
	  hits=$$(scan '$(SECRET_VALUE)' | grep -vE '$(SAMPLE_VALUES)'); \
	  blocks=$$(pairs | grep -aE '$(SECRET_PAIR)'); \
	  shapes=$$(scan '$(SECRET_SHAPES)'); \
	  accounts=$$(scan '[0-9]{12,}' | grep -E ':[0-9]{12}$$' | grep -vE ':($(ALLOWED_IDS))$$'); \
	  if [ -n "$$hits$$blocks$$shapes$$accounts" ]; then \
	    printf '%s\n' "$$hits" "$$blocks" "$$shapes" "$$accounts" | sed '/^$$/d' | cut -c1-160; \
	    echo "ERROR: possible credential or account ID committed (history: find it with git log -S'<value>')" \
	      && exit 1; fi; \
	  for f in .env .env.local service-account-key.yaml; do \
	    if git ls-files --error-unmatch $$f >/dev/null 2>&1; then echo "ERROR: $$f is tracked" && exit 1; fi; \
	  done; \
	  echo "  no credential-shaped strings or account IDs found"

.PHONY: deploy-test
deploy-test: ## Seller test stack (us-east-1) from a local env file: ENV_FILE=<path> [GEMINI=on|off] [BROWSER_AGENT=Enabled|Disabled] [LANGUAGE_MODEL=] [GEMINI_MODEL=] [DISABLE_ROLLBACK=1]
	@test -n "$(ENV_FILE)" || (echo "ERROR: ENV_FILE=<path to local env file> is required" && exit 1)
	VERSION=$(VERSION) TEST_TAG=$(TEST_TAG) AWS_REGION=$(AWS_REGION) STACK_NAME="$(STACK_NAME)" GEMINI="$(GEMINI)" BROWSER_AGENT="$(BROWSER_AGENT)" \
	  LANGUAGE_MODEL="$(LANGUAGE_MODEL)" GEMINI_MODEL="$(GEMINI_MODEL)" DISABLE_ROLLBACK="$(DISABLE_ROLLBACK)" \
	  scripts/deploy-test.sh "$(ENV_FILE)"

.PHONY: e2e-test
# The Teams sign-in link reaches the script through the environment only: its
# '&'s would cut a shell line, and make would echo its signed state
e2e-test: export LOGIN_URL := $(value LOGIN_URL)
e2e-test: ## Drive the test stack like Teams does: ENV_FILE=<path> [E2E_ARGS="--skip-schedule --restart-probe --ws-idle 120"] [LOGIN_URL='<url>']
	@test -n "$(ENV_FILE)" || (echo "ERROR: ENV_FILE=<path to local env file> is required" && exit 1)
	python3 scripts/e2e-test.py "$(ENV_FILE)" --stack "$(STACK_NAME)" --region $(AWS_REGION) $(E2E_ARGS)

.PHONY: destroy-test
destroy-test: ## Delete the seller test stack, its secret and the e2e capture function; PURGE_DATA=1 also deletes its kept tables and buckets
	-python3 scripts/e2e-test.py --teardown --stack "$(STACK_NAME)" --region $(AWS_REGION)
	AWS_REGION=$(AWS_REGION) STACK_NAME="$(STACK_NAME)" PURGE_DATA="$(PURGE_DATA)" scripts/destroy-test.sh
