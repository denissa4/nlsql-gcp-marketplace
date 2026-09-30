# NLSQL AI Agent — AWS Marketplace (ECS container product) packaging.
#
# The image is built from the application repository (denissa4/agent4docs,
# branch aws-marketplace-offer, `make build` there); this repo holds the
# deployment template and listing texts, and publishes the image to the AWS
# Marketplace ECR repository.

# From the AWS Marketplace Management Portal, after "Add repositories".
# <sellerName>/<repositoryName>, both created by the portal and permanent.
ECR_REGISTRY ?= 709825985650.dkr.ecr.us-east-1.amazonaws.com
ECR_REPO     ?= nlsql/ai-agent
AWS_REGION   ?= us-east-1
VERSION      ?= 1.0.0

# The locally built enforce image (agent4docs `make build`).
LOCAL_IMAGE  ?= ai-agent:$(VERSION)
# Where listing links point. Always a commit, never a branch: the repository's
# default branch is the Google Cloud packaging.
GITHUB_REPO  ?= denissa4/nlsql-gcp-marketplace
# Optional checkout of the application, to compare the baked trust anchors.
APP_REPO     ?= ../agent4docs

IMAGE       := $(ECR_REGISTRY)/$(ECR_REPO)
REGISTRY_ID := $(firstword $(subst ., ,$(ECR_REGISTRY)))
TEMPLATE    := cloudformation/nlsql-ai-agent-ecs-fargate.yaml

# Trust anchors baked into the image. `push` refuses anything else.
PRODUCT_CODE  := dwcy8qajiorcyvb3gtznytb5g
PUBKEY_SHA256 := 0afc3a22bdf3ab2074a80bdba2a08cd15c82888d1767a3119147d8c9653cf874

# Seller-account test stack (scripts/deploy-test.sh).
ENV_FILE ?=
MODE     ?= DocsAndSql

LISTING_USAGE    := listing/usage-instructions.txt
LISTING_DELIVERY := listing/delivery-option-description.txt
LISTING_NOTES    := listing/release-notes/$(VERSION).md
LISTING_DESC     := listing/product-description-additions.md

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: check-vars
check-vars: ## Template, listing and guide agree on the image and product code
	@test "$(ECR_REPO)" != "ECR_REPO" \
	  || (echo "ERROR: set ECR_REPO to the repository created in the Marketplace portal" && exit 1)
	@# The template's ImageUri default is what buyers launch with, so it must
	@# name the tag this Makefile pushes.
	@grep -qF 'Default: $(IMAGE):$(VERSION)' $(TEMPLATE) \
	  || (echo "ERROR: ImageUri default in $(TEMPLATE) does not match $(IMAGE):$(VERSION)" && exit 1)
	@grep -qF 'AWS_MARKETPLACE_PRODUCT_CODE, Value: $(PRODUCT_CODE)}' $(TEMPLATE) \
	  || (echo "ERROR: AWS_MARKETPLACE_PRODUCT_CODE in $(TEMPLATE) is not $(PRODUCT_CODE)" && exit 1)
	@if [ -f "$(APP_REPO)/api/nlsql/aws_marketplace.py" ]; then \
	  grep -qF 'BAKED_PRODUCT_CODE = "$(PRODUCT_CODE)"' "$(APP_REPO)/api/nlsql/aws_marketplace.py" \
	    || (echo "ERROR: $(APP_REPO) bakes a different product code" && exit 1); \
	  test "$$(shasum -a 256 "$(APP_REPO)/api/nlsql/aws_marketplace_pubkey_v1.pem" | cut -d' ' -f1)" = "$(PUBKEY_SHA256)" \
	    || (echo "ERROR: $(APP_REPO) bakes a different public key" && exit 1); \
	  echo "  $(APP_REPO) bakes the same product code and public key"; \
	else \
	  echo "  (no application checkout at $(APP_REPO); baked product code not compared)"; \
	fi
	@test -f $(LISTING_NOTES) || (echo "ERROR: $(LISTING_NOTES) is missing" && exit 1)
	@grep -qF '$(IMAGE):$(VERSION)' $(LISTING_USAGE) \
	  || (echo "ERROR: $(LISTING_USAGE) does not name $(IMAGE):$(VERSION)" && exit 1)
	@grep -qF '$(IMAGE):$(VERSION)' docs/aws-deployment-guide.md \
	  || (echo "ERROR: docs/aws-deployment-guide.md does not name $(IMAGE):$(VERSION)" && exit 1)
	@echo "  template, listing and guide agree on $(IMAGE):$(VERSION), product $(PRODUCT_CODE)"

