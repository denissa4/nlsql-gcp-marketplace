# NLSQL AI Employee — AWS Marketplace (ECS container product) packaging.
#
# The image is built from the application repository (denissa4/ai-employee,
# branch aws-marketplace-offer, `make build` there); this repo holds the
# deployment template, and publishes the image to the AWS Marketplace ECR
# repository.

# From the AWS Marketplace Management Portal, after "Add repositories".
# <sellerName>/<repositoryName>, both created by the portal and permanent.
ECR_REGISTRY ?= 709825985650.dkr.ecr.us-east-1.amazonaws.com
ECR_REPO     ?= nlsql/ai-employee
AWS_REGION   ?= us-east-1
VERSION      ?= 1.0.0

# Checkout of the application on branch aws-marketplace-offer, to compare the
# baked product code, the model list and the env contract. The default is
# skipped when absent; an APP_REPO given on the command line must exist.
APP_REPO     ?= ../ai-employee-aws
APP_BRANCH   := aws-marketplace-offer

IMAGE    := $(ECR_REGISTRY)/$(ECR_REPO)
TEMPLATE := cloudformation/nlsql-ai-employee-ecs-fargate.yaml

# Trust anchor baked into the image (marketplace/aws_marketplace.py).
PRODUCT_CODE := f5jcvx1rl6wb66fhzy5qycpnr

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
check-vars: ## Template and application agree on the image and product code
	@test "$(ECR_REPO)" != "ECR_REPO" \
	  || (echo "ERROR: set ECR_REPO to the repository created in the Marketplace portal" && exit 1)
	@# The template's ImageUri default is what buyers launch with, so it must
	@# name the tag this Makefile pushes.
	@grep -qF 'Default: $(IMAGE):$(VERSION)' $(TEMPLATE) \
	  || (echo "ERROR: ImageUri default in $(TEMPLATE) does not match $(IMAGE):$(VERSION)" && exit 1)
	@grep -qF 'AWS_MARKETPLACE_PRODUCT_CODE, Value: $(PRODUCT_CODE)}' $(TEMPLATE) \
	  || (echo "ERROR: AWS_MARKETPLACE_PRODUCT_CODE in $(TEMPLATE) is not $(PRODUCT_CODE)" && exit 1)
	@if [ -d "$(APP_REPO)" ] || [ "$(origin APP_REPO)" != file ]; then \
	  branch=$$(git -C "$(APP_REPO)" rev-parse --abbrev-ref HEAD 2>/dev/null); \
	  test "$$branch" = $(APP_BRANCH) \
	    || { echo "ERROR: $(APP_REPO) is not on branch $(APP_BRANCH) ($${branch:-not a git checkout})"; exit 1; }; \
	  grep -qxF 'BAKED_PRODUCT_CODE = "$(PRODUCT_CODE)"' "$(APP_REPO)/marketplace/aws_marketplace.py" 2>/dev/null \
	    || { echo "ERROR: $(APP_REPO) does not bake product code $(PRODUCT_CODE) in marketplace/aws_marketplace.py"; exit 1; }; \
	  echo "  $(APP_REPO) ($(APP_BRANCH)) bakes the same product code"; \
	else \
	  echo "  (no application checkout at $(APP_REPO); baked product code not compared)"; \
	fi
	@echo "  template agrees on $(IMAGE):$(VERSION), product $(PRODUCT_CODE)"

.PHONY: template-invariants
template-invariants: ## Offline: env names, path allowlist, key schema, Regions, size, ENCRYPTION_KEY pin, models
	@$(if $(APP_ARG),test -d "$(APP_REPO)" || { echo "ERROR: no application checkout at $(APP_REPO)"; exit 1; };,echo \
	    "  (no application checkout at $(APP_REPO); model list and env contract not compared)";) \
	  $(call with-cfn-lint,python3 scripts/template-invariants.py --template $(TEMPLATE) $(APP_ARG))

