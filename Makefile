# NLSQL — Google Cloud Marketplace packaging.
#
# Prerequisites: docker, gcloud, helm, and mpdev
# (https://github.com/GoogleCloudPlatform/marketplace-k8s-app-tools).
#
# Set these before running any target:
#   export MARKETPLACE_PROJECT_ID=<your Marketplace-assigned GCP project>
#   export SERVICE_NAME=<your Marketplace service name>
#   export TAG=1.2.0

MARKETPLACE_PROJECT_ID ?= MARKETPLACE_PROJECT_ID
SERVICE_NAME           ?= SERVICE_NAME
TAG                    ?= 1.2.0

REGISTRY  := gcr.io/$(MARKETPLACE_PROJECT_ID)/nlsql
APP_IMAGE := $(REGISTRY):$(TAG)
DEPLOYER  := $(REGISTRY)/deployer:$(TAG)

# The image built by the NLSQL application repository, before it is promoted into
# the Marketplace registry.
SOURCE_IMAGE ?= us-docker.pkg.dev/nlsql-public/nlsql/nlsql:$(TAG)

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: check-vars
check-vars:
	@test "$(MARKETPLACE_PROJECT_ID)" != "MARKETPLACE_PROJECT_ID" \
	  || (echo "ERROR: set MARKETPLACE_PROJECT_ID" && exit 1)
	@test "$(SERVICE_NAME)" != "SERVICE_NAME" \
	  || (echo "ERROR: set SERVICE_NAME" && exit 1)

.PHONY: configure
configure: ## Substitute the Marketplace placeholders with your real values
	@test -n "$(PARTNER_ID)" || (echo "ERROR: set PARTNER_ID (from Producer Portal)" && exit 1)
	@test -n "$(LISTING_URL)" || (echo "ERROR: set LISTING_URL (the published listing page)" && exit 1)
	@$(MAKE) --no-print-directory check-vars
	@# Only the literal placeholders are rewritten; the Makefile's own variable
	@# names are left alone. perl -i is portable across macOS and GNU userland.
	grep -rl --include='*.yaml' --include='*.md' 'gcr.io/MARKETPLACE_PROJECT_ID' . \
	  | xargs perl -pi -e 's{gcr\.io/MARKETPLACE_PROJECT_ID}{gcr.io/$(MARKETPLACE_PROJECT_ID)}g'
	grep -rl --include='*.yaml' --include='*.md' 'LISTING_URL' . \
	  | xargs perl -pi -e 's{LISTING_URL}{$(LISTING_URL)}g'
	grep -rl --include='*.yaml' '"PARTNER_ID"' . \
	  | xargs perl -pi -e 's{"PARTNER_ID"}{"$(PARTNER_ID)"}g'
	@echo "Placeholders substituted. Review the diff, then run: make lint verify"

.PHONY: lint
lint: ## Lint and render the chart with representative values
	helm lint chart/nlsql \
	  --set nlsql.StaticEndPoint=https://nlsql.example.com/ \
	  --set database.DataSource=db.example.com \
	  --set database.DbName=analytics \
	  --set database.DbUser=nlsql_ro \
	  --set credentials.ApiToken=dummy \
	  --set credentials.DbPassword=dummy
	helm lint apptest/deployer/chart/nlsql-tester

.PHONY: no-secrets
no-secrets: ## Fail if any credential-shaped string is committed
	@! grep -rniE '(ApiToken|AppPassword|DbPassword)[":= ]+[A-Za-z0-9!@#$$%^&*_-]{8,}' \
	    --include='*.yaml' --include='*.yml' --include='*.md' . \
	    | grep -viE '(dummy|example|REPLACE|<|\$$|required|type:|title:|description:|MASKED)' \
	  || (echo "ERROR: possible credential committed" && exit 1)
	@echo "no credential-shaped strings found"

.PHONY: promote-image
promote-image: check-vars ## Copy the app image into the Marketplace registry and annotate it
	docker pull $(SOURCE_IMAGE)
	printf 'FROM $(SOURCE_IMAGE)\nLABEL com.googleapis.cloudmarketplace.product.service.name="services/$(SERVICE_NAME)"\n' \
	  | docker build --tag $(APP_IMAGE) -
	docker push $(APP_IMAGE)

.PHONY: deployer-image
deployer-image: check-vars ## Build and push the Marketplace deployer image
	docker build --tag $(DEPLOYER) --file deployer/Dockerfile .
	docker push $(DEPLOYER)

.PHONY: digest
digest: check-vars ## Print the immutable digest to quote in README.md
	@gcloud container images describe $(APP_IMAGE) \
	  --format='value(image_summary.fully_qualified_digest)'

.PHONY: doctor
doctor: ## Check the local mpdev environment
	mpdev doctor

.PHONY: install
install: check-vars ## Install into the current kubectl context via mpdev
	kubectl create namespace nlsql-test --dry-run=client -o yaml | kubectl apply -f -
	mpdev install \
	  --deployer=$(DEPLOYER) \
	  --parameters='{"name": "nlsql-1", "namespace": "nlsql-test"}'

.PHONY: verify
verify: check-vars ## Run the full Marketplace verification (install, test, uninstall)
	mpdev verify --deployer=$(DEPLOYER)

.PHONY: clean
clean: ## Remove a local test install
	-kubectl delete application nlsql-1 --namespace nlsql-test
	-kubectl delete namespace nlsql-test

.PHONY: all
all: lint no-secrets promote-image deployer-image verify ## Full release pipeline
