# NLSQL AI Employee on AWS Marketplace — ECS container product

This branch, **`AWS-ECS-AI-EMPLOYEE`**, packages **NLSQL AI Employee** as an **AWS Marketplace
container product** with a **Container image** delivery option, deployed on Amazon ECS with AWS
Fargate from a CloudFormation template. NLSQL AI Employee is an AI assistant for Microsoft Teams
and the other Azure Bot Service channels, using Claude on Amazon Bedrock; in Teams it also works
with each user's Microsoft 365 mail, calendar and OneDrive.

**How it relates to the other branches:**

- It starts from **`AWS-ECS`**, which packages the separate NLSQL listing, as `AWS-ECS-AI-AGENT`
  (NLSQL AI Agent) did. It is a sibling of that branch, not a fork: the AI Agent's product-neutral
  tooling (push guards, listing checker, test-stack and e2e scripts, the `teams/` and `listing/`
  layout) was ported here as new files and adapted. The NLSQL template is removed on this branch.
- Like the other product branches it is never merged. `main` is the repository's default branch
  and packages NLSQL for **Google Cloud Marketplace**, so every listing link must name a commit
  of this branch, never the repository root or a branch.
- The image is built from [`denissa4/ai-employee`](https://github.com/denissa4/ai-employee),
  branch **`aws-marketplace-offer`**, not `master`. That branch carries what AWS requires and the
  running Azure and EKS deployments do not take: a non-root Debian 13 image with nginx on 8080,
  no sshd, Python 3.13 and Node 24, Bedrock and storage through the task role, the security fixes,
  the hardened live browser and the fail-closed `RegisterUsage` check. `master` is merged into it,
  never the reverse. **Its `docs/aws-marketplace.md` is authoritative** for everything the image
  expects: the environment and IAM contract, the exact strings the guide and the e2e quote,
  sizing and pinned values.

## Listing shape

| | |
|---|---|
| Product | NLSQL AI Employee |
| Product type | Container product |
| Delivery option | Container image, Amazon ECS on AWS Fargate, CloudFormation template |
| Supported services (portal) | **ECS** only. Fargate is part of ECS; do not tick EKS, since no EKS deployment is shipped |
| Pricing | fixed monthly subscription (`RecurringPaymentTerm`). **The price is not decided yet**: `listing/README.md` carries a `<PRICE>` placeholder, and the offer a nominal test price |
| Free trial | none (AWS offers container free trials with hourly pricing only) |
| Entitlement | `RegisterUsage`, called once per task from inside the image, fail-closed |
| Architecture | **linux/amd64** only: the release image, its hashed Python lock (CPython 3.13 on x86_64) and the template's `X86_64` are what the app branch builds and tests. Marketplace itself accepts arm64 |
| Container port | 8080; the container runs as uid 10001 |
| Public endpoint | CloudFront (HTTPS, default certificate) in front of an HTTP load balancer that rejects direct requests and every path but the product's own |
| LLM | Claude on Amazon Bedrock through global inference profiles and the task role; no API keys. Default `global.anthropic.claude-sonnet-5-5` |
| Optional | Google Gemini (browser agent with a live view, image reading), only with the buyer's own key, off by default |
| Storage | 2 DynamoDB tables and 2 S3 buckets the stack creates, kept when it is deleted |
| Channels | every Azure Bot Service channel the buyer enables; the Microsoft 365 features in Teams only |

## Identifiers

| | |
|---|---|
| Product ID | `prod-m37urirlvp7mw` |
| Product code | `f5jcvx1rl6wb66fhzy5qycpnr` |
| RegisterUsage public key version | `1` (Active in the product entity's `SignatureVerificationKeys`) |
| Public key | baked by the app as `marketplace/aws_marketplace_pubkey_v1.pem` (app item B11), sha256 `68f9c9b44a3bf144a3292718879681231ab20719386792c8aa2559c82f86d2c1`, the Makefile's `PUBKEY_SHA256`. `push` and `push-test` refuse any other key, including the app's earlier placeholder (sha256 `c0dc17d4a034c0f42d73a806bd6b0449c5ad3a18d307db14528579160829ae5b`) |
| ECR repository | `709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-employee` (in the product entity's `Repositories`) |
| Current version | `1.0.1` (local tag `ai-employee-aws:1.0.1`) |

## Layout

```
cloudformation/nlsql-ai-employee-ecs-fargate.yaml   deployment template buyers launch
docs/aws-deployment-guide.md                        buyer guide the usage instructions link to
listing/usage-instructions.txt                      Add new version: usage instructions (<= 4000)
listing/delivery-option-description.txt             Add new version: delivery option description (< 1000)
listing/release-notes/<version>.md                  Add new version: version title and release notes
listing/README.md                                   which file goes into which portal field; offer and version fields
listing/product/                                    Update product information: title, descriptions, highlights, keywords, support
teams/                                              Teams manifest template, build-package.sh, icons (teams/README.md)
scripts/template-invariants.py                      offline template checks (env names, paths, Regions, size, key pin)
scripts/guide-check.py                              offline guide checks against the template and the app's exact strings
scripts/listing-check.py                            listing length, character and Region checks
scripts/push-guard.sh                               offline image checks that push and push-test run first
scripts/deploy-test.sh, destroy-test.sh             seller-account test stack
scripts/e2e-test.py                                 end-to-end test of that stack, the way Teams drives it
scripts/test_*.py                                   offline unit tests of all of the above (make test-scripts)
Makefile                                            checks, publishing and test-stack targets
```

## The deployment template

`cloudformation/nlsql-ai-employee-ecs-fargate.yaml` creates one Fargate task behind an Application
Load Balancer and a CloudFront distribution, in a VPC and subnets the buyer already has, plus two
DynamoDB tables and two S3 buckets for the users' data. It is what the listing's **Deployment
templates** entry points at. Its environment is exactly the image's contract
(`docs/aws-marketplace.md`, *Environment and IAM contract*); `make template-invariants` checks the
names against that table when the app checkout is at `../ai-employee-aws`. The load-bearing parts:

- **The task role grants `aws-marketplace:RegisterUsage`.** AWS requires the call for monthly
  pricing too: it verifies at start-up that the account is subscribed. The image strips every other
  credential and endpoint source before it calls, pins the regional metering endpoint, and verifies
  the returned signature against the baked key with a nonce bound to the task ARN. A definitive
  refusal (not subscribed, access denied, an untrusted product code, a bad signature, ...) stops
  the task with container exit 78 and the line `NLSQL AI Employee stopped: <reason>`. The bot and
  the server each verify the result again before they serve.
- **`AWS_MARKETPLACE_PRODUCT_CODE`** may only repeat the baked code. Editing it in a downloaded
  template stops the task (entitlement exit 18) instead of running it without a subscription.
- **`ImageUri` has an `AllowedPattern`** that accepts only
  `709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-employee:<x.y.z>`. `deploy-test.sh`
  rewrites it by exact string to also accept the private test repository; `check-vars` fails when
  the two copies differ.
- **CloudFront terminates TLS**, because Bot Framework refuses plain HTTP and the load balancer
  has no certificate. With the default certificate CloudFront applies its **TLSv1** security
  policy whatever `MinimumProtocolVersion` says, so none is set and the guide says so; a TLS 1.2
  minimum needs a custom domain, which 1.0.1 does not offer. The distribution caches nothing (TTL
  0), forwards query strings, cookies and 8 headers (the 4 `Sec-WebSocket-*` among them, never
  `Referer`, since the viewer's URL carries its token), uses `PriceClass_100` and
  `OriginReadTimeout` 60. The CloudFront-to-ALB hop is HTTP, so Security Hub control CloudFront.9
  flags it; the guide records that as accepted.
- **The path allowlist.** The load balancer's security group admits port 80 only from the
  `com.amazonaws.global.cloudfront.origin-facing` prefix list (per-Region IDs in `RegionMap`; it
  counts as 55 inbound rules). The listener answers 403 by default. Three rules forward only
  requests carrying `X-Origin-Verify` with the stack's UUID (`!Select [2, !Split ['/', !Ref
  AWS::StackId]]`): core `/api/messages`, `/callback`, `/health`; assets `/static/*`; and, only
  with `GeminiFeatures` Enabled, the live view `/vnc/*`, `/websockify/*`, `/api/browser-use/*`.
  `/api/prompt`, `/proactive` and `/validate-vnc` are internal and never routed; the image's nginx
  serves nothing else either. The idle timeout is 3600 s for the live view's WebSockets. A v2
  coder adds priority 40 (`/mcp/oauth/callback`).
- **`/health` returns 503 until the subscription is verified**, and ECS replaces a task whose
  target never turns healthy (grace period 300 s). It is not what keeps an unentitled task from
  serving: with a single target the load balancer fails open. nginx's own 503 gate on every public
  route, and each program's verification of the signature, do that.
- **`DesiredCount` is fixed at 1.** Users' sessions, live browsers and the replies a paused
  browser agent waits for live in one process's memory, and Bot Framework carries no ALB cookie.
  Scaling is vertical (`TaskCpu` 2048 or 4096, `TaskMemory`, `EphemeralStorage` 30-200 GiB,
  default 2 vCPU / 8 GiB / 40 GiB). With 4096 CPU a deployment stops the old task first
  (`MinimumHealthyPercent` 0), because two 4 vCPU tasks exceed the default Fargate quota of 6 vCPU.
  The deployment circuit breaker rolls back a task that keeps stopping. Draining is 300 s.
- **Secrets.** The buyer's one Secrets Manager secret holds `AppPassword` (the bot's client
  secret) and, only with Gemini, `GeminiApiKey`; ECS injects them as `Secrets`, never as
  `Environment`. The stack generates only `ENCRYPTION_KEY` (64 lowercase hex), which encrypts the
  stored Microsoft 365 refresh tokens and signs the OAuth state. AWS makes a new secret version
  whenever `GenerateSecretString` changes, and a new key signs every user out, so that block is
  pinned by hash (`# >>> invariant: EncryptionKeySecret`, checked by `template-invariants`).
  `VNC_SECRET_KEY` and `INTERNAL_AUTH_SECRET` are made per task by the image's `startup.sh`;
  `template-invariants` refuses them, and every other name the contract says must not be set, in
  the template.
- **Gemini gating.** `GeminiFeatures` defaults to `Disabled`. Only under `GeminiEnabled` does the
  template add the `GEMINI_API_KEY` secret (a `ValueFrom` naming an absent JSON key would stop the
  task), `GEMINI_MODEL` (default `gemini-3.8-flash`), the live-view rule and the `BrowserViewUrl`
  output. Without it nothing is sent to Google, and the image offers neither the browser agent
  nor image reading. `BROWSER_USE_HOST` is set either way: the cards' images need it.
- **Retention.** The two tables (PAY_PER_REQUEST, SSE, point-in-time recovery) and the two buckets
  (SSE-S3, public access blocked, TLS only) are `RetainExceptOnCreate` with `UpdateReplacePolicy`
  Retain: deleting the stack keeps the users' data, and only a failed first creation removes it.
  The log group (14 days) and the generated key are deleted with the stack, which leaves the kept
  refresh tokens unreadable. The guide explains deleting one user's data and the leftovers.
- **Least privilege.** The task role has `RegisterUsage`; Bedrock `InvokeModel` and
  `InvokeModelWithResponseStream` on the `anthropic.*` foundation models and the account's
  `global.anthropic.*` profiles (the AI Agent's scope); DynamoDB item operations, `Query`, `Scan`
  and `DescribeTable` on the two tables; `s3:ListBucket` and object Get/Put/Delete on the two
  buckets. No `CreateTable`, `CreateBucket` or Marketplace `Subscribe`: enabling the model is a
  documented buyer prerequisite. The execution role reads exactly the two secrets (plus
  `kms:Decrypt` through Secrets Manager with `KmsKeyArn`). The container drops every Linux
  capability, has no ECS Exec and no init process (tini is the image's PID 1).
- **Region rule.** `AWS::Region` must be one of 16 Regions: the product's availability intersected
  with the Regions where every `LanguageModel` value is a global inference profile. It is the AI
  Agent's set, checked there on 2026-09-30 for the same 7 models; ap-east-1 and me-south-1 have no
  Bedrock endpoint. Container products have no Region setting in the portal, so this rule is the
  only Region control. **Re-run the check before each release**, in each of the 16 Regions:

  ```shell
  aws bedrock list-inference-profiles --region <region> --output text \
    --query "inferenceProfileSummaries[?starts_with(inferenceProfileId, 'global.anthropic.')].inferenceProfileId"
  aws ec2 describe-managed-prefix-lists --region <region> --output text --query 'PrefixLists[0].PrefixListId' \
    --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing
  ```

  The first must list all 7 `LanguageModel` values, the second must equal that Region's
  `RegionMap` entry.
- **`LanguageModel` values** are the AI Agent's 7 global inference-profile IDs, default
  `global.anthropic.claude-sonnet-5-5` (the agent loop and the unattended watches favour Sonnet).
  `llama-index-llms-bedrock-converse` 0.15.3 in the app's lock knows each one, and the app's
  `tests/unit/test_bedrock_models.py` covers each; `template-invariants --app-repo` compares the
  two lists.
- **Size.** `validate-template --template-body` takes at most 51,200 bytes; `template-invariants`
  fails the template above that.

Validate any change before publishing: `make validate` (needs AWS credentials; it runs
`template-invariants` and cfn-lint too).

## Makefile

| Target | What it does |
|---|---|
| `check-vars` | the template's `ImageUri` default is `$(IMAGE):$(VERSION)` and its product code is the baked one; with `../ai-employee-aws` (`APP_REPO`) checked out, which must be on `aws-marketplace-offer`, compares the app's baked code, its three trusted sets and the key hash; the release notes exist; the usage instructions and guide name the image and version; the copies in `deploy-test.sh`; then `guide-check` |
| `guide-check` | offline: the guide's Regions, model table, parameter and output tables equal the template's, its in-page links resolve, and (with the app) every message it quotes is in the app's Exact strings |
| `template-invariants` | offline: env names against the contract, Gemini gating, the path allowlist, the key schema, Regions against `RegionMap`, the size, the `ENCRYPTION_KEY` pin, the hardening, the model list |
| `test-scripts` | offline unit tests of every script here, each check failing on a planted defect |
| `validate` | `template-invariants`, `validate-template` (AWS credentials) and cfn-lint 1.57.2 |
| `push-guard` | offline: refuses `$(LOCAL_IMAGE)` unless it is the enforce build with the product's key and code, uid 10001, 8080 only, one linux/amd64 manifest, no sshd, no `.env*`, no stray PEM, no test files, no `change-me-in-production`, version `$(VERSION)`; records the accepted image ID |
| `check-tags` | fails if `$(VERSION)` already exists in the Marketplace repository |
| `login` | docker login to the Marketplace registry |
| `push` | `check-vars` (the app checkout required), `template-invariants`, `push-guard`, `check-tags`, `login`, then tags the guarded image ID and pushes it, then `check-pushed`. **Cannot be undone** |
| `push-test` | `push-guard`, then the same image to the seller's private `nlsql-ai-employee-test:$(TEST_TAG)` (created IMMUTABLE and tagged `nlsql:purpose=test` if missing) |
| `check-pushed` | the pushed tag (or `PUSHED_REF`) is one image manifest, not an index |
| `digest` | prints the pushed image digest |
| `template-url` | prints the commit-pinned template and guide URLs; fails on uncommitted changes to the template, `docs`, `listing` or `teams`, an unpushed commit, or a URL of 256 characters or more |
| `listing-check` | length and printable-ASCII checks on the listing texts (`<commit>` counted as 40 characters), and the usage instructions' Regions against the template |
| `no-secrets` | fails on credential-shaped strings (the app's secret names, Google API keys and Direct Line secrets included), AWS account IDs other than the Marketplace registry's and the placeholders, or a tracked `.env` or key file, in the working tree and in the branch's history (`git log -p`) |
| `deploy-test`, `destroy-test` | the seller-account test stack below (`STACK_NAME`, default `nlsql-ai-employee-test`); `GEMINI=on/off`, `LANGUAGE_MODEL`, `GEMINI_MODEL`; `PURGE_DATA=1` also deletes the kept tables and buckets |
| `e2e-test` | drives that stack the way Teams does (`scripts/e2e-test.py`); `E2E_ARGS`, `LOGIN_URL` |

## Publishing a version (seller)

1. In `ai-employee@aws-marketplace-offer` (`../ai-employee-aws`), on the day of the push: the
   release refresh (B11: `make scan` with the day's database, raise any new fixable HIGH/CRITICAL
   with reasons), then the release gate, building the name this Makefile pushes:

   ```shell
   make lock && git diff --exit-code requirements*.lock     # networked; the lock reproduces
   make lock-check build test e2e scan build-local smoke inspect size FRESH=1 IMAGE_LOCAL=ai-employee-aws:<version>
   ```

   For a new version, first bump the app Makefile's `VERSION` (the app's release step 5), or the
   push guard's version check fails. This produces `ai-employee-aws:$(VERSION)` (linux/amd64,
   enforce stage). `FRESH=1` rebuilds the
   Debian layers, so the image carries the day's Debian fixes.
2. Here: bump `VERSION`, the template's `ImageUri` default, the image and first line of
   `listing/usage-instructions.txt`, the guide's version and image, and `VERSION`'s default in
   `scripts/deploy-test.sh`; add `listing/release-notes/<version>.md`. Then:

   ```shell
   make check-vars template-invariants listing-check test-scripts no-secrets
   make validate                                             # AWS credentials
   ```

3. Commit and push this branch, then `make template-url` for the pinned URLs.
4. **Push the image**: `make push`, then `make digest`. A pushed tag is permanent, so confirm the
   version before running it.
5. **Add new version** in the portal (`listing/README.md`, *Version*), **Container image**
   delivery option:
   - version title and release notes from `listing/release-notes/<version>.md`;
   - delivery option title `NLSQL AI Employee on Amazon ECS (AWS Fargate)` and
     `listing/delivery-option-description.txt`;
   - the image `709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-employee:<version>`;
   - **Supported services: ECS** only (untick EKS);
   - usage instructions from `listing/usage-instructions.txt`, with `<commit>` replaced;
   - **Deployment templates**: the pinned `raw.githubusercontent.com` template URL, and the guide.
6. For the first version, also apply `listing/product/` under **Update product information**,
   and the offer's price and refund policy (`listing/README.md`, *Offer*).

Repository names in the Marketplace registry are permanent and unique across the seller's
products. `nlsql/ai-employee` is requested under **Server products, Request changes, Add
repositories** (prerequisite 2); `nlsql/ai-agent` and `nlsql/nlsql` belong to the other listings.

## Testing in the seller account

The app bakes public key v1 since B11, and `PUBKEY_SHA256` pins it, so `push-guard` accepts the
release image. An image that still bakes the placeholder key exits 19 on every ECS task (bad
signature, container 78), and `push-guard` refuses it.

**Subscribe the seller account first.** RegisterUsage entitles only an account that is subscribed
to the product, the seller's own account included. While the listing is Limited, the seller account
is on the offer's buyer allowlist: sign in to the console as that account, open **AWS Marketplace,
Discover products**, find NLSQL AI Employee, then **View purchase options** and **Subscribe**
(accept the offer's terms). Until then every task stops at start-up with `this AWS account is not
subscribed to NLSQL AI Employee ... (CustomerNotEntitledException) (exit 10).` and container exit
code 78, and the deployment circuit breaker rolls the stack back.

AWS requires a test task in us-east-1. Push the enforce image to a **private** test repository
(`<account>.dkr.ecr.us-east-1.amazonaws.com/nlsql-ai-employee-test:<tag>`) rather than the
Marketplace repository, so no tag is spent. Then:

```shell
make push-test                                                          # TEST_TAG, default $(VERSION)-rc1
make deploy-test ENV_FILE=<path-to-local-env-file> GEMINI=off
make e2e-test    ENV_FILE=<path-to-local-env-file>
make deploy-test ENV_FILE=<path-to-local-env-file> GEMINI=on            # same stack, switched
make e2e-test    ENV_FILE=<path-to-local-env-file> E2E_ARGS="--restart-probe"
make destroy-test PURGE_DATA=1                                          # stack, secret, capture function, data
```

Run the GEMINI=on stack once more with `STREAMING=true` in the env file (`deploy-test` again,
then `e2e-test`). A sign-in link copied from a real Teams login card is checked with
`make e2e-test ENV_FILE=<path> LOGIN_URL='<url>'`. The test targets take `STACK_NAME=<name>`
(default `nlsql-ai-employee-test`, or `$STACK_NAME` from the environment).

A failed creation rolls back and deletes the log group that says why; `DISABLE_ROLLBACK=1` keeps
the stack in `CREATE_FAILED` with its log group and stopped tasks, and the next `deploy-test`
rolls it back and deletes it before creating it again. The switch applies to a creation only:
CloudFormation refuses to replace the task definition in an update that cannot roll back
(`Replacement type updates not supported on stack with disable-rollback`), and a failed update
keeps the log group anyway. A test stack left in `UPDATE_FAILED` is rolled back before the update,
and one left in `ROLLBACK_COMPLETE` is deleted before deploying again.

The env file uses the application's env names (`connections__serviceConnection__settings__clientId`,
`...clientSecret`, `...tenantId`, `MODEL_NAME`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `STREAMING`,
`MODEL_MEMORY_TOKENS`, `SYSTEM_PROMPT`) and is never committed (`.env` and `.env.*` are ignored;
the scripts refuse a tracked file). `deploy-test` writes the secret through a 0600 temporary file,
deploys into the default VPC unless `VPC_ID`/`SUBNET_IDS` are set, and tags everything
`nlsql:purpose=test`; `destroy-test` refuses anything without that tag. The seller account's
Bedrock model access decides which `LanguageModel` a run can use: `LANGUAGE_MODEL=<allowed value>`
overrides it. A model is usable only once the account has its Bedrock Marketplace agreement:
`aws bedrock get-foundation-model-availability --model-id anthropic.<model>` must show
`agreementAvailability` `AVAILABLE`. Bedrock creates the agreement on a first call by an identity
allowed `aws-marketplace:Subscribe`, which the task role is not; without it the start-up probe can
still log `succeeded` (the first call gets through while Bedrock subscribes) and every later turn
is denied (`ConverseStream ... not authorized to perform the required AWS Marketplace actions`).
The env file's `STREAMING` sets the `Streaming` parameter.

`e2e-test` (`scripts/e2e-test.py`) exercises the stack the way Microsoft Teams does, with no Teams
tenant: a temporary Lambda function URL stands in for the Bot Connector, and each Teams-shaped
activity carries an Entra ID client-credentials token for the bot's own app. It checks `/health`
(with `--restart-probe`, the 503 sequence while the task is replaced), the edge 403s including
encoded traversal and a forged OAuth state, acks under 8 s, the welcome and `/help` for the
stack's features, exactly one answer per question, the storage round trip, login, attachments,
`/schedule`, identity (a Direct Line sender reusing the Teams user's ID gets a separate, empty
conversation), and log hygiene since the stack was created. With Gemini off, the live view must
be absent; with Gemini on, the live browser through the Teams dialog (`task/fetch`), WSS and RFB
through CloudFront kept idle for `--ws-idle` seconds (default 900, S4), 403 for missing, tampered
or stale tokens, close-session, and `read_image` on an RGB PNG, a 16-bit TIFF and a CMYK JPEG
with the real Gemini model (S7). The capture function is removed at the end, also when the run
fails, unless `--keep-capture`.

**S1, one real Converse call per model.** For each `LanguageModel` value the seller account has
enabled, `make deploy-test ENV_FILE=<path> LANGUAGE_MODEL=<model>` and look for
`NLSQL AI Employee: Bedrock probe for <model> succeeded.` in the log group (the probe makes one
16-token call with the task role as the server starts), then one e2e answer. Record a model the
account has not enabled as not tested, not as passed.

**T6, fail-closed.** "Not subscribed" shows on the seller account before it subscribes (seen on
2026-11-04: `CustomerNotEntitledException (exit 10)`, container exit code 78, the circuit breaker
rolled the stack back). After subscribing, test the access-denied half: run one task from the
stack's task definition with a task role that lacks `aws-marketplace:RegisterUsage`:

```shell
stack=nlsql-ai-employee-test
out() { aws cloudformation describe-stacks --stack-name "$stack" --output text \
          --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue"; }
cluster=$(out ClusterName); service=$(out ServiceName)
aws iam create-role --role-name "$stack-t6" --tags Key=nlsql:purpose,Value=test \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
role=$(aws iam get-role --role-name "$stack-t6" --query Role.Arn --output text)
td=$(aws ecs describe-services --cluster "$cluster" --services "$service" --query 'services[0].taskDefinition' --output text)
net=$(aws ecs describe-services --cluster "$cluster" --services "$service" --query 'services[0].networkConfiguration' --output json)
task=$(aws ecs run-task --cluster "$cluster" --launch-type FARGATE --task-definition "$td" \
  --network-configuration "$net" --overrides "{\"taskRoleArn\":\"$role\"}" \
  --tags key=nlsql:purpose,value=test --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$cluster" --tasks "$task"
aws ecs describe-tasks --cluster "$cluster" --tasks "$task" --query 'tasks[0].containers[0].exitCode'   # 78
aws logs filter-log-events --log-group-name "$(out LogGroupName)" --filter-pattern '"cannot start"' \
  --query 'events[].message' --output text
aws iam delete-role --role-name "$stack-t6"
```

It must stop with exit code 78 and log
`AWS Marketplace: NLSQL AI Employee cannot start: the ECS task role may not call aws-marketplace:RegisterUsage (AccessDeniedException) (exit 16).`
The configuration half of T6: register a copy of `$td` under a `-t6` family without the
`ENCRYPTION_KEY` secret (`describe-task-definition`, drop it, also strip the read-only fields
`taskDefinitionArn`, `revision`, `status`, `requiresAttributes`, `compatibilities`,
`registeredAt` and `registeredBy`, then `register-task-definition` with the test tag), run one
task from it with the normal role, and expect exit 78 and
`NLSQL AI Employee stopped: configuration error: ENCRYPTION_KEY is missing or shorter than 32 characters`;
then `deregister-task-definition` and only then `delete-task-definitions`. Both halves ran on
2026-11-04 (see the test record). Run T6 last: its tasks log `cannot start:` into the stack's log
group, which a later `e2e-test` counts as a boot error of the stack.

**Manual Teams test** (seller tenant, after `e2e-test` passes), the final step: paste the
`MessagingEndpoint` output into the Azure Bot, add the `OAuthRedirectUri` output to the app
registration, build the package with `teams/build-package.sh --stack <stack>` and install it from
the org catalog. Then, on Teams desktop, web and mobile:

- the welcome in personal scope starts `**Welcome! I'm NLSQL AI Employee.**` and mentions
  Microsoft 365; the card icons show in light and dark themes;
- `login` ends on the **Authentication Successful!** page; signing in with another account shows
  the "different Microsoft account" page;
- read and send mail, create and delete a calendar event, save and list a OneDrive file;
- an inline image and a shared file; `read_document` reads a real ODT, a DOCX and a dense PDF;
- `/schedule`, `/briefing` (a few minutes ahead) and `/watch emails on` deliver proactively;
- S5: these turns run on `@microsoft/agents-hosting` 1.9.1 with the msal-node 7 and zod 4
  overrides: JWT validation against the live keys, the client-secret token, streamed card edits,
  proactive delivery and invoke routing all work;
- with Gemini: **Open live browser** opens the Teams dialog (S4: from the bot's card and from a
  proactive card); the viewer page loads under its CSP (cursor, clipboard, the WebSocket);
  takeover and Stop work; two users at once each see only their own browser; through noVNC,
  Ctrl+P opens nothing, the live browser starts on a blank page, and `file:///proc/1/environ` and
  `http://169.254.170.2/` do not open; `read_image` uploads the annotated image to OneDrive with
  the folder's link;
- Azure's **Test in Web Chat** answers, without the Microsoft 365 features;
- afterwards the log group `/ecs/<stack>` shows no `AccessDenied` (least privilege), and any
  `NLSQL AI Employee: AWS calls use the ECS task role only; ignoring <NAMES>.` line lists names
  only, never a value.

### Test record

**1.0.1 as published, the buyer's path** (2026-11-04/05, seller account, us-east-1). The template
from the listing's pinned URL, byte for byte the one at `fd126d4`, with its default `ImageUri`: the
task pulled `709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-employee:1.0.1` (digest
`sha256:e62d180d…a36a`) through the account's subscription. Every parameter at its default but the
app IDs, `LanguageModel` Opus 5.5 and `Streaming`; Gemini off.

| Test | Status |
|---|---|
| Creation, entitlement, Bedrock probe | pass |
| e2e, `Streaming` false | pass, 101 of 101 |
| Stack update to `Streaming` true, e2e with `--restart-probe` | 107 of 108: only the restart probe's unobservable gate (entitled 5 s before UP) |
| Security probes (three forged or foreign tokens; a content-filtered prompt) | pass, 10 of 10 |
| T6, both halves | pass |

**1.0.1.** All runs on 2026-11-04 in the seller account, us-east-1, image `1.0.1-rc1` (digest
`sha256:e62d180d…a36a`), `LanguageModel` `global.anthropic.claude-opus-5-5`, Gemini off (not
tested: the owner's decision, the test key had no credits).

| Test | Status |
|---|---|
| T1: entitled run | pass |
| e2e, GEMINI=off, `Streaming` false | pass, 101 of 101 |
| e2e, GEMINI=off, `STREAMING=true` and `--restart-probe` | 107 of 108: Direct Line now gets exactly one answer, Teams answers are edited in place; the restart probe's `the gate was never seen` as for 1.0.0 (the new task was entitled 9 s before `/health` answered UP) |
| A turn whose answer has no text (Bedrock `content_filtered`, the `/proc/1/environ` probe prompt) | pass: the reply `I have no answer to that: ...` and the log line `NLSQL AI Employee: the language model returned no answer text.`, instead of `No response from server.` |
| Security probes: a Graph-audience token, an `alg none` JWT, a borrowed signature | pass: 401 each, no reply |
| T6, both halves | pass: both exit 78 with the exact lines (run last) |
| S1 | Opus 5.5 only, as for 1.0.0 |
| S5 and the manual Teams test | not run |

**1.0.0.** All runs on 2026-11-04 in the seller account, us-east-1, image `1.0.0-rc1` (digest
`sha256:d53f1458…02d1`, the same as the Marketplace `1.0.0`), `LanguageModel`
`global.anthropic.claude-opus-5-5`.

| Test | Status |
|---|---|
| T1: test stack in us-east-1, entitled run (`AWS Marketplace: entitlement verified for NLSQL AI Employee ...`) | pass, after the seller account subscribed to the Limited listing |
| T6: no `RegisterUsage` gives exit 78 and `(exit 16)`; no `ENCRYPTION_KEY` gives exit 78 and `configuration error` | pass: both exit 78 with the exact lines |
| e2e, GEMINI=off | pass, 101 of 101 (`Streaming` false) |
| e2e, GEMINI=on, with S4 (WSS idle 900 s) and S7 (the Gemini model's browser task and `read_image`) | not passed: the test Gemini key's project had no prepaid credits (`402 RESOURCE_EXHAUSTED`), so the browser session ended at once and `read_image` failed; everything else passed. Run again with a funded key |
| e2e, GEMINI=on, `STREAMING=true` and `--restart-probe` | streaming (with Gemini off): Teams answers are edited in place; Direct Line gets the `Thinking...` card and then one message per streamed chunk (`exactly one answer` fails, master's behaviour). Restart probe: the new task was entitled before `/health` answered UP (16 s); the gate's 503 window (about 1 s) is shorter than the load balancer's health check, so `the gate was never seen` |
| S1: one real Converse call per `LanguageModel` value | Opus 5.5: probe and answers pass. Not tested: the other six (no Bedrock agreement in the seller account) |
| Security probes beyond e2e: a Graph-audience token of the bot's app, an `alg none` JWT and a JWT with a borrowed signature | pass: 401 each, no reply. Prompts asking the agent to read `/proc/1/environ` or to browse to metadata addresses never reach a tool: Bedrock answers `content_filtered` |
| S5 and the manual Teams test | not run |
| Offline: `check-vars`, `template-invariants`, `listing-check`, `test-scripts`, `no-secrets`, cfn-lint | pass at this commit |

## Differences from the AI Agent packaging

- **Branch history.** Started from `AWS-ECS`, not from `AWS-ECS-AI-AGENT`; the AI Agent's tooling
  is ported as new files, and that branch is never modified.
- **No database or documents mode.** No `Mode`, no NLSQL settings and no documents bucket; instead
  two tables and two buckets the stack creates and keeps.
- **Secrets.** The buyer's secret holds `AppPassword` and the optional `GeminiApiKey`; the stack
  generates `ENCRYPTION_KEY`, pinned by hash; the per-task keys come from the image.
- **Optional Gemini.** `GeminiFeatures` gates the key, the model, the live-view route and output,
  and the Teams help tab.
- **A path allowlist** of three listener rules instead of one forward rule, an 8-header CloudFront
  allowlist with the WebSocket headers, and a 3600 s ALB idle timeout.
- **All Bot Service channels** are served; the Microsoft 365 features stay Teams-only, and a user
  ID belongs to the first channel that used it.
- **Sizing.** 2 vCPU / 8 GiB / 40 GiB by default, 4096 CPU deploys stop-first, no init process.
- **Default model** `global.anthropic.claude-sonnet-5-5` (the AI Agent's is Opus 5), from the same
  7-model list and 16 Regions.
- **TLS wording.** The guide states the default certificate's TLSv1 policy; the AI Agent's guide
  claims TLS 1.2+, which is wrong there too (out of scope for this branch).
- **Release guards** live in `scripts/push-guard.sh`, offline and unit-tested, with more rules
  (no sshd, no test files, no secret-named ENV, no VOLUME) and the accepted image ID recorded;
  `push-test` and `check-pushed` are new.
- **Offline checks** the AI Agent lacks: `template-invariants`, `guide-check` and `test-scripts`.
- **Teams package** built by `teams/build-package.sh` from the stack, with `validDomains` for the
  live browser's dialog; icons made from the bot avatar the image serves.

## Values copied across files

`check-vars` and the offline checks catch most of these copies; the rest go stale silently.

- **Version.** Checked: Makefile `VERSION`, the template's `ImageUri` default, the image and first
  line of `listing/usage-instructions.txt`, the guide's image and "Applies to" line, the existence
  of `listing/release-notes/<version>.md`, and `VERSION`'s default in `scripts/deploy-test.sh`.
  Not checked: the version table in `listing/README.md` and this README's identifiers table.
  `teams/manifest.template.json`'s `version` is the Teams app's own and unrelated.
- **Product code and public key.** Makefile `PRODUCT_CODE` and `PUBKEY_SHA256`, the template's env
  var, and the app's `marketplace/aws_marketplace.py`, `marketplace/aws_marketplace_verify.py`,
  `bot/entitlement.js` and PEM: `check-vars` with the app checkout, and `push-guard` in the image.
- **`ImageUri` `AllowedPattern`** and the `marketplace` string in `scripts/deploy-test.sh`
  (`check-vars`).
- **Regions (16).** The template's `SupportedRegion` list, its `AssertDescription` and `RegionMap`
  (`template-invariants`), the guide's list in section 3 (`guide-check`) and line 5 of
  `listing/usage-instructions.txt` (`listing-check`). This README and `listing/README.md` give
  only the count.
- **`LanguageModel` values**: the app's per-model test (`template-invariants --app-repo`), the
  guide's model table (`guide-check`), and the models named in the long description and the
  release notes (`test-scripts`).
- **Env names and exact strings**: the app's contract table (`template-invariants --app-repo`) and
  its Exact strings table (`guide-check --app-repo`, and `e2e-test.py`'s unit tests with
  `APP_REPO`).

## Outstanding before submission

- [ ] Rotate the bot's Direct Line secret (prerequisite 1): Azure Bot, Channels, Direct Line,
      regenerate.
- [x] Add the Marketplace repository `nlsql/ai-employee` to `prod-m37urirlvp7mw` (prerequisite 2):
      **Server products, Request changes, Add repositories**. The name is permanent and unique
      across the seller account.
- [x] Obtain RegisterUsage public key v1 for `prod-m37urirlvp7mw` from the product entity, the
      same way as for the AI Agent (prerequisite 3). App item **B11** bakes it as
      `marketplace/aws_marketplace_pubkey_v1.pem` and flips the placeholder tests; then set
      `PUBKEY_SHA256` in the Makefile and run `make check-vars`.
- [ ] Decide the monthly price and the refund wording (prerequisite 4); replace `<PRICE>` and the
      refund example in `listing/README.md`; **Update pricing terms** and **Update refund
      policy** once, after the seller's own test subscription and before **Update visibility**.
- [ ] Release-time dependency refresh (B11): on the day of `make push`, `make scan` in the app
      with that day's database, raise new fixable HIGH/CRITICAL findings (known at B15: pypdf,
      pyasn1, joserfc), `make build FRESH=1`, every suite again. Trivy cannot see Node 24, so
      check Node's security releases separately.
- [ ] Re-check the 16 Regions and the `RegionMap` prefix lists (commands above) on release day.
- [ ] Apply `listing/product/` under **Update product information**: title, descriptions with the
      two disclosures, highlights, keywords, support text, the logo.
- [ ] **Add new version** (ECS only): the fields in [Publishing](#publishing-a-version-seller)
      step 5 and `listing/README.md`, with the image pushed by `make push`.
- [ ] T1: test stack in us-east-1, entitled run.
- [ ] T6: both fail-closed runs ([Testing](#testing-in-the-seller-account)).
- [ ] `make e2e-test` on the final image with GEMINI=off, and with GEMINI=on including S4 (WSS idle
      at least 15 minutes) and S7 (`gemini-3.8-flash` with browser-use and `read_image`); once more
      with `STREAMING=true` and `--restart-probe`.
- [ ] S1: one real Converse call per `LanguageModel` value.
- [ ] S5: a real Teams turn on agents-hosting 1.9.1 with msal-node 7 and zod 4.
- [ ] The manual Teams test above, including Ctrl+P opening nothing and the blank start page in
      the noVNC dialog, and `read_document` on a real ODT, DOCX and dense PDF.
- [ ] Icons: `teams/color.png` and `teams/outline.png` are made from the bot avatar; replace them
      with the brand mark if wanted (same names and sizes, `teams/README.md`), and set the
      product logo.
- [ ] Ask AWS Seller Operations, with the exact disclosure text, before submission whether review
      accepts the optional Gemini key ("Optional, only with your own key: the Google Gemini API
      and websites the browser agent opens.") and the manual Azure Bot and Entra setup ("Microsoft
      Azure Bot Service (an Azure Bot in your Azure subscription; its Microsoft Teams channel is
      free)", shared with the AI Agent). Contingency for 1.0.1: the browser agent on browser-use's
      `ChatAnthropicBedrock` and `read_image` on Claude vision, both through Bedrock.
- [ ] Before pushing this branch (`template-url` needs the commit pushed), audit every commit in
      `AWS-ECS..AWS-ECS-AI-EMPLOYEE` for credentials; `make no-secrets` is the floor, not the audit.
- [ ] The app's open owner decisions (`docs/aws-marketplace.md`, *Open owner decisions*): the
      Direct Line welcome, and whether the system prompt names NLSQL AI Employee.
- [ ] v2: code mode needs the `nlsql-coder` image in its own Marketplace repository (proposed
      `nlsql/ai-employee-coder`, permanent once added), running as **its own ECS service** (Service
      Connect and a bearer token, never a same-task sidecar, which could reach the browser's
      loopback DevTools port) with its own `RegisterUsage` call. Every version then lists both
      images, and the template adds the coder service and listener priority 40.
