# NLSQL on AWS — full deployment guide

The complete instructions for deploying NLSQL from AWS Marketplace onto Amazon ECS. The
listing's **Usage instructions** field is capped at 4000 characters, so it carries a
condensed form and links here.

Applies to NLSQL **1.0.1**, image
`709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/nlsql:1.0.1`.

**Contents**

1. [What gets deployed](#1-what-gets-deployed)
2. [What it costs](#2-what-it-costs)
3. [Before you start](#3-before-you-start)
4. [Step 1 — Store your credentials](#step-1--store-your-credentials)
5. [Step 2 — Launch the stack](#step-2--launch-the-stack)
6. [Step 3 — Point your Teams bot at the deployment](#step-3--point-your-teams-bot-at-the-deployment)
7. [Step 4 — Add the Teams channel and install the bot](#step-4--add-the-teams-channel-and-install-the-bot)
8. [Step 5 — Verify](#step-5--verify)
9. [Database settings by engine](#database-settings-by-engine)
10. [Troubleshooting](#troubleshooting)
11. [Operating the deployment](#operating-the-deployment)

---

## 1. What gets deployed

```
Microsoft Teams
      │  HTTPS
      ▼
CloudFront distribution        ← HTTPS, CloudFront's own certificate
      │  HTTP (inside AWS)
      ▼
Application Load Balancer :80  ← sticky sessions
      │  HTTP :8080
      ▼
ECS Fargate task               ← NLSQL container, uid 10001
      │
      ├──► your database (inside your VPC, or reachable from it)
      ├──► api.nlsql.com           (question → SQL)
      └──► AWS Marketplace Metering (subscription + hourly usage)
```

| Resource | Notes |
|---|---|
| ECS cluster and Fargate service | `DesiredCount` tasks, default 1 |
| Application Load Balancer | internet-facing by default, HTTP on 80 |
| Target group | port **8080**, health check `/health`, `lb_cookie` stickiness |
| CloudFront distribution | HTTPS via the default `*.cloudfront.net` certificate |
| ExecutionRole | pulls the image, writes logs, reads your one secret |
| TaskRole | `aws-marketplace:RegisterUsage` only |
| CloudWatch log group | `/ecs/<stack-name>`, 14-day retention |

**Why CloudFront is in the path.** Microsoft Bot Framework refuses a plain HTTP messaging
endpoint, and the load balancer has no certificate of its own. CloudFront supplies a trusted
HTTPS endpoint with no custom domain and no ACM certificate to buy. Nothing is cached — every
TTL is zero; it exists purely to terminate TLS.

**Why port 8080.** The container runs as an unprivileged user (uid 10001), and an
unprivileged process cannot bind port 80. AWS Fargate cannot grant `NET_BIND_SERVICE` back,
so nginx listens on 8080. If you write your own task definition instead of using the
template, map **8080** — a target group pointed at 80 will never go healthy.

## 2. What it costs

The AWS Marketplace charge is per running ECS task-hour, metered by AWS. The free trial
covers **one task for 30 days**; after that every task is billed, and a second task is billed
from the moment you start it, trial or not.

You pay AWS separately for the infrastructure the template creates. At the defaults
(1 task, 1024 CPU units, 2048 MiB), in us-east-1, the approximate monthly order of magnitude:

| Resource | Rough monthly cost |
|---|---|
| Fargate task, 1 vCPU / 2 GB, running continuously | ~$36 |
| Application Load Balancer | ~$17 plus LCU charges |
| CloudFront | free tier covers light use; then ~$0.085/GB out |
| CloudWatch Logs | ~$0.50/GB ingested, 14-day retention |
| Secrets Manager | ~$0.40 per secret |

Check the current figures on the AWS pricing pages — these are indicative only, and vary by
Region and traffic.

**Service quotas to be aware of.** The defaults are generous, but a constrained account may
hit: VPCs per Region (5), Application Load Balancers per Region (50), CloudFront
distributions per account (200), ECS tasks per service (5000), and Fargate vCPU concurrency
(the on-demand vCPU quota, which starts low in new accounts). If stack creation fails with a
quota error, request an increase in Service Quotas and retry.

## 3. Before you start

**Subscribe first, and deploy into the same AWS account.** The container calls
`RegisterUsage` at start-up and exits if the account is not entitled. This is also how AWS
meters your hourly usage, so it is not optional.

**Pick one AWS Region and stay in it.** The Secrets Manager secret must be in the same Region
as the stack — ECS cannot read a secret across Regions. The `us-east-1` in the examples is a
placeholder for your Region, but leave the `709825985650.dkr.ecr.us-east-1.amazonaws.com`
image path exactly as written: the Marketplace registry is served from us-east-1 for every
Region.

**Networking.** Two or more subnets in different Availability Zones. They must have a route
to the internet — the task reaches `api.nlsql.com` and the Marketplace Metering Service, and
Microsoft must be able to reach the load balancer. The load balancer and the tasks share one
subnet list, and the load balancer is internet-facing by default, so **use public subnets**
(with an internet gateway route) unless you pass `LoadBalancerScheme=internal`, in which case
Teams cannot deliver messages and only in-VPC clients can reach NLSQL.

**A database** reachable from those subnets — see
[Database settings by engine](#database-settings-by-engine). A read-only user is recommended:
NLSQL issues only `SELECT` statements.

**An NLSQL API token.** Generate it self-service in your account at https://nlsql.com. The
NLSQL account and the api.nlsql.com service are included with your AWS Marketplace
subscription — there is no separate purchase and no approval step.

**An Azure Bot resource** in the Microsoft 365 tenant you already use for Teams, with a
single-tenant app registration. You need three values from it: the **Microsoft App ID**, a
**client secret**, and the **tenant ID**. Create it in the Azure portal under *Azure Bot*,
not as a bare Entra app registration — only the Azure Bot resource has the messaging-endpoint
field you will need in Step 3.

**IAM permissions** for whoever launches the stack: creating IAM roles
(`iam:CreateRole`, `iam:AttachRolePolicy`, `iam:PutRolePolicy`, `iam:PassRole`) — this is what
`--capabilities CAPABILITY_IAM` acknowledges — plus `secretsmanager:CreateSecret` and the
usual ECS, Elastic Load Balancing, EC2, CloudFront and CloudWatch Logs permissions.

---

## Step 1 — Store your credentials

One AWS Secrets Manager secret, JSON, with all three keys. `AppPassword` is the Azure Bot
client secret.

```shell
aws secretsmanager create-secret \
  --name nlsql/credentials \
  --region us-east-1 \
  --secret-string '{"ApiToken":"<your-nlsql-token>","DbPassword":"<your-db-password>","AppPassword":"<your-bot-secret>"}'
```

Note the returned ARN — Step 2 needs it. Nothing else in the deployment stores these values:
ECS injects them at task start, and they never appear in the task definition, the image, the
template or the logs.

## Step 2 — Launch the stack

Download the deployment template from the listing's **Deployment templates** link, then in
the CloudFormation console choose **Create stack → Upload a template file**. Or use the CLI:

```shell
aws cloudformation create-stack \
  --stack-name nlsql \
  --template-body file://nlsql-ecs-fargate.yaml \
  --capabilities CAPABILITY_IAM \
  --region us-east-1 \
  --parameters \
    ParameterKey=VpcId,ParameterValue=vpc-xxxxxxxx \
    ParameterKey=SubnetIds,ParameterValue='subnet-aaaa\,subnet-bbbb' \
    ParameterKey=DatabaseType,ParameterValue=MSSQL \
    ParameterKey=DataSource,ParameterValue=db.example.com \
    ParameterKey=DbName,ParameterValue=analytics \
    ParameterKey=DbPort,ParameterValue=1433 \
    ParameterKey=DbUser,ParameterValue=nlsql_readonly \
    ParameterKey=CredentialsSecretArn,ParameterValue=arn:aws:secretsmanager:us-east-1:...:secret:nlsql/credentials-XXXXXX \
    ParameterKey=MicrosoftAppId,ParameterValue=00000000-0000-0000-0000-000000000000 \
    ParameterKey=MicrosoftAppTenantId,ParameterValue=00000000-0000-0000-0000-000000000000 \
    ParameterKey=MicrosoftAppType,ParameterValue=SingleTenant
```

### Every parameter

| Parameter | Required | Default | Meaning |
|---|---|---|---|
| `VpcId` | yes | — | VPC to deploy into |
| `SubnetIds` | yes | — | two or more subnets, different AZs |
| `LoadBalancerScheme` | no | `internet-facing` | `internal` keeps it inside the VPC (breaks Teams) |
| `ImageUri` | no | the 1.0.1 image | leave alone unless pinning an older version |
| `DatabaseType` | no | `MSSQL` | `MSSQL`, `MySQL`, `PostgreSQL`, `Redshift`, `Snowflake` |
| `DataSource` | yes | — | database host, or Snowflake account identifier |
| `DbName` | yes | — | database name |
| `DbPort` | no | `1433` | 1433 / 3306 / 5432 / 5439; ignored for Snowflake |
| `DbSchema` | no | empty | engine default; **required** for Snowflake |
| `DbUser` | yes | — | read-only user recommended |
| `Warehouse` | no | empty | **required** for Snowflake |
| `CredentialsSecretArn` | yes | — | the ARN from Step 1 |
| `MicrosoftAppId` | yes | — | Azure Bot application ID |
| `MicrosoftAppTenantId` | yes | — | Entra tenant ID |
| `MicrosoftAppType` | no | `SingleTenant` | or `MultiTenant` |
| `TaskCpu` | no | `1024` | 512 / 1024 / 2048 / 4096 |
| `TaskMemory` | no | `2048` | must be valid for the CPU size |
| `DesiredCount` | no | `1` | the free trial covers 1 |

### Wait for it

Creation takes roughly 10–20 minutes, most of it CloudFront. The outputs do not exist until
the stack completes, so wait rather than moving straight on:

```shell
aws cloudformation wait stack-create-complete --stack-name nlsql --region us-east-1
```

If it rolls back, the first failure is at the bottom of:

```shell
aws cloudformation describe-stack-events --stack-name nlsql --region us-east-1 --max-items 25
```

## Step 3 — Point your Teams bot at the deployment

**This is the step that connects your existing Teams bot to your new AWS deployment.** Until
you do it, Teams messages still go wherever the bot pointed before, and your AWS deployment
receives nothing.

Read the stack outputs:

```shell
aws cloudformation describe-stacks --stack-name nlsql \
  --query 'Stacks[0].Outputs' --region us-east-1 --output table
```

| Output | What it is |
|---|---|
| `MessagingEndpoint` | `https://<id>.cloudfront.net/api/messages` — **paste this into Azure** |
| `ApplicationUrl` | `https://<id>.cloudfront.net/` — open in a browser |
| `HealthCheck` | returns 200 once the service is in service |
| `LogGroupName` | where container logs land |
| `LoadBalancerUrl` | the HTTP origin behind CloudFront — **not** valid for Bot Framework |

Then, in the Azure portal:

1. Open your **Azure Bot** resource.
2. Go to **Settings → Configuration**.
3. Put the `MessagingEndpoint` value into the **Messaging endpoint** field. It must be the
   HTTPS CloudFront URL ending in `/api/messages` — Bot Framework rejects plain HTTP, so
   `LoadBalancerUrl` will not work here.
4. **Apply**.

The CloudFront domain is generated when the stack is created, so it is different for every
deployment and changes if you delete and recreate the stack. If you recreate it, come back
and update this field again.

## Step 4 — Add the Teams channel and install the bot

Still on the Azure Bot resource:

1. Go to **Channels**, add **Microsoft Teams**, and accept the terms.
2. From the Teams channel row, use **Open in Teams** to start a conversation with the bot,
   or distribute it to your organisation by uploading a Teams app package under
   *Teams → Apps → Manage your apps → Upload an app*.

A Teams app package is a zip containing `manifest.json` and two icons, where `id` and
`bots[0].botId` are both your Microsoft App ID and `validDomains` includes your CloudFront
domain. If your organisation already distributes the NLSQL Teams app, update its
`validDomains` to the new CloudFront domain rather than creating a second app.

## Step 5 — Verify

**The application answers.** Open `ApplicationUrl` in a browser, or check the health endpoint:

```shell
curl -sS -o /dev/null -w '%{http_code}\n' "$(aws cloudformation describe-stacks \
  --stack-name nlsql --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`HealthCheck`].OutputValue' --output text)"
```

**The subscription verified.** In the log group named by `LogGroupName`, look for the
entitlement line at task start:

```shell
aws logs filter-log-events --log-group-name /ecs/nlsql --region us-east-1 \
  --filter-pattern '"AWS Marketplace"' --query 'events[].message' --output text
```

`AWS Marketplace: entitlement verified for product ...` means `RegisterUsage` succeeded and
AWS is metering this task. If you see `CustomerNotEntitledException`, the account is not
subscribed; if the task exits repeatedly, see [Troubleshooting](#troubleshooting).

**Ask a real question.** Message the bot in Teams — this is the only end-to-end proof that
Teams, CloudFront, the container and your database all work together.

---

## Database settings by engine

| Parameter | MSSQL | MySQL | PostgreSQL | Redshift | Snowflake |
|---|---|---|---|---|---|
| `DataSource` | host | host | host | host | **account identifier** |
| `DbPort` | 1433 | 3306 | 5432 | 5439 | ignored |
| `DbName` | yes | yes | yes | yes | yes |
| `DbSchema` | optional | optional | optional | optional | **required** |
| `Warehouse` | — | — | — | — | **required** |
| `DbUser` / `DbPassword` | yes | yes | yes | yes | yes |

The database must accept connections from the task's subnets. For Amazon RDS or Redshift in
the same VPC, add a security group rule allowing the stack's task security group on the
database port. For a database outside AWS, allow the public egress IP of your NAT gateway, or
the task ENI's public IP if you deployed into public subnets.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Task starts then exits, repeatedly | account not subscribed, or no outbound internet access | subscribe in the deploying account; check the subnets have an internet route |
| `CustomerNotEntitledException` in the logs | deployed into a different account from the subscription | redeploy in the subscribed account |
| Target group never healthy | a custom task definition mapping port 80 | map container port 8080 |
| Stack rolls back on the secret | secret in a different Region from the stack | recreate the secret in the stack's Region |
| `Can't connect to DataBase: <...>` | a required database parameter is empty | see [Database settings by engine](#database-settings-by-engine) |
| Bot saves but never replies | messaging endpoint is the HTTP `LoadBalancerUrl` | use the HTTPS `MessagingEndpoint` value |
| Teams replies stop after a scale-up | conversation state is per-task | stickiness handles this; avoid changing the target group |
| `ResourceInitializationError` fetching the secret | ExecutionRole cannot read the secret ARN | confirm `CredentialsSecretArn` is exactly the ARN from Step 1 |

## Operating the deployment

### What the IAM roles are for

**ExecutionRole** pulls the Marketplace image, writes to CloudWatch Logs, and calls
`secretsmanager:GetSecretValue` on exactly the one secret ARN you supply — nothing else.

**TaskRole** is the running container's own role. It grants only
`aws-marketplace:RegisterUsage`, so the task can verify your subscription and report the
hours it runs. Its `Resource` is `*` because `RegisterUsage` takes no resource ARN.

### Where your sensitive data lives

Only in the Secrets Manager secret you create, encrypted at rest by AWS Secrets Manager with
its KMS key. The template creates no KMS keys of its own. Your database is queried from
inside your own network and its contents are not sent to NLSQL or to AWS.

### Rotating credentials

Update the secret value, then force a new deployment so running tasks pick it up:

```shell
aws secretsmanager put-secret-value --secret-id nlsql/credentials \
  --secret-string '{"ApiToken":"<new>","DbPassword":"<new>","AppPassword":"<new>"}' --region us-east-1
aws ecs update-service --cluster nlsql --service <service-name> --force-new-deployment --region us-east-1
```

### Scaling

Raise `DesiredCount` with a stack update. Every task beyond the first is billed at the hourly
rate immediately, including during the free trial. Conversation state lives in the task, so
the target group uses `lb_cookie` stickiness to keep a conversation on one task.

### Upgrading

Update the stack with a new `ImageUri`, or re-upload a newer template from the listing. ECS
replaces tasks with a rolling deployment.

### Uninstalling

```shell
aws cloudformation delete-stack --stack-name nlsql --region us-east-1
aws cloudformation wait stack-delete-complete --stack-name nlsql --region us-east-1
aws secretsmanager delete-secret --secret-id nlsql/credentials --region us-east-1
```

Then unsubscribe in AWS Marketplace. The log group is deleted with the stack. Remember to
clear the messaging endpoint on your Azure Bot resource, since the CloudFront domain is gone.

## Support

info@nlsql.com · https://nlsql.com/contacts
