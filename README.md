# NLSQL AI Agent on AWS Marketplace — ECS container product

This branch, **`AWS-ECS-AI-AGENT`**, packages **NLSQL AI Agent** as an **AWS Marketplace
container product** with a **Container image** delivery option, deployed on Amazon ECS with AWS
Fargate from a CloudFormation template.

**How it relates to the other branches:**

- It is forked from **`AWS-ECS`**, which packages the separate NLSQL listing. The NLSQL template
  is removed here; the Makefile, the guide layout and the CloudFront-in-front-of-an-ALB shape
  are carried over.
- It is independent of `main`, which packages NLSQL for **Google Cloud Marketplace** (Helm chart,
  `mpdev` deployer). `main` is the repository's default branch, so every listing link must name
  a commit of this branch, never the repository root or a branch.
- The image is built from [`denissa4/agent4docs`](https://github.com/denissa4/agent4docs), branch
  **`aws-marketplace-offer`**, not `master`. That branch carries what AWS requires and the running
  Azure/Terraform deployments do not take: Debian 13 and Python 3.13 instead of end-of-life
  Python 3.7, a non-root container with nginx on 8080, Bedrock through the task role, the baked
  embedding model, and the fail-closed `RegisterUsage` check. `master` is merged into it, never
  the reverse. Its `docs/aws-marketplace.md` has the merge checklist.

## Listing shape

| | |
|---|---|
| Product | NLSQL AI Agent |
| Product type | Container product |
| Delivery option | Container image, Amazon ECS on AWS Fargate, CloudFormation template |
| Supported services (portal) | **ECS** only. Fargate is part of ECS; do not tick EKS, since no EKS deployment is shipped |
| Pricing | hourly, per Amazon ECS task (metered to the second, billed per hour) |
| Free trial | 30 days, covering one running task |
| Entitlement and metering | `RegisterUsage`, called once per task from inside the image, fail-closed |
| Architecture | **linux/amd64** only: `msodbcsql17` (SQL Server driver) has no arm64 build for Debian 13. Marketplace itself accepts arm64 |
| Container port | 8080; the container runs as uid 10001 |
| Public endpoint | CloudFront (HTTPS, default certificate) in front of an HTTP load balancer that rejects direct requests |
| LLM | Claude on Amazon Bedrock through global inference profiles and the task role; no API keys |

## Identifiers

| | |
|---|---|
| Product ID | `prod-lmpbrn4c3vus4` |
| Product code | `dwcy8qajiorcyvb3gtznytb5g` |
| RegisterUsage public key version | `1` |
| Public key (baked as `api/nlsql/aws_marketplace_pubkey_v1.pem`) | RSA 3072, SHA-256 `0afc3a22bdf3ab2074a80bdba2a08cd15c82888d1767a3119147d8c9653cf874` |
| ECR repository | `709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-agent` |
| Current version | `1.0.0` (local tag `ai-agent:1.0.0`) |

## Layout

```
cloudformation/nlsql-ai-agent-ecs-fargate.yaml   deployment template buyers launch
docs/aws-deployment-guide.md                     buyer guide the usage instructions link to
listing/usage-instructions.txt                   Add new version: usage instructions (<= 4000)
listing/delivery-option-description.txt          Add new version: delivery option description (< 1000)
listing/release-notes/<version>.md               Add new version: version title and release notes
listing/product-description-additions.md         Update product information: required disclosures
teams/                                           Teams manifest template, placeholder icons
scripts/deploy-test.sh, destroy-test.sh          seller-account test stack
scripts/listing-check.py                         listing length and character checks
Makefile                                         checks, publishing and test-stack targets
```

## The deployment template

`cloudformation/nlsql-ai-agent-ecs-fargate.yaml` creates one Fargate task behind an Application
Load Balancer and a CloudFront distribution, in a VPC and subnets the buyer already has, plus an
optional private documents bucket. It is what the listing's **Deployment templates** entry points
at. The load-bearing parts:

- **The task role grants `aws-marketplace:RegisterUsage`.** Under hourly pricing that call is the
  billing path, not only an entitlement check. The image strips every other credential and
  endpoint source (`AWS_ENDPOINT_URL*`, static keys, profiles, proxies, CA bundles) before it
  calls, pins the regional metering endpoint, and verifies the returned signature against the
  baked key with a nonce bound to the task ARN. Anything but success or throttling stops the task
  (container exit 78), and the bot and document service refuse to serve until they have verified
  the signature themselves.
- **`AWS_MARKETPLACE_PRODUCT_CODE`** may only repeat the baked code. Editing it in a downloaded
  template stops the task instead of buying unmetered use.
- **`ImageUri` has an `AllowedPattern`** that accepts only
  `709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-agent:<x.y.z>`. The test scripts deploy a
  temporary copy that also accepts a private test repository.
- **CloudFront terminates TLS**, because Bot Framework refuses plain HTTP and the load balancer has
  no certificate. The listener's default action is `403`; a rule forwards only requests carrying
  `X-Origin-Verify` with the stack's UUID (`!Select [2, !Split ['/', !Ref AWS::StackId]]`), which
  CloudFront adds as an origin custom header. `OriginReadTimeout` is 60 s: the bot acknowledges
  turns at once and replies proactively.
- **`/health` returns 503 until the subscription is verified**, so the target group never routes
  to an unentitled task. `/ready` reports document indexing and is not used for routing.
- **`DesiredCount` is fixed at 1.** Chart and CSV links live on the task's disk, NLSQL keeps
  process-global state, and Bot Framework carries no ALB cookie, so a second task would break
  conversations. There is no stickiness to configure. Scaling is vertical (`TaskCpu`,
  `TaskMemory`, `EphemeralStorage`).
- **Credentials come from one Secrets Manager secret**: `AppPassword` always, `ApiToken` and
  `DbPassword` only in DocsAndSql (added with `!If`/`AWS::NoValue`). AWS rejects templates and
  images with hardcoded secrets.
- **Mode.** `DocsAndSql` (default) leaves `TalkToDocs` unset, which the bot treats as "NLSQL
  first, documents as the fallback"; `DocsOnly` sets it to `true` and drops every database
  variable. Database parameters default to empty and are enforced by `Rules` only in DocsAndSql.
- **Least-privilege task role**: S3 read on the one bucket, and Bedrock `InvokeModel` /
  `InvokeModelWithResponseStream` on Anthropic foundation models and the account's
  `global.anthropic.*` inference profiles. No Marketplace `Subscribe`: enabling the model is a
  documented buyer prerequisite.
- **Region rule.** `AWS::Region` must be one of 16 Regions: the product's Region availability
  intersected with the Regions where every `LanguageModel` value is a global inference profile.
  Checked with `aws bedrock list-inference-profiles` in each Region on 2026-09-30; ap-east-1 and
  me-south-1 have no Bedrock endpoint and are excluded. **Re-run the check before each release**,
  and keep the listing's Regions in the portal equal to this list.
- **`LanguageModel` values** are global inference-profile IDs, default
  `global.anthropic.claude-opus-5`. `global.anthropic.claude-sonnet-5-5` is included because
  `llama-index-llms-bedrock-converse` 0.15.3 (the pinned minimum) knows its context size and
  temperature rule; the application's per-model unit test must cover every value in the list.

Validate any change before publishing: `make validate`.

## Makefile

| Target | What it does |
|---|---|
| `check-vars` | the template's `ImageUri` default is `$(IMAGE):$(VERSION)` and its product code is the baked one; with `../agent4docs` checked out, also compares the app's baked code and key hash; the release notes exist; the usage instructions and guide name the image |
| `check-tags` | fails if `$(VERSION)` already exists in the Marketplace repository |
| `validate` | `validate-template` plus cfn-lint (local, `pipx run`, or in a Python container) |
| `login` | docker login to the Marketplace registry |
| `push` | refuses unless `ai-agent:$(VERSION)` is labelled `com.nlsql.stage=enforce`, bakes the product key (`com.nlsql.pubkey-sha256`) and is linux/amd64; then `check-tags`, tag and push |
| `digest` | prints the pushed image digest |
| `template-url` | prints the commit-pinned template and guide URLs; fails on uncommitted files, an unpushed commit, or a URL of 256 characters or more |
| `listing-check` | length and printable-ASCII checks on the listing texts (`<commit>` counted as 40 characters) |
| `no-secrets` | fails on credential-shaped strings, AWS account IDs other than the Marketplace registry's, or a tracked `.env` |
| `deploy-test`, `destroy-test` | the seller-account test stack below |

## Publishing a version (seller)

1. In `agent4docs@aws-marketplace-offer`: `make build test scan smoke inspect` produces
   `ai-agent:$(VERSION)` (linux/amd64, enforce stage).
2. Here: bump `VERSION`, the template's `ImageUri` default, the image in
   `listing/usage-instructions.txt` and `docs/aws-deployment-guide.md`, and add
   `listing/release-notes/<version>.md`. Then:

   ```shell
   make check-vars validate listing-check no-secrets
   ```

3. Commit and push this branch, then `make template-url` for the pinned URLs.
4. **Push the image**: `make push`, then `make digest`. A pushed tag is permanent, so confirm
   the version before running it.
5. **Add new version** in the portal, **Container image** delivery option:
   - version title and release notes from `listing/release-notes/<version>.md`;
   - the image `709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-agent:<version>`;
   - **Supported services: ECS**;
   - delivery option title (for example "Amazon ECS on AWS Fargate (CloudFormation)") and
     `listing/delivery-option-description.txt`;
   - usage instructions from `listing/usage-instructions.txt`, with `<commit>` replaced;
   - **Deployment templates**: the pinned `raw.githubusercontent.com` template URL.
6. For the first version, also apply `listing/product-description-additions.md` under **Update
   product information**.

Repository names in the Marketplace registry are permanent and unique across the seller's
products. `nlsql/ai-agent` already exists; new repositories come from **Server products, Request
changes, Add repositories**.

## Testing in the seller account

AWS requires a test task in us-east-1. Push the enforce image to a **private** test repository
(`<account>.dkr.ecr.us-east-1.amazonaws.com/nlsql-ai-agent-test:<tag>`) rather than the
Marketplace repository, so no tag is spent. Then:

```shell
make deploy-test ENV_FILE=<path-to-local-env-file>                  # DocsAndSql
make deploy-test ENV_FILE=<path-to-local-env-file> MODE=DocsOnly    # same stack, switched
make destroy-test                                                   # stack and secret
```

The env file uses the application's env names and is never committed (`.env` and `.env.*` are
ignored; the script refuses a tracked file). The script creates the secret from it through a
0600 temporary file, uses `AwsS3BucketName` as the documents bucket (creating it if needed),
deploys into the default VPC unless `VPC_ID`/`SUBNET_IDS` are set, and tags everything
`nlsql:purpose=test`; `destroy-test` refuses anything without that tag. `DOCS_DIR=<folder>` syncs
sample documents and restarts the task. While the product is Draft or the seller account is not
subscribed, the expected result is the fail-closed path: the task stops with exit 78 and
`CustomerNotEntitledException`, and the circuit breaker rolls the stack back.

