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
scripts/e2e-test.py                              end-to-end test of that stack, the way Teams drives it
scripts/e2e-docs/                                sample document deploy-test syncs and e2e-test asks about
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
  baked key with a nonce bound to the task ARN. A definitive refusal (not subscribed, access
  denied, an untrusted product code, a signature that does not verify, ...) stops the task with
  container exit 78. Throttling is retried for up to 6 hours and transient Metering errors (5xx,
  connection failures) for up to 30 minutes, with `/health` at 503 meanwhile; behind the load
  balancer ECS replaces a task still retrying once the 300 s health-check grace period is over (a
  new task and a new `RegisterUsage` call, no exit 78). The bot, NLSQL and the document service
  each refuse to serve until they have verified the signature themselves.
- **`AWS_MARKETPLACE_PRODUCT_CODE`** may only repeat the baked code. Editing it in a downloaded
  template stops the task instead of buying unmetered use.
- **`ImageUri` has an `AllowedPattern`** that accepts only
  `709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-agent:<x.y.z>`. The test scripts deploy a
  temporary copy that also accepts a private test repository.
- **CloudFront terminates TLS**, because Bot Framework refuses plain HTTP and the load balancer has
  no certificate. The load balancer's security group admits port 80 only from the AWS-managed
  prefix list `com.amazonaws.global.cloudfront.origin-facing` (its per-Region IDs are in the
  `RegionMap` mapping; the reference counts as 55 inbound rules). The listener's default action is
  `403`; a rule forwards only requests carrying `X-Origin-Verify` with the stack's UUID
  (`!Select [2, !Split ['/', !Ref AWS::StackId]]`), which CloudFront adds as an origin custom
  header. The CloudFront-to-ALB hop is HTTP, so Security Hub control CloudFront.9 flags the
  distribution; the guide records that as accepted. `OriginReadTimeout` is 60 s: the bot
  acknowledges turns at once and replies proactively. Draining is 300 s, so an answer still being
  computed in the old task during a deployment has up to five minutes to finish.