.PHONY: test-scripts
test-scripts: ## Offline unit tests: template invariants (each fails on a planted defect), teams/build-package.sh
	@$(call with-cfn-lint,$(if $(APP_ARG),APP_REPO=$(APP_REPO) )python3 -m unittest discover -s scripts -p "test_*.py")

.PHONY: validate
validate: template-invariants ## Validate the template with CloudFormation (needs AWS credentials) and cfn-lint
	aws cloudformation validate-template \
	  --template-body file://$(TEMPLATE) --region $(AWS_REGION) >/dev/null
	@echo "  CloudFormation accepts the template"
	@$(call with-cfn-lint,cfn-lint $(TEMPLATE))
	@echo "  cfn-lint passed"

# Credential-shaped strings, and AWS account IDs other than the Marketplace
# registry's and documentation placeholders, must never reach this public
# repository: not in the working tree, and not anywhere in the branch's history
# (`git log -p`), which stays public after a file is fixed. Every match is
# judged on its own, so an allowed value never hides another on the same line.
# Placeholders (<...>, ${...}, $VAR) cannot match SECRET_VALUE; SAMPLE_VALUES
# are the dummy values the published history already carries.
SECRET_VALUE  := (ApiToken|AppPassword|DbPassword|clientSecret|client_secret|LanguageModelApiKey|EmbeddingModelApiKey|GraphModelApiKey|HF_TOKEN|HUGGING_FACE_HUB_TOKEN|SasToken|AzureStorageSasToken|AwsSecretAccessKey|aws_secret_access_key|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|private_key|MicrosoftAppPassword|DIRECT_LINE_SECRET)["'\'':= ]+[A-Za-z0-9+/!@%^&*_~.-]{8,}
SECRET_SHAPES := AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|[A-Za-z0-9_~.-]{3}[0-9]Q~[A-Za-z0-9_~.-]{31,34}|hf_[A-Za-z0-9]{30,}|gh[pousr]_[A-Za-z0-9]{36}|xox[abprs]-[A-Za-z0-9-]{10,}|sv=20[0-9]{2}-[0-9]{2}-[0-9]{2}&.*sig=|AccountKey=[A-Za-z0-9+/=]{40,}
SAMPLE_VALUES := "(YOUR_[A-Z_]+|verify-not-a-real-[a-z]+)$$|:client_secret": client_secret$$
ALLOWED_IDS   := 709825985650|000000000000|111122223333

.PHONY: no-secrets
no-secrets: ## Fail if a credential or account ID is committed, now or in history (this repo is public)
	@files=$$(git ls-files -co --exclude-standard | grep -vE '\.(png|jpg|ico)$$' \
	    | while read -r f; do if [ -f "$$f" ]; then echo "$$f"; fi; done); \
	  scan() { grep -onIE "$$1" $$files; \
	    git log -p --no-color --no-ext-diff | grep -aonE "$$1" | sed 's/^/history:/'; }; \
	  hits=$$(scan '$(SECRET_VALUE)' | grep -vE '$(SAMPLE_VALUES)'); \
	  shapes=$$(scan '$(SECRET_SHAPES)'); \
	  accounts=$$(scan '[0-9]{12,}' | grep -E ':[0-9]{12}$$' | grep -vE ':($(ALLOWED_IDS))$$'); \
	  if [ -n "$$hits$$shapes$$accounts" ]; then \
	    printf '%s\n' "$$hits" "$$shapes" "$$accounts" | sed '/^$$/d' | cut -c1-160; \
	    echo "ERROR: possible credential or account ID committed (history: find it with git log -S'<value>')" \
	      && exit 1; fi; \
	  for f in .env .env.local service-account-key.yaml; do \
	    if git ls-files --error-unmatch $$f >/dev/null 2>&1; then echo "ERROR: $$f is tracked" && exit 1; fi; \
	  done; \
	  echo "  no credential-shaped strings or account IDs found"