.PHONY: check-tags
check-tags: ## Fail if $(VERSION) already exists in the Marketplace repository
	@# A tag submitted in a version cannot be replaced, so never overwrite one.
	@out=$$(aws ecr describe-images --registry-id $(REGISTRY_ID) --repository-name $(ECR_REPO) \
	    --image-ids imageTag=$(VERSION) --region $(AWS_REGION) --output text 2>&1); rc=$$?; \
	  if [ $$rc -eq 0 ]; then echo "ERROR: $(IMAGE):$(VERSION) already exists; bump VERSION" && exit 1; fi; \
	  case "$$out" in *ImageNotFoundException*) echo "  $(VERSION) is not yet in $(ECR_REPO)";; \
	    *) echo "ERROR: could not check $(ECR_REPO): $$out" && exit 1;; esac

.PHONY: validate
validate: ## Validate the template with CloudFormation and cfn-lint
	aws cloudformation validate-template \
	  --template-body file://$(TEMPLATE) --region $(AWS_REGION) >/dev/null
	@echo "  CloudFormation accepts the template"
	@if command -v cfn-lint >/dev/null 2>&1; then cfn-lint $(TEMPLATE); \
	elif command -v pipx >/dev/null 2>&1; then pipx run cfn-lint $(TEMPLATE); \
	elif command -v docker >/dev/null 2>&1; then \
	  docker run --rm -v "$(CURDIR)":/src -w /src public.ecr.aws/docker/library/python:3.12-slim \
	    sh -c 'pip install --quiet --disable-pip-version-check cfn-lint && cfn-lint $(TEMPLATE)'; \
	else echo "ERROR: install cfn-lint, pipx or docker" && exit 1; fi
	@echo "  cfn-lint passed"

.PHONY: login
login: ## Authenticate docker against the Marketplace ECR registry
	aws ecr get-login-password --region $(AWS_REGION) \
	  | docker login --username AWS --password-stdin $(ECR_REGISTRY)

.PHONY: push
push: check-vars ## Guard, tag and push $(LOCAL_IMAGE) to Marketplace ECR
	@# Only the enforce stage with the product's own key may ever reach buyers;
	@# the local-test image signs its own entitlement and must never be pushed.
	@test "$$(docker image inspect -f '{{ index .Config.Labels "com.nlsql.stage" }}' $(LOCAL_IMAGE))" = enforce \
	  || (echo "ERROR: $(LOCAL_IMAGE) is not labelled com.nlsql.stage=enforce" && exit 1)
	@test "$$(docker image inspect -f '{{ index .Config.Labels "com.nlsql.pubkey-sha256" }}' $(LOCAL_IMAGE))" = $(PUBKEY_SHA256) \
	  || (echo "ERROR: $(LOCAL_IMAGE) does not bake the product's public key" && exit 1)
	@# Marketplace accepts arm64 too, but msodbcsql17 is amd64-only and the
	@# template pins X86_64.
	@test "$$(docker image inspect -f '{{ .Os }}/{{ .Architecture }}' $(LOCAL_IMAGE))" = linux/amd64 \
	  || (echo "ERROR: $(LOCAL_IMAGE) is not linux/amd64" && exit 1)
	@$(MAKE) --no-print-directory check-tags
	@$(MAKE) --no-print-directory login
	docker tag $(LOCAL_IMAGE) $(IMAGE):$(VERSION)
	docker push $(IMAGE):$(VERSION)

.PHONY: digest
digest: ## Print the digest of the pushed $(VERSION)
	@aws ecr describe-images --registry-id $(REGISTRY_ID) --repository-name $(ECR_REPO) \
	  --image-ids imageTag=$(VERSION) --region $(AWS_REGION) \
	  --query 'imageDetails[0].imageDigest' --output text

