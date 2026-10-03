# NLSQL AI Agent on AWS — full deployment guide

The complete instructions for deploying NLSQL AI Agent from AWS Marketplace onto Amazon ECS
with AWS Fargate. The listing's **Usage instructions** field is capped at 4000 characters, so
it carries a condensed form and links here.

Applies to NLSQL AI Agent **1.0.0**, image
`709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-agent:1.0.0`, and the template
`cloudformation/nlsql-ai-agent-ecs-fargate.yaml` at the same commit as this document.

NLSQL AI Agent is a Microsoft Teams bot. It answers questions from your company documents in
Amazon S3, using Claude on Amazon Bedrock, and in **DocsAndSql** mode it also answers
natural-language questions from your SQL database through NLSQL, with charts and tables.

**Contents**

1. [What gets deployed](#1-what-gets-deployed)
2. [What it costs](#2-what-it-costs)
3. [Before you start](#3-before-you-start)
4. [Step 1 — Store your credentials](#step-1--store-your-credentials)
5. [Step 2 — Launch the stack](#step-2--launch-the-stack)
6. [Step 3 — Point your Teams bot at the deployment](#step-3--point-your-teams-bot-at-the-deployment)
7. [Step 4 — Install the bot in Teams](#step-4--install-the-bot-in-teams)
8. [Step 5 — Verify](#step-5--verify)
9. [Step 6 — Load your documents](#step-6--load-your-documents)
10. [Database settings by engine](#database-settings-by-engine)
11. [Troubleshooting](#troubleshooting)
12. [Operating the deployment](#operating-the-deployment)
13. [External dependencies](#external-dependencies)
14. [Support](#support)

---

## 1. What gets deployed

```
Microsoft Teams ──► Microsoft Bot Connector
                          │  HTTPS, Bot Framework token
                          ▼
CloudFront distribution           ← HTTPS (TLS 1.2+), CloudFront's own certificate
      │  HTTP inside AWS, adds X-Origin-Verify: <this stack's unique ID>
      ▼
Application Load Balancer :80     ← admits CloudFront only; 403 without that header
      │  HTTP :8080
      ▼
ECS Fargate task (exactly one)    ← NLSQL AI Agent container, uid 10001
      │
      ├──► AWS Marketplace Metering Service   (subscription check, once per task)
      ├──► Amazon Bedrock                      (Claude, global inference profile)
      ├──► Amazon S3 documents bucket          (read-only)
      ├──► Microsoft Bot Connector             (replies) and Teams/SharePoint file hosts
      ├──► your database                       (DocsAndSql only)
      ├──► api.nlsql.com                       (question text → SQL, DocsAndSql only)
      └──► cdn.plot.ly                         (map outlines for map answers, DocsAndSql only)
```

| Resource | Notes |
|---|---|
| ECS cluster and Fargate service | exactly one task, rolling deployments with automatic rollback |
| Task definition | linux/amd64, 4 vCPU / 8 GiB / 40 GiB disk by default, port 8080 |
| Application Load Balancer | internet-facing, HTTP 80, idle timeout 120 s; default action 403 |
| Listener rule | forwards only requests carrying this stack's CloudFront origin header |
| Target group | port **8080**, health check `/health` every 30 s, 300 s draining |
| CloudFront distribution | HTTPS on the default `*.cloudfront.net` certificate, nothing cached |
| S3 bucket | only if you do not name an existing one; private, SSE-S3, TLS-only, kept on delete |
| ExecutionRole | pulls the image, writes logs, reads your one secret |
| TaskRole | RegisterUsage, read-only access to the bucket, Claude on Bedrock |
| Security groups | load balancer: 80 from CloudFront's origin-facing addresses only (AWS-managed prefix list); task: 8080 from the load balancer only |
| CloudWatch log group | `/ecs/<stack-name>`, 14-day retention |

Inside the container, nginx on 8080 is the only listening port. It routes `/api/messages` to
the Teams bot, serves chart and CSV links under `/bot/static`, and answers `/health` and
`/ready`. The bot, the NLSQL service and the document service listen on 127.0.0.1 only.

**Why CloudFront is in the path.** Microsoft Bot Framework refuses a plain HTTP messaging
endpoint, and the load balancer has no certificate of its own. CloudFront supplies a trusted
HTTPS endpoint with no custom domain or certificate to buy. Every TTL is zero; it exists only to
terminate TLS.

**How the load balancer is kept behind CloudFront.** Its security group admits port 80 only from
CloudFront's origin-facing addresses (the AWS-managed prefix list
`com.amazonaws.global.cloudfront.origin-facing`), so a request sent straight to the load
balancer's DNS name gets no answer. CloudFront also adds a header whose value is this stack's
unique ID, and the listener forwards only requests that carry it, so a request through any other
CloudFront distribution gets `403 Forbidden`. The hop from CloudFront to the load balancer is HTTP
inside AWS, because the load balancer has no certificate of its own. AWS Security Hub control
**CloudFront.9** reports the distribution's http-only origin for that reason; for this design the
finding is expected and accepted.

**Why port 8080.** The container runs as an unprivileged user (uid 10001), which cannot bind
port 80, and Fargate cannot grant `NET_BIND_SERVICE` back. If you write your own task definition,
map **8080**.

**Why exactly one task.** Chart and CSV links point at files on the task's own disk, NLSQL keeps
conversation state in the process, and Bot Framework carries no load-balancer cookie that could
pin a conversation to one of several tasks. Scale up by giving the task more CPU and memory
([Scaling](#scaling)).

**Why a turn gets an instant reply.** The bot acknowledges each Teams message at once and sends
the answer when it is ready, so a long document search is not cut off by Bot Framework's 15 s or
CloudFront's 60 s limits.

**How a message is answered in DocsAndSql mode.** A message with a file attached (a file sent in
Teams, or a named attachment in a [supported format](#step-6--load-your-documents)) is answered
from that file. Every other message goes to NLSQL first, including one with a pasted image or
screenshot: images are not read. When NLSQL writes no SQL (the question is not about your data) or
cannot reach the database or the NLSQL API, your documents answer instead. If NLSQL has not
answered within 330 seconds, the bot replies
`Your data question is taking longer than expected. Please try again in a few minutes.` and does
not ask the documents.

**Who can call the bot.** `/api/messages` accepts requests from the Microsoft Bot Connector (Teams,
and **Test in Web Chat** in the Azure portal) and from the bot's own app registration. Another app
in your Microsoft Entra tenant that obtains a token for the bot gets `401`, and the log shows
`[auth] rejected a request from an unexpected caller (iss=..., appid=...)`.

## 2. What it costs

The AWS Marketplace software charge is per running ECS task-hour, metered by AWS through
`RegisterUsage`. The free trial covers the software charge for **one task for 30 days**. It does
not cover the AWS infrastructure below or Amazon Bedrock usage, which AWS bills to your account
from the start.

Approximate monthly cost of the infrastructure at the defaults, running continuously in
us-east-1 (730 hours):

| Resource | Rough monthly cost |
|---|---|
| Fargate task, 4 vCPU / 8 GiB (default) | ~$144 ($0.04048 per vCPU-hour, $0.004445 per GiB-hour) |
| Fargate task, 2 vCPU / 4 GiB (smallest allowed) | ~$72 |
| Fargate ephemeral storage above the free 20 GiB (default 40 GiB) | ~$1.60 |
| Application Load Balancer | ~$16.40 plus LCU charges (~$6 at light traffic) |
| Public IPv4 addresses (load balancer in two AZs, plus the task) | ~$11 ($0.005 per address-hour) |
| CloudFront (includes chart and CSV downloads) | the always-free tier covers 1 TB and 10 million requests a month; then ~$0.085/GB |
| S3 | ~$0.023 per GB-month, plus requests; every task start lists and reads every document |
| Secrets Manager | ~$0.40 per secret, plus $0.05 per 10,000 API calls |
| CloudWatch Logs | ~$0.50 per GB ingested, ~$0.03 per GB-month stored |
| NAT gateway (only if your network routes through one) | ~$33 plus $0.045/GB |
| **Amazon Bedrock** | per input and output token, at the chosen model's price |

**Amazon Bedrock is usually the variable part.** Each document question sends the retrieved
passages plus the question to Claude, and answers can use up to 16,000 output tokens. Opus models
cost several times more per token than Sonnet, and Haiku the least. The template calls Claude
through **global cross-Region inference profiles** (`global.anthropic.…`), billed at the
global-inference price for that model in the Region you deploy in. See the
[Amazon Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/) for current figures.

These figures are indicative; check the AWS pricing pages for your Region.

**Outside AWS.** The Azure Bot resource lives in your own Microsoft Azure subscription, where its
standard Microsoft Teams channel is free; Microsoft 365 or Teams licences are your own. The NLSQL
API token (DocsAndSql) is free.

**Service quotas.** Check these before launching, in **Service Quotas** in the console:

- **Fargate On-Demand vCPU resource count.** The task uses 4 vCPU, and every deployment briefly
  runs a second task, so you need at least **8 vCPU** (4 with `TaskCpu=2048`). New accounts
  can start lower.
- **Amazon Bedrock tokens and requests per minute** for the chosen model's global
  cross-Region inference. Low quotas show up as throttling errors in the logs. Request an
  increase, or choose a smaller model.
- **Application Load Balancers per Region** (default 50), **VPC security groups** and **inbound
  rules per security group** (default 60; the load balancer's one rule references CloudFront's
  origin-facing prefix list, which counts as 55 rules), and **CloudFront distributions per
  account**.

If stack creation fails with a quota error, request an increase and create the stack again.

## 3. Before you start

**Subscribe first, and deploy into the same AWS account.** The container calls `RegisterUsage`
at start-up and stops if the account is not entitled. This is also how AWS meters your hourly
usage.

**Pick one supported Region and stay in it.** The template accepts us-east-1, us-east-2,
us-west-1, us-west-2, ca-central-1, sa-east-1, eu-west-1, eu-west-2, eu-west-3, eu-central-1,
eu-north-1, ap-south-1, ap-northeast-1, ap-northeast-2, ap-southeast-1 and ap-southeast-2:
the Regions where the product is offered and every listed Claude model is available through a
global inference profile. The Secrets Manager secret, an existing documents bucket and your
Bedrock model enablement must all be in that Region. Leave the
`709825985650.dkr.ecr.us-east-1.amazonaws.com` image path as written: AWS Marketplace serves it
to every Region.

**Enable the Claude model in Amazon Bedrock.** The task role can call Claude but, by design,
cannot subscribe to it. Someone with AWS Marketplace permissions (`aws-marketplace:Subscribe`
and `aws-marketplace:ViewSubscriptions`) and Bedrock access does this once per model, in the
deployment's account and Region:

1. Open the **Amazon Bedrock console** in your Region, choose **Model catalog** in the left
   navigation, and filter by provider **Anthropic**.
2. Choose the model you will set as `LanguageModel` (see the table below).
3. If asked, submit the **Anthropic use case details** form. Anthropic requires it once per
   account (or once for the whole organisation from the management account).
4. Choose **Open in playground** and send one short prompt. The first call subscribes the
   account to the model; it can take a few minutes to become active.

If your account still shows the older **Model access** page, request access to the model there
instead. Then check from the CLI:

```shell
aws bedrock get-foundation-model-availability \
  --model-id anthropic.claude-opus-5 --region <region>
# expect "authorizationStatus": "AUTHORIZED", "entitlementAvailability": "AVAILABLE"
# and "regionAvailability": "AVAILABLE"

aws bedrock-runtime converse --region <region> \
  --model-id global.anthropic.claude-opus-5 \
  --messages '[{"role":"user","content":[{"text":"Reply with OK"}]}]' \
  --inference-config maxTokens=16
```

| `LanguageModel` value | Model | `--model-id` for the availability check |
|---|---|---|
| `global.anthropic.claude-opus-5` (default) | Claude Opus 5 | `anthropic.claude-opus-5` |
| `global.anthropic.claude-opus-5-5` | Claude Opus 5.5 | `anthropic.claude-opus-5-5` |
| `global.anthropic.claude-opus-4-8` | Claude Opus 4.8 | `anthropic.claude-opus-4-8` |
| `global.anthropic.claude-sonnet-5` | Claude Sonnet 5 | `anthropic.claude-sonnet-5` |
| `global.anthropic.claude-sonnet-5-5` | Claude Sonnet 5.5 | `anthropic.claude-sonnet-5-5` |
| `global.anthropic.claude-sonnet-4-6` | Claude Sonnet 4.6 | `anthropic.claude-sonnet-4-6` |
| `global.anthropic.claude-haiku-4-5-20251001-v1:0` | Claude Haiku 4.5 | `anthropic.claude-haiku-4-5-20251001-v1:0` |

**A Microsoft Azure subscription and a Microsoft Teams bot registration.** Create an **Azure
Bot** resource for the Microsoft 365 tenant you use for Teams, with a **single-tenant** app
registration. The Azure Bot resource lives in a **Microsoft Azure subscription** of yours; the
standard Microsoft Teams channel is free. You need three values from it: the **Microsoft App
ID**, a **client secret** (note its expiry date; see [Rotating credentials](#rotating-credentials))
and the **tenant ID**. Create it as an Azure Bot, not a bare Entra app registration: only the
Azure Bot has the messaging-endpoint field used in Step 3.

**DocsAndSql mode only: an NLSQL API token and a database.** Generate the token self-service at
https://nlsql.com; it is free, with no separate purchase or approval. The NLSQL API receives the
text of each question and returns SQL; it never receives database credentials, query results or
documents. Use a read-only database user: the product runs the SQL the NLSQL API returns, so the
user's permissions are what limits it. The database must be reachable from the subnets below. See
[Database settings by engine](#database-settings-by-engine).

**Networking.** Two or more **public** subnets (route to an internet gateway) in different
Availability Zones of one VPC. The load balancer is internet-facing, and by default the task runs
in the same subnets with a public IP (`AssignPublicIp=ENABLED`) to reach Bedrock, the Metering
Service, Microsoft and api.nlsql.com through the internet gateway.

**If your database only admits known IP addresses** (an Azure SQL or other managed-database
firewall, a Snowflake network policy, an on-premises firewall), that public IP changes at every
deployment. Instead pass **`TaskSubnetIds`**: two or more private subnets whose route goes through
a NAT gateway, and allow the NAT gateway's Elastic IP in the database's firewall. The load
balancer stays in `SubnetIds`. The private subnets must be in the same VPC, and only in
Availability Zones where `SubnetIds` also has a subnet: the load balancer cannot reach a task in
any other zone, so that task never becomes healthy.

**IAM permissions for whoever launches the stack:**

- creating IAM roles (`iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`,
  `iam:GetRole`, `iam:PassRole`, and the matching delete actions): this is what
  `--capabilities CAPABILITY_IAM` acknowledges;
- if the stack creates the documents bucket: `s3:CreateBucket`, `s3:PutEncryptionConfiguration`,
  `s3:PutBucketPublicAccessBlock`, `s3:PutBucketOwnershipControls`, `s3:PutBucketPolicy`;
- `secretsmanager:CreateSecret` for Step 1;
- the usual ECS, Elastic Load Balancing, EC2 (security groups), CloudFront and CloudWatch Logs
  create, describe and delete permissions.

---

## Step 1 — Store your credentials

One AWS Secrets Manager secret, JSON, in the stack's Region.

| Key | Mode | Value |
|---|---|---|
| `AppPassword` | always | the Azure Bot's client secret |
| `ApiToken` | DocsAndSql | your NLSQL API token |
| `DbPassword` | DocsAndSql | the database user's password |

Writing the JSON to a file keeps the values out of your shell history and process list:

```shell
cat > secret.json <<'EOF'
{"AppPassword":"<bot-client-secret>","ApiToken":"<nlsql-api-token>","DbPassword":"<db-password>"}
EOF
aws secretsmanager create-secret \
  --name nlsql-ai-agent/credentials \
  --region <region> \
  --secret-string file://secret.json
rm secret.json
```

For DocsOnly, `{"AppPassword":"<bot-client-secret>"}` is enough. To encrypt the secret with your
own KMS key, add `--kms-key-id <kms-key-arn>` and pass the same ARN as `KmsKeyArn` in Step 2.

Note the returned ARN; Step 2 needs it. ECS injects these values into the container at task start;
they never appear in the task definition, the image, the template or the logs.

## Step 2 — Launch the stack

From the listing, choose **Continue to Subscribe**, then **Continue to Configuration**, pick the
**Amazon ECS** delivery option and the version, and **Continue to Launch**. The launch page links
the deployment template. Download it, then in the CloudFormation console choose **Create stack**,
**With new resources**, **Upload a template file**. For the first launch, under **Stack failure
options** choose **Preserve successfully provisioned resources**: if the first task cannot
start, the stack then keeps its log group, cluster and stopped tasks so you can read why (see
[If creation fails](#if-creation-fails)). Or use the CLI, where `--disable-rollback` does the same:

```shell
aws cloudformation create-stack \
  --stack-name nlsql-ai-agent \
  --template-body file://nlsql-ai-agent-ecs-fargate.yaml \
  --capabilities CAPABILITY_IAM \
  --disable-rollback \
  --region <region> \
  --parameters \
    ParameterKey=Mode,ParameterValue=DocsAndSql \
    ParameterKey=VpcId,ParameterValue=<vpc-id> \
    ParameterKey=SubnetIds,ParameterValue='<subnet-id-1>\,<subnet-id-2>' \
    ParameterKey=CredentialsSecretArn,ParameterValue=<secret-arn> \
    ParameterKey=MicrosoftAppId,ParameterValue=<microsoft-app-id> \
    ParameterKey=MicrosoftAppTenantId,ParameterValue=<tenant-id> \
    ParameterKey=LanguageModel,ParameterValue=global.anthropic.claude-opus-5 \
    ParameterKey=DatabaseType,ParameterValue=PostgreSQL \
    ParameterKey=DataSource,ParameterValue=<database-host> \
    ParameterKey=DbName,ParameterValue=<database-name> \
    ParameterKey=DbUser,ParameterValue=<database-user>
```

For DocsOnly, pass `Mode=DocsOnly` and leave out the database parameters.

### Every parameter

| Parameter | Required | Default | Meaning |
|---|---|---|---|
| `Mode` | no | `DocsAndSql` | `DocsAndSql`: NLSQL first, documents as the fallback. `DocsOnly`: documents only |
| `VpcId` | yes | — | VPC to deploy into |
| `SubnetIds` | yes | — | two or more public subnets in different AZs |
| `AssignPublicIp` | no | `ENABLED` | keep `ENABLED` in public subnets; ignored with `TaskSubnetIds` |
| `TaskSubnetIds` | no | empty | private subnets behind a NAT gateway for the task, for a database that admits known IPs only; same VPC, only in Availability Zones that also have one of `SubnetIds` |
| `MicrosoftAppId` | yes | — | Azure Bot application (client) ID |
| `MicrosoftAppTenantId` | yes | — | Entra tenant ID of the bot |
| `CredentialsSecretArn` | yes | — | full ARN of the secret from Step 1 |
| `KmsKeyArn` | no | empty | customer managed KMS key of the secret or of an existing bucket |
| `LanguageModel` | no | `global.anthropic.claude-opus-5` | Claude model; must be enabled in Bedrock |
| `LanguageModelContext` | no | empty | your own system prompt for document answers |
| `Streaming` | no | `false` | `true` shows document answers in Teams as they are generated |
| `ExistingDocumentsBucket` | no | empty | an S3 bucket in this Region with your documents; empty creates one |
| `DatabaseType` | DocsAndSql | empty | `MSSQL`, `MySQL`, `PostgreSQL`, `Redshift`, `Snowflake` |
| `DataSource` | DocsAndSql | empty | database host; see [the engine table](#database-settings-by-engine) |
| `DbName` | DocsAndSql | empty | database name |
| `DbPort` | no | empty | MySQL and PostgreSQL only; empty means 3306 / 5432 |
| `DbSchema` | Snowflake | empty | Snowflake schema; other engines use the user's default schema |
| `DbUser` | DocsAndSql | empty | database user; use a read-only one, since the product runs the SQL the NLSQL API returns |
| `Warehouse` | Snowflake | empty | Snowflake warehouse |
| `TaskCpu` | no | `4096` | `2048` or `4096` CPU units |
| `TaskMemory` | no | `8192` | MiB; 4096–16384 with 2048 CPU, 8192–30720 with 4096 CPU |
| `EphemeralStorage` | no | `40` | task disk in GiB, 21–200 |
| `ImageUri` | no | the 1.0.0 image | leave alone unless pinning another published version |

The template refuses to create a stack in an unsupported Region, in DocsAndSql mode without the
database parameters, for Snowflake without `Warehouse` and `DbSchema`, or with a CPU and memory
pair Fargate does not offer.

### Wait for it

Creation takes roughly 15–25 minutes: CloudFront, then the first task starting and verifying the
subscription. The outputs exist only once the stack completes:

```shell
aws cloudformation wait stack-create-complete --stack-name nlsql-ai-agent --region <region>
```

`CREATE_COMPLETE` does not mean your documents are indexed: indexing starts once the subscription
is verified and goes on after the stack completes. `/ready` (Step 5) says when it is done.

To watch the task start, follow its logs from another terminal once the log group exists (a
minute or two after creation starts). The output stays on your screen even if the stack rolls back
and deletes the log group:

```shell
aws logs tail /ecs/nlsql-ai-agent --follow --region <region>
```

### If creation fails

The first failure is at the bottom of:

```shell
aws cloudformation describe-stack-events --stack-name nlsql-ai-agent --region <region> \
  --query "StackEvents[?contains(ResourceStatus, 'FAILED')].[LogicalResourceId, ResourceStatusReason]" \
  --output table
```

`ECS Deployment Circuit Breaker was triggered` means the task kept stopping. With rollback disabled
the stack stops in `CREATE_FAILED` and keeps its resources: read the stopped tasks' reasons (ECS
lists stopped tasks for about an hour) and the logs ([Monitoring](#monitoring)), and match them in
[Troubleshooting](#troubleshooting). If the stack rolled back instead (the default), it ends in
`ROLLBACK_COMPLETE`: its cluster, stopped tasks and log group are gone, and a stack in that state
can only be deleted. Either way, fix the cause, delete the stack, and create it again.

## Step 3 — Point your Teams bot at the deployment

Read the stack outputs:

```shell
aws cloudformation describe-stacks --stack-name nlsql-ai-agent --region <region> \
  --query 'Stacks[0].Outputs' --output table
```

| Output | What it is |
|---|---|
| `MessagingEndpoint` | `https://<id>.cloudfront.net/api/messages`: **paste this into Azure** |
| `HealthCheck` | `/health`: 200 once the subscription is verified, 503 before |
| `ReadinessCheck` | `/ready`: 200 once documents are indexed, 503 with a status otherwise |
| `DocumentsBucket` | where your documents go |
| `ClusterName`, `ServiceName` | for `aws ecs update-service` |
| `LogGroupName` | container logs |
| `TaskSecurityGroupId` | the task's security group, to allow in a database's security group |

Then, in the Azure portal:

1. Open your **Azure Bot** resource.
2. Go to **Settings**, **Configuration**.
3. Put the `MessagingEndpoint` value into **Messaging endpoint** and choose **Apply**.
4. Go to **Channels**, add **Microsoft Teams**, and accept the terms.

The CloudFront domain is generated per stack. If you delete and recreate the stack, update the
messaging endpoint and the Teams package again.

## Step 4 — Install the bot in Teams

The repository's [`teams/`](../teams/README.md) folder holds a manifest template and icons. The
manifest uses **personal scope** with `supportsFiles: true`: Teams delivers files to a bot only in
one-to-one chats. Build the package from your stack outputs, with no values typed by hand:

```shell
STACK=nlsql-ai-agent; REGION=<region>
APP_ID=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Parameters[?ParameterKey=='MicrosoftAppId'].ParameterValue" --output text)
DOMAIN=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='MessagingEndpoint'].OutputValue" --output text | cut -d/ -f3)
sed -e "s/<MicrosoftAppId>/$APP_ID/g" -e "s/<CloudFrontDomain>/$DOMAIN/g" \
  teams/manifest.template.json > teams/manifest.json
zip -j nlsql-ai-agent-teams.zip teams/manifest.json teams/color.png teams/outline.png
```

Upload the zip in Teams (**Apps**, **Manage your apps**, **Upload an app**), or have a Teams
administrator publish it for the organisation in the Teams admin center (**Teams apps**,
**Manage apps**, **Upload new app**).

## Step 5 — Verify

**The subscription verified and the task is serving.** `HealthCheck` returns 200:

```shell
curl -sS -w ' %{http_code}\n' https://<cloudfront-domain>/health
# {"status":"UP"} 200     (503 {"status":"STARTING"} until the subscription is verified)
```

**Documents are indexed.** `ReadinessCheck` returns 200:

```shell
curl -sS -w ' %{http_code}\n' https://<cloudfront-domain>/ready
# {"status":"ready"} 200
```

Other statuses, all with 503: `waiting_for_entitlement`, `initializing` (indexing in progress),
`no_documents` (nothing was indexed: the bucket is empty, or the task cannot read it; see
[Troubleshooting](#troubleshooting)) and `error` (see the logs).

**The log confirms metering.** Look for the entitlement line at task start:

```shell
aws logs filter-log-events --log-group-name /ecs/nlsql-ai-agent --region <region> \
  --filter-pattern '"entitlement verified"' --query 'events[].message' --output text
```

`AWS Marketplace: entitlement verified for NLSQL AI Agent (product …, region …)` means
`RegisterUsage` succeeded and AWS is metering this task.

**Ask a real question.** Message the bot in Teams, and send it a file to ask about. This is the
only end-to-end proof that Teams, CloudFront, the container, Bedrock and your database work
together. The load balancer's own DNS name not answering is expected: its security group admits
CloudFront only.

## Step 6 — Load your documents

Documents are read from the bucket and indexed when a task starts. After adding, changing or
removing documents, start a new task:

```shell
aws s3 sync <local-folder> s3://<DocumentsBucket>/ --region <region>
aws ecs update-service --cluster <ClusterName> --service <ServiceName> \
  --force-new-deployment --region <region>
aws ecs wait services-stable --cluster <ClusterName> --services <ServiceName> --region <region>
curl -sS -w ' %{http_code}\n' https://<cloudfront-domain>/ready   # repeat until {"status":"ready"} 200
```

The new task takes over as soon as its health check passes, which does not wait for indexing:
until indexing finishes, it answers document questions with a short "still initializing" reply.
Until the old task has drained, `/ready` may still be answered by the old one, so poll it only
after `services-stable` returns.

Supported formats, in the bucket and as files sent to the bot:

- PDF (`.pdf`);
- Word (`.docx`, tables included);
- Excel (`.xlsx`, all worksheets);
- OpenDocument (`.odt`, `.ods`);
- text files (`.txt`, `.csv`, `.md`, `.json`, `.jsonl`, `.html`, `.htm`), in UTF-8 or a Western
  single-byte encoding (Windows-1252, Latin-1).

The older Excel format `.xls` is not supported: save it as `.xlsx`. A scanned PDF with no text
layer cannot be read either. A document in the bucket that cannot be read is skipped, and the log
shows `Talk-to-Docs: could not read <name> (<ErrorName>); skipped.` Files sent to the bot in a Teams
chat (up to 25 MB) are answered from directly and are not added to the bucket. If the bot cannot
read one, it replies
`I could not read the attached file <name>. Supported formats: PDF, Word (.docx), Excel (.xlsx), OpenDocument (.odt, .ods) and text files (.txt, .csv, .md, .json, .html).`
Images and screenshots pasted into a message are not read.

---

## Database settings by engine

DocsAndSql only.

| Parameter | MSSQL | MySQL | PostgreSQL | Redshift | Snowflake |
|---|---|---|---|---|---|
| `DataSource` | host, or **`host,port`** for a non-default port | host | host | cluster endpoint host | **account identifier** |
| `DbPort` | ignored: put the port in `DataSource` | empty = 3306 | empty = 5432 | ignored: always 5439 | ignored |
| `DbName` | required | required | required | required | required |
| `DbSchema` | ignored | ignored | ignored | ignored | **required** |
| `Warehouse` | ignored | ignored | ignored | ignored | **required** |
| `DbUser` / `DbPassword` | required | required | required | required | required |
| TLS | ODBC Driver 17 does not request encryption; traffic is encrypted only when the server forces it (Azure SQL, or RDS with `rds.force_ssl=1`) | TLS 1.2 requested; the server certificate is not verified | TLS when the server offers it (`sslmode=prefer`); the certificate is not verified | TLS required, server certificate verified | HTTPS, certificate verified |

A Redshift cluster on a port other than 5439 is not supported in this version.

The database must accept connections from the task. For Amazon RDS or Redshift in the same VPC,
add a rule to the database's security group allowing the stack's task security group (the
`TaskSecurityGroupId` output) on the database port. For a database outside AWS that admits known IP
addresses only, run the task in private subnets behind a NAT gateway (`TaskSubnetIds`) and allow
the NAT gateway's Elastic IP; the task's own public IP changes at every deployment. A refusal
shows in the logs as `cannot connect to the <engine> database (...)`, for Azure SQL with error
40615 and the refused address. Where the engine's TLS is weak, keep the database inside the VPC or use a private
connection.

## Troubleshooting

### First launch

| Symptom | Cause | Fix |
|---|---|---|
| Stack stuck in `CREATE_IN_PROGRESS` on `Service`, then `ECS Deployment Circuit Breaker was triggered` | the task keeps stopping; usually one of the rows below | see [If creation fails](#if-creation-fails). With rollback disabled, read the stopped reason and the logs (ECS console, cluster, service, **Tasks**, **Stopped**). After a rollback (`ROLLBACK_COMPLETE`) nothing is left to read: delete the stack and create it again with rollback disabled, or while following the logs |
| Task stops with exit code 78; log shows `AWS Marketplace: NLSQL AI Agent cannot start: this AWS account is not subscribed` (`CustomerNotEntitledException`) | the deploying account is not subscribed | subscribe in this account and wait a few minutes, then delete the stack and create it again |
| Task stops with exit code 78 and another `cannot start` reason | see the reason: access denied (task role lacks `aws-marketplace:RegisterUsage`), no credentials, unsupported Region, product code edited | delete the stack and create it again from the unmodified template, in a supported Region |
| Stopped reason `ResourceInitializationError: unable to retrieve secret` or `did not contain json key` | the secret is in another Region, lacks a key (`ApiToken`/`DbPassword` in DocsAndSql), or uses a KMS key without `KmsKeyArn` | fix the secret or pass `KmsKeyArn`, then delete the stack and create it again |
| Stack fails: "not supported in this Region" | the template's Region rule | deploy in a listed Region |
| Stack fails on vCPU limits, or a deployment never starts its second task | Fargate On-Demand vCPU quota | request at least 8 vCPU |
| Stack fails on `LoadBalancerSecurityGroup` with a rules-per-security-group limit | CloudFront's prefix list counts as 55 inbound rules, more than this account's quota | raise **Inbound or outbound rules per security group** to at least 55 (the default is 60) |

### Running stack

| Symptom | Cause | Fix |
|---|---|---|
| Document answers fail with `Sorry — NLSQL AI Agent could not answer right now.`; at task start the log shows `NLSQL AI Agent: Bedrock probe for <model> failed (AccessDeniedException: ...)` | the Claude model is not enabled in this account and Region | enable it as in [Before you start](#3-before-you-start); the task role deliberately cannot subscribe |
| Logs show `ThrottlingException` or "Too many tokens"; slow or failed answers | Bedrock tokens-per-minute or requests-per-minute quota | request a quota increase for the model's global cross-Region inference, or choose a smaller model |
| `/health` and `/api/messages` return 503 for a few seconds after a task starts | the subscription check has not completed yet | normal; the container itself answers 503 until it passes |
| Task stops with exit code 78 and a `cannot start` reason | see [First launch](#first-launch); for example the subscription was cancelled | fix the cause, then force a new deployment |
| `/ready` returns `initializing` for a long time | indexing a large document set | wait; raise `TaskCpu`/`TaskMemory` if it takes too long |
| `/ready` returns `no_documents` | nothing was indexed: the bucket is empty or `ExistingDocumentsBucket` names the wrong bucket, or the task cannot read it (S3 `AccessDenied` from SSE-KMS without `KmsKeyArn`, or from a bucket policy). Then the logs show `Failed to download S3 objects` (the bucket could not be listed) or `Failed to process S3 object` (an object could not be read) | upload documents, or fix the access (pass `KmsKeyArn` and update the stack, or fix the bucket policy); then force a new deployment (Step 6) |
| `/ready` returns `error` | the document service could not start, for example because of its embedding or Claude settings | the logs show `Initialization failed` with the reason; fix it and update the stack |
| A document is missing from answers; the log shows `Talk-to-Docs: could not read <name> (<ErrorName>); skipped.` | the file is in an unsupported format (for example `.xls`), damaged, or holds no text (for example a scanned PDF) | convert it to a [supported format](#step-6--load-your-documents), upload it and force a new deployment |
| The bot replies `I could not read the attached file <name>. Supported formats: ...` | the attached file is in an unsupported format or damaged | send it in a supported format |
| A data question gets `Your data question is taking longer than expected. Please try again in a few minutes.` | NLSQL did not answer within 330 s: a slow database or NLSQL API, or several data questions waiting (NLSQL answers one at a time) | ask again later; for slow queries, check the database and the NLSQL lines in the logs |
| Logs show `[auth] rejected a request from an unexpected caller (iss=..., appid=...)` | something other than the Bot Connector or the bot's own app called the messaging endpoint, for example another app in your Entra tenant | nothing to fix: Teams and Web Chat are not affected. If the `appid` is unexpected, find that app in Entra ID |
| A chart or CSV link returns 404 | links point at the task's disk: files are deleted after 24 hours and do not survive a new deployment | ask the question again |
| NL-to-SQL answers stop and documents answer instead; logs show `NLSQL API rejected the ApiToken` | invalid or revoked NLSQL token | generate a new token at nlsql.com, update the secret, force a new deployment |
| Data questions are answered from the documents; logs show `both mode: NLSQL could not answer (database unreachable, see the nlsql-api log)` and `cannot connect to the <engine> database (...)` | the task cannot reach or log in to the database: a firewall (Azure SQL error 40615 names the refused address), a security group, or a wrong parameter or password | allow the task ([Database settings by engine](#database-settings-by-engine), `TaskSubnetIds` for a fixed egress IP) or fix the parameter or secret, then force a new deployment |
| Map questions are answered from the documents; the logs mention `cdn.plot.ly` | the task cannot reach https://cdn.plot.ly, where map charts load their outlines from | allow outbound HTTPS to cdn.plot.ly ([External dependencies](#external-dependencies)) |
| The bot never replies | messaging endpoint not the HTTPS `MessagingEndpoint`, Teams channel missing, or wrong/expired client secret or tenant ID (the logs then show `[process]` errors from the bot) | recheck Step 3; renew the secret ([Rotating credentials](#rotating-credentials)) |

## Operating the deployment

### Monitoring

**Is it up?** `HealthCheck` and `ReadinessCheck` (Step 5), and target health:

```shell
TG=$(aws ecs describe-services --cluster <ClusterName> --services <ServiceName> --region <region> \
  --query 'services[0].loadBalancers[0].targetGroupArn' --output text)
aws elbv2 describe-target-health --target-group-arn "$TG" --region <region> \
  --query 'TargetHealthDescriptions[].TargetHealth' --output table
```

**What did ECS do?** Service events, and why tasks stopped:

```shell
aws ecs describe-services --cluster <ClusterName> --services <ServiceName> --region <region> \
  --query 'services[0].events[:10].[createdAt, message]' --output table
aws ecs describe-tasks --cluster <ClusterName> --region <region> \
  --tasks $(aws ecs list-tasks --cluster <ClusterName> --desired-status STOPPED \
    --region <region> --query 'taskArns[:5]' --output text) \
  --query 'tasks[].[stoppedAt, stoppedReason, containers[0].exitCode]' --output table
```

**Logs.** `aws logs tail /ecs/<stack-name> --follow --region <region>`, or these filter patterns
in CloudWatch Logs Insights or `filter-log-events`:

| Pattern | Shows |
|---|---|
| `"AWS Marketplace"` | subscription check and metering |
| `"cannot start"` | why a task refused to run |
| `ThrottlingException` | Bedrock quota pressure |
| `AccessDenied` | IAM, KMS or Bedrock model access problems |
| `"NLSQL API"` | NLSQL API failures (DocsAndSql) |
| `"both mode: NLSQL could not answer"` | why a question was answered from the documents (the reason, never the question) |
| `"cannot connect to the"` | database connection failures, with the driver's reason |
| `"Bedrock probe"` | whether Claude answered at task start (model access) |
| `"Failed to download S3 objects"`, `"Failed to process S3 object"` | a documents bucket that could not be listed, or an object that could not be read |
| `"could not read"` | documents and attached files that could not be read |
| `"rejected a request"` | callers of the messaging endpoint other than the Bot Connector and the bot's own app |
| `ERROR` | everything else worth a look |

Useful alarms: ALB `UnHealthyHostCount` above 0 and `HTTPCode_Target_5XX_Count`, ECS
`CPUUtilization` and `MemoryUtilization` above 80%, CloudFront `5xxErrorRate`.

### What the IAM roles are for

**ExecutionRole** is used by ECS itself, not by the application:

| Permission | Purpose |
|---|---|
| `AmazonECSTaskExecutionRolePolicy` (AWS managed) | pull the image from the Marketplace ECR repository and write container logs |
| `secretsmanager:GetSecretValue` on your one secret ARN | inject the credentials at task start |
| `kms:Decrypt` on `KmsKeyArn`, only through Secrets Manager (only if set) | decrypt a secret encrypted with your key |

**TaskRole** is the running application's only AWS identity. The application uses the ECS task
role, and static AWS credential variables are ignored on ECS, so these are the only AWS
permissions it uses:

| Permission | Purpose |
|---|---|
| `aws-marketplace:RegisterUsage` on `*` | verify the subscription and meter the task; the action takes no resource ARN |
| `s3:ListBucket` on the documents bucket, `s3:GetObject` on its objects | read documents; no write or delete |
| `kms:Decrypt` on `KmsKeyArn`, only through S3 (only if set) | read an existing bucket encrypted with your key |
| `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` on Anthropic foundation models and this account's `global.anthropic.*` inference profiles | call Claude; no model subscription or management |

### Where your sensitive data lives, and how it is encrypted

| Data | Where | At rest | In transit |
|---|---|---|---|
| Bot client secret, NLSQL token, database password | your Secrets Manager secret; environment variables inside the running task | AWS KMS (`aws/secretsmanager`, or your `KmsKeyArn`) | TLS |
| Documents | your S3 bucket | SSE-S3 (AES-256) for the bucket the stack creates; your settings for an existing bucket | HTTPS; the created bucket denies non-TLS requests |
| Document index | task memory and disk, rebuilt at every start | Fargate ephemeral storage, AES-256 with AWS-managed keys | — |
| Charts and CSV exports of query results | the task's disk, deleted after 24 hours, at unguessable 128-bit random URLs | Fargate ephemeral storage, AES-256 | HTTPS from CloudFront to the viewer |
| Questions and answers | not stored by the application (conversation memory is off) | — | Teams to CloudFront TLS 1.2+; CloudFront to the load balancer HTTP inside AWS, admitted from CloudFront's addresses only and with a per-stack header (Security Hub CloudFront.9 flags this http-only origin; accepted, as the load balancer has no certificate of its own); task to Bedrock, S3 and Microsoft over HTTPS |
| Prompts to Claude | Amazon Bedrock; a global profile may process a request in any commercial AWS Region. Per AWS, Bedrock does not use prompts or answers to train models or share them with model providers | not stored by the application | HTTPS |
| Question text (DocsAndSql) | sent to api.nlsql.com to generate SQL | not stored by the application | HTTPS |
| Logs | CloudWatch Logs, 14 days; secrets, tokens and request headers are never logged | CloudWatch Logs service encryption | TLS |
| Database data | your database; query results pass through the task | your database's settings | per engine, see [the engine table](#database-settings-by-engine) |

**Keys.** The template creates no KMS keys and no other cryptographic material. The only key in
the image is AWS Marketplace's public key (version 1) for checking the `RegisterUsage` signature;
it is public, and a new image version carries any replacement AWS issues.

### Rotating credentials

The container reads the secret only at task start, so every rotation ends with a new deployment.
Secrets Manager automatic rotation is not configured. Rotate in this order so the bot never goes
dark:

1. Create the new value alongside the old one:
   - **Bot client secret:** Entra client secrets expire (at most 24 months). Before the expiry
     date, add a new secret to the bot's app registration in the Azure portal (**Microsoft Entra
     ID**, **App registrations**, the bot app, **Certificates & secrets**). An expired secret
     stops all replies.
   - **NLSQL API token:** generate a new one at nlsql.com.
   - **Database password:** add or change it so the old one keeps working until step 3.
2. Update the secret and start a new task:

   ```shell
   aws secretsmanager put-secret-value --secret-id nlsql-ai-agent/credentials \
     --secret-string file://secret.json --region <region>
   aws ecs update-service --cluster <ClusterName> --service <ServiceName> \
     --force-new-deployment --region <region>
   ```

3. Once the new task is healthy and answering, revoke the old value.

### Backup and recovery

- **Documents:** the S3 bucket is the only persistent data store and the source of truth. The
  stack keeps it when deleted. For point-in-time recovery, turn on S3 Versioning or protect it
  with AWS Backup.
- **Index:** rebuilt from the bucket at every task start; nothing to back up.
- **Charts and exports:** transient by design; nothing to back up.
- **Credentials:** Secrets Manager keeps previous versions; a deleted secret can be restored
  during its recovery window.
- **Recovering the whole deployment:** create a new stack with the same parameters and
  `ExistingDocumentsBucket` set to the kept bucket. The CloudFront domain changes, so repeat
  Steps 3 and 4.

### Scaling

Vertical only. The service always runs exactly one task (see
[Why exactly one task](#1-what-gets-deployed)). For more capacity, update the stack with a larger
`TaskMemory`, `TaskCpu` or `EphemeralStorage`; ECS starts the new task before stopping the old
one. Answer throughput beyond that is set by your Bedrock quotas.

### Upgrading

Read the new version's release notes, then update the stack with the new version's template,
which carries the new `ImageUri` default. Keep every other parameter with `UsePreviousValue=true`,
but not `ImageUri`: a kept value is the old image, and only an omitted parameter takes the new
default. Omit `ImageUri`, or pass the new image explicitly:

```shell
aws cloudformation update-stack --stack-name nlsql-ai-agent --region <region> \
  --template-body file://nlsql-ai-agent-ecs-fargate.yaml --capabilities CAPABILITY_IAM \
  --parameters ParameterKey=Mode,UsePreviousValue=true ... \
    ParameterKey=ImageUri,ParameterValue=709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-agent:<new-version>
```

(one `UsePreviousValue=true` per parameter other than `ImageUri`). In the console, choose
**Update**, **Replace current template**, upload the new template, and overwrite the pre-filled
`ImageUri` with the new image. Changing only `ImageUri` with the current template is enough only
when the release notes say the template did not change.

ECS starts the new task, waits for it to pass health checks, then stops the old one; a failing new
version rolls back automatically. Documents are re-indexed from the bucket, and chart links from
the old task stop working. There is no data to migrate. As in Step 6, run
`aws ecs wait services-stable` after the stack update, then poll `/ready` until it returns 200.

### Uninstalling

```shell
aws cloudformation delete-stack --stack-name nlsql-ai-agent --region <region>
aws cloudformation wait stack-delete-complete --stack-name nlsql-ai-agent --region <region>
aws secretsmanager delete-secret --secret-id nlsql-ai-agent/credentials --region <region>
```

The documents bucket the stack created is **kept** so your documents are not lost with the stack.
To remove it, empty it and delete it:

```shell
aws s3 rm s3://<DocumentsBucket> --recursive --region <region>
aws s3api delete-bucket --bucket <DocumentsBucket> --region <region>
```

The log group is deleted with the stack. Then cancel the subscription in AWS Marketplace, clear
the messaging endpoint on your Azure Bot, and remove the Teams app.

## External dependencies

This product requires an ongoing internet connection. The following ongoing external services
are required to properly function:

- **AWS:** AWS Marketplace Metering Service, Amazon Bedrock, Amazon S3, AWS Secrets Manager,
  Amazon CloudWatch Logs and Amazon ECR.
- **Microsoft:** a Microsoft Azure subscription for the Azure Bot resource (the standard Microsoft
  Teams channel is free), Microsoft Entra ID, the Microsoft Bot Framework Bot Connector, and the
  Microsoft Teams and SharePoint file hosts that files sent to the bot are downloaded from.
- **NLSQL API** at api.nlsql.com, in DocsAndSql mode only: receives question text, returns SQL.
  The token is free and self-service.
- **Plotly CDN** at https://cdn.plot.ly, in DocsAndSql mode only: map answers load their map
  outlines from it, both in the task (for the chart image) and in the viewer's browser (for the
  interactive map). Other charts do not use it.

No packages are downloaded on deployment: the embedding model is part of the image.

This product extends the functionality of Microsoft Teams and without it, this product has very
limited utility. Please note that Microsoft Teams might require its own license for full
functionality with this listing.

## Support

info@nlsql.com · https://nlsql.com/contacts