## Corrections to the NLSQL precedent

- **Supported services** is **ECS** only. Fargate is covered by ECS; the portal default of ECS
  plus EKS promises an EKS deployment this product does not ship.
- The image is amd64 because of `msodbcsql17`, not because Marketplace is x86-only.
- `/health` is no longer a bare `return 200`: it is gated on the verified subscription.
- No target-group stickiness and no `LoadBalancerScheme`: an internal ALB cannot be a CloudFront
  origin, and there is only ever one task.

## Outstanding before submission

- [ ] The real hourly price and the free trial (1 task, 30 days) on the product's offer, which
      is still Draft with no price.
- [ ] Apply `listing/product-description-additions.md`: the add-on sentence, the external services
      list, and replacements for the highlight and short description, which still promise Azure
      infrastructure and a website assistant this listing does not deliver.
- [ ] Portal Regions equal to the template's Region rule (16 Regions).
- [ ] Test stack in us-east-1: fail-closed run (T2) while unentitled; entitled run (T1) with the
      E2E suite after the seller account subscribes; secret-leak log check.
- [ ] Replace the placeholder Teams icons with the brand mark (same names and sizes).
- [ ] The Teams bot needs an Azure Bot registration (free standard channel). The NLSQL listing
      passed review with the same requirement; keep the wording factual if review asks.
- [ ] NLSQL listing follow-up: the same entitlement holes fixed here, the public Docker Hub image,
      and its Node 20 runtime.