.PHONY: template-url
template-url: ## Print the commit-pinned template and guide URLs for the listing
	@test -z "$$(git status --porcelain -- $(TEMPLATE) docs listing)" \
	  || (echo "ERROR: commit the template, docs and listing texts first" && exit 1)
	@commit=$$(git rev-parse HEAD); \
	  test -n "$$(git branch -r --contains $$commit 2>/dev/null)" \
	    || (echo "ERROR: $$commit is not on any pushed branch; push it first" && exit 1); \
	  url="https://raw.githubusercontent.com/$(GITHUB_REPO)/$$commit/$(TEMPLATE)"; \
	  test $${#url} -lt 256 || (echo "ERROR: template URL is $${#url} characters (limit 255)" && exit 1); \
	  echo "Deployment template: $$url"; \
	  echo "Deployment guide:    https://github.com/$(GITHUB_REPO)/blob/$$commit/docs/aws-deployment-guide.md"; \
	  echo "Replace <commit> in $(LISTING_USAGE) with $$commit before pasting it."

.PHONY: listing-check
listing-check: ## Enforce Marketplace length and character limits on listing texts
	@python3 scripts/listing-check.py \
	  $(LISTING_USAGE):4000 $(LISTING_DELIVERY):999 $(LISTING_NOTES):29999 $(LISTING_DESC):0

# Credential-shaped strings, and AWS account IDs other than the Marketplace
# registry's and documentation placeholders, must never reach this public
# repository. Placeholders (<...>, ${...}, $VAR) and CloudFormation references
# are not values.
SECRET_VALUE  := (ApiToken|AppPassword|DbPassword|clientSecret|client_secret|LanguageModelApiKey|EmbeddingModelApiKey|GraphModelApiKey|HF_TOKEN|HUGGING_FACE_HUB_TOKEN|SasToken|AzureStorageSasToken|AwsSecretAccessKey|aws_secret_access_key|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|private_key|MicrosoftAppPassword|DIRECT_LINE_SECRET)["'\'':= ]+[A-Za-z0-9+/!@%^&*_~.-]{8,}
SECRET_SHAPES := AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|[A-Za-z0-9_~.-]{3}[0-9]Q~[A-Za-z0-9_~.-]{31,34}|hf_[A-Za-z0-9]{30,}|gh[pousr]_[A-Za-z0-9]{36}|xox[abprs]-[A-Za-z0-9-]{10,}|sv=20[0-9]{2}-[0-9]{2}-[0-9]{2}&.*sig=|AccountKey=[A-Za-z0-9+/=]{40,}
PLACEHOLDER   := <[^>]*>|\$$\{|\$$\(|\$$[A-Za-z_]|ValueFrom|Description|ParameterKey|\{Name:|SECRET_(VALUE|SHAPES)

.PHONY: no-secrets
no-secrets: ## Fail if a credential or account ID is committed (this repo is public)
	@files=$$(git ls-files -co --exclude-standard | grep -vE '\.(png|jpg|ico)$$' \
	    | while read -r f; do if [ -f "$$f" ]; then echo "$$f"; fi; done); \
	  hits=$$(grep -nIE '$(SECRET_VALUE)' $$files | grep -vE '$(PLACEHOLDER)'); \
	  shapes=$$(grep -nIE '$(SECRET_SHAPES)' $$files | grep -v 'SECRET_SHAPES'); \
	  accounts=$$(grep -nIE '(^|[^0-9])[0-9]{12}([^0-9]|$$)' $$files \
	    | grep -vE '709825985650|000000000000|111122223333'); \
	  if [ -n "$$hits$$shapes$$accounts" ]; then \
	    printf '%s\n' "$$hits" "$$shapes" "$$accounts" | sed '/^$$/d' | cut -c1-160; \
	    echo "ERROR: possible credential or account ID committed" && exit 1; fi; \
	  for f in .env .env.local; do \
	    if git ls-files --error-unmatch $$f >/dev/null 2>&1; then echo "ERROR: $$f is tracked" && exit 1; fi; \
	  done; \
	  echo "  no credential-shaped strings or account IDs found"

.PHONY: deploy-test
deploy-test: ## Seller test stack from a local env file: ENV_FILE=<path> [MODE=DocsOnly]
	@test -n "$(ENV_FILE)" || (echo "ERROR: ENV_FILE=<path to local env file> is required" && exit 1)
	VERSION=$(VERSION) scripts/deploy-test.sh "$(ENV_FILE)" $(MODE)

.PHONY: destroy-test
destroy-test: ## Delete the seller test stack and its secret
	scripts/destroy-test.sh $(ENV_FILE)