- **`/health` returns 503 until the subscription is verified**, and ECS replaces a task whose
  target never turns healthy. It is not what keeps an unentitled task from serving: with a single
  target the load balancer fails open and forwards to it anyway. nginx's own 503 gate on
  `/api/messages` and each service's verification of the signature do that. `/ready` reports
  document indexing and is not used for routing; `CREATE_COMPLETE` does not wait for it.
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
| `push` | refuses unless `ai-agent:$(VERSION)` is labelled `com.nlsql.stage=enforce`, bakes the product key (`com.nlsql.pubkey-sha256`) and is linux/amd64, and unless the image itself (`docker run`) holds the key with that hash and trusts only the product code in `aws_marketplace.py`, `aws_marketplace_verify.py` and `bot/entitlement.js`; then `check-tags`, tag and push |
| `digest` | prints the pushed image digest |
| `template-url` | prints the commit-pinned template and guide URLs; fails on uncommitted changes to the template, `docs`, `listing` or `teams`, an unpushed commit, or a URL of 256 characters or more |
| `listing-check` | length and printable-ASCII checks on the listing texts (`<commit>` counted as 40 characters) |
| `no-secrets` | fails on credential-shaped strings, AWS account IDs other than the Marketplace registry's, or a tracked `.env`, in the working tree and in the branch's history (`git log -p`); allowed IDs and sample values are filtered per match, never per line |
| `deploy-test`, `destroy-test` | the seller-account test stack below (`STACK_NAME`, default `nlsql-ai-agent-test`); `deploy-test` also syncs `DOCS_DIR` (default `scripts/e2e-docs`) |
| `e2e-test` | drives that stack the way Teams does (`scripts/e2e-test.py`); `SQL_QUESTION`, `DOCS_QUESTION`, `DOCS_EXPECT` |

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
make e2e-test    ENV_FILE=<path-to-local-env-file> [SQL_QUESTION="..."]
make deploy-test ENV_FILE=<path-to-local-env-file> MODE=DocsOnly    # same stack, switched
make destroy-test                                                   # stack, secret, capture function
```

The test targets take `STACK_NAME=<name>` (default `nlsql-ai-agent-test`, or `$STACK_NAME` from
the environment), so `e2e-test` and `destroy-test` act on the stack `deploy-test` made.

`e2e-test` (`scripts/e2e-test.py`) exercises the deployed stack the way Microsoft Teams does, with
no Teams tenant involved: a temporary Lambda function URL stands in for the Bot Connector (it is
sent as the activities' `serviceUrl` and captures the bot's replies), and each Teams-shaped
activity carries an Entra ID client-credentials token for the bot's own app ID. It checks
`/health` and `/ready`, that the load balancer refuses requests that bypass CloudFront, that every
turn is acknowledged within seconds and answered proactively exactly once, the welcome message,
a document answer, a Teams file attachment answer, an NLSQL answer and its chart link
(`SQL_QUESTION`, DocsAndSql), and that no secret from the env file appears in the task's logs:
the bot secret under either of its names and every key that names a password, secret or token,
in every log event since the stack was created. The load balancer check accepts no connection
(its security group admits CloudFront only) or `403`. The default document question expects
`AMBER-FALCON-42` from `scripts/e2e-docs/nlsql-ai-agent-test-handbook.txt`, which
`make deploy-test` syncs; `DOCS_QUESTION` and `DOCS_EXPECT` ask about your own documents
instead. The capture function and its role are deleted at the end, also when the run fails
(`--keep-capture` keeps them), and only if they carry the test tag. A real Teams check is still
the final step: paste the `MessagingEndpoint` output into the Azure Bot and chat with it.

The env file uses the application's env names and is never committed (`.env` and `.env.*` are
ignored; the script refuses a tracked file). The script creates the secret from it through a
0600 temporary file, uses `AwsS3BucketName` as the documents bucket (creating it if needed),
deploys into the default VPC unless `VPC_ID`/`SUBNET_IDS` are set, and tags everything
`nlsql:purpose=test`; `destroy-test` refuses anything without that tag. `make deploy-test` syncs
`DOCS_DIR` (default `scripts/e2e-docs`; `DOCS_DIR=<folder>` for your own, `DOCS_DIR=` for none)
to the documents bucket and restarts the task.

The seller account is entitled to its own product even while it is Draft: `RegisterUsage`
verified on the first test stack (2026-09-30). The fail-closed path is exercised separately: run
one task from the stack's task definition with a task role that lacks
`aws-marketplace:RegisterUsage` (`aws ecs run-task ... --overrides '{"taskRoleArn":"<role>"}'`);
it must stop with exit code 78 and log
`AWS Marketplace: NLSQL AI Agent cannot start: the ECS task role may not call aws-marketplace:RegisterUsage (AccessDeniedException) (exit 16).`
This is test T6; it passed on the seller account.

The seller account's Bedrock model access decides which `LanguageModel` a test can use:
`LANGUAGE_MODEL=<allowed value> make deploy-test ...` overrides the default. On 2026-09-30 Claude
Sonnet 4.6 was enabled there and Claude Opus 5 (the template default) was not.

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
      list (with the Azure subscription and cdn.plot.ly), and replacements for the highlight and
      short description, which still promise Azure infrastructure and a website assistant this
      listing does not deliver.
- [ ] Portal Regions equal to the template's Region rule (16 Regions).
- [x] Test stack in us-east-1, entitled run (T1): `RegisterUsage` verified on the seller account
      (2026-09-30).
- [x] Fail-closed run (T6): a task whose role lacks `aws-marketplace:RegisterUsage` stops with exit
      78 ([Testing](#testing-in-the-seller-account)). It replaces T2 ("while unentitled"), which
      cannot run: the seller account is entitled to its own Draft product.
- [x] E2E suite (`make e2e-test`) on the test stack: 18 of 18 passed (image rc2, Claude Sonnet 4.6).
- [x] Secret-leak log check, part of the E2E suite.
- [x] msodbcsql17 against Azure SQL from the test stack: connection and TLS behaviour as recorded
      in the guide's engine table.
- [ ] Before `make push`, run `make deploy-test e2e-test` again on an image built with the
      final-review fixes (bot caller check, NLSQL timeout reply, attachment rule, NLSQL entitlement
      gate) and this template (CloudFront prefix list, 300 s draining).
- [ ] Replace the placeholder Teams icons with the brand mark (same names and sizes).
- [x] The Teams bot needs an Azure Bot registration, in a Microsoft Azure subscription (free
      standard channel): disclosed in the usage instructions, the guide and the product-description
      additions. The NLSQL listing passed review with the same requirement; keep the wording
      factual if review asks.
- [ ] NLSQL listing follow-up: the same entitlement holes fixed here, the public Docker Hub image,
      and its Node 20 runtime.
