# NLSQL — AWS Marketplace (ECS container product) packaging.
#
# The image is built from the application repository (denissa4/ainlbot); this
# repo holds the deployment template and publishes the image to the AWS
# Marketplace ECR repository.

# From the AWS Marketplace Management Portal, after "Add repositories".
ECR_REGISTRY ?= 709825985650.dkr.ecr.us-east-1.amazonaws.com
ECR_REPO     ?= ECR_REPO
AWS_REGION   ?= us-east-1
VERSION      ?= 1.0.0

IMAGE   := $(ECR_REGISTRY)/$(ECR_REPO)
TEMPLATE := cloudformation/nlsql-ecs-fargate.yaml

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: check-vars
check-vars:
	@test "$(ECR_REPO)" != "ECR_REPO" \
	  || (echo "ERROR: set ECR_REPO to the repository created in the Marketplace portal" && exit 1)

.PHONY: validate
validate: ## Validate the CloudFormation deployment template
	aws cloudformation validate-template \
	  --template-body file://$(TEMPLATE) --region $(AWS_REGION) >/dev/null
	@echo "  template is valid"

.PHONY: login
login: ## Authenticate docker against the Marketplace ECR registry
	aws ecr get-login-password --region $(AWS_REGION) \
	  | docker login --username AWS --password-stdin $(ECR_REGISTRY)

.PHONY: push
push: check-vars login ## Tag the application image and push it to Marketplace ECR
	@# Marketplace is x86-only, so the source image must be linux/amd64.
	docker tag nlsql:$(VERSION) $(IMAGE):$(VERSION)
	docker push $(IMAGE):$(VERSION)

.PHONY: no-secrets
no-secrets: ## Fail if a credential-shaped string is committed (this repo is public)
	@! grep -rniE '(ApiToken|AppPassword|DbPassword)[":= ]+[A-Za-z0-9!@#$$%^&*_-]{8,}' \
	    --include='*.yaml' --include='*.yml' --include='*.md' . \
	    | grep -viE '(dummy|example|REPLACE|<|\$$|Ref |ValueFrom|Description|Name:|TODO)' \
	  || (echo "ERROR: possible credential committed" && exit 1)
	@echo "  no credential-shaped strings found"
