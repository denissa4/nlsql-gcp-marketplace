# NLSQL on AWS — deployment guide

The full version of the AWS Marketplace usage instructions. The listing's **Usage
instructions** field is capped at 4000 characters, so it carries a condensed form of this
document and links here for the detail.

Applies to NLSQL `1.0.1`, image
`709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/nlsql:1.0.1`.

---

## What this deploys

A CloudFormation stack containing:

| Resource | Purpose |
|---|---|
| ECS cluster + Fargate service | runs the NLSQL container |
| Application Load Balancer | fronts the task on HTTP port 80 |
| CloudFront distribution | terminates HTTPS using CloudFront's own certificate |
| ExecutionRole / TaskRole | see [What the template creates, and why](#what-the-template-creates-and-why) |
| CloudWatch log group | `/ecs/<your stack name>`, 14-day retention |

You pay AWS separately for these resources; the AWS Marketplace charge covers the NLSQL
software only.

The container listens on **TCP 8080** and runs as an unprivileged user, **uid 10001**. The
load balancer accepts traffic on port 80 and forwards it to 8080, and CloudFront serves
HTTPS to the outside world. If you build your own task definition rather than using the
template, map container port 8080 and point the `/health` check at it — mapping port 80
will never pass a health check.

## Why CloudFront is in the path

Microsoft Bot Framework refuses to accept a plain HTTP messaging endpoint, and the load
balancer carries no certificate of its own. CloudFront supplies a trusted
`https://<id>.cloudfront.net` endpoint using its default certificate, with no custom domain
or ACM certificate needed. Viewers are redirected to HTTPS; CloudFront reaches the load
balancer over HTTP inside AWS.

Nothing is cached — every TTL is zero. Cookies and query strings are forwarded because the
target group uses `lb_cookie` stickiness to keep a conversation on one task.

## External dependency

NLSQL requires an ongoing internet connection:

- `https://api.nlsql.com` — translates natural-language questions into SQL.
- AWS Marketplace Metering Service — verifies your subscription and reports container hours.

Your database is queried from inside your own VPC, and its contents are not sent to either
service. Tasks must therefore run in subnets with outbound internet access.

## Before you start

1. **Subscribe first, and deploy into the same AWS account.** The container verifies the
   subscription at start-up and exits if the account is not entitled. A task that keeps
   restarting usually means the wrong account, or subnets with no outbound internet access.
2. **Pick one Region and stay in it.** Create the secret in the same Region as the stack —
   ECS cannot read a secret from another Region. The `us-east-1` in the examples below is a
   placeholder for your Region, but leave the
   `709825985650.dkr.ecr.us-east-1.amazonaws.com` image path exactly as written: the
   Marketplace registry is served from us-east-1 for every Region.
3. **Two or more public subnets**, in different Availability Zones, each with a route to an
   internet gateway. The load balancer and the tasks share a single subnet list, and the
   load balancer is internet-facing by default. To keep it private, pass
   `LoadBalancerScheme=internal` and reach NLSQL from inside the VPC — note that Microsoft
   Teams then cannot deliver messages to it.
4. **A database** reachable from those subnets. See [Database settings](#database-settings).
5. **An NLSQL API token.** Generate it self-service in your account at https://nlsql.com.
   The NLSQL account and api.nlsql.com are included with your AWS Marketplace subscription —
   there is no separate purchase and no approval step.
6. **An Azure Bot resource** in your Microsoft Entra tenant, with a single-tenant app
   registration. Note the Microsoft App ID, the client secret and the tenant ID.
7. **Permissions.** Creating IAM roles (`iam:CreateRole`, `iam:AttachRolePolicy`,
   `iam:PutRolePolicy`, `iam:PassRole`) — this is what `--capabilities CAPABILITY_IAM`
   acknowledges — plus `secretsmanager:CreateSecret` and the usual ECS, Elastic Load
   Balancing, EC2, CloudFront and CloudWatch Logs permissions.

## Database settings

Supported engines: **Microsoft SQL Server, MySQL, PostgreSQL and Snowflake**. A read-only
user is recommended — NLSQL issues only SELECT statements.

| Parameter | MSSQL / MySQL / PostgreSQL | Snowflake |
|---|---|---|
| `DataSource` | database host | your account identifier |
| `DbPort` | 1433 / 3306 / 5432 | ignored |
| `DbName` | database name | database name |
| `DbSchema` | optional | **required** |
| `Warehouse` | ignored | **required** |
| `DbUser` / `DbPassword` | yes | yes |

## Step 1 — Store the credentials

One Secrets Manager secret, three JSON keys, all required:

```shell
aws secretsmanager create-secret \
  --name nlsql/credentials \
  --secret-string '{"ApiToken":"YOUR_TOKEN","DbPassword":"YOUR_DB_PASSWORD","AppPassword":"YOUR_BOT_SECRET"}' \
  --region us-east-1
```

Note the returned ARN. `AppPassword` is the Azure Bot client secret.

## Step 2 — Launch the stack

Download the deployment template from the listing, then in the CloudFormation console choose
**Create stack → Upload a template file**, or use the CLI:

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
    ParameterKey=DataSource,ParameterValue=db.example.internal \
    ParameterKey=DbName,ParameterValue=analytics \
    ParameterKey=DbUser,ParameterValue=nlsql_readonly \
    ParameterKey=CredentialsSecretArn,ParameterValue=arn:aws:secretsmanager:... \
    ParameterKey=MicrosoftAppId,ParameterValue=00000000-0000-0000-0000-000000000000 \
    ParameterKey=MicrosoftAppTenantId,ParameterValue=00000000-0000-0000-0000-000000000000
```

Creation takes roughly 10–20 minutes, most of it CloudFront. Wait for it rather than moving
straight on — the outputs do not exist until the stack completes:

```shell
aws cloudformation wait stack-create-complete --stack-name nlsql --region us-east-1
```

If it rolls back, the first failure is at the bottom of:

```shell
aws cloudformation describe-stack-events --stack-name nlsql --region us-east-1 --max-items 20
```

`ImageUri` defaults to the AWS Marketplace image for this version; leave it alone unless you
are deliberately pinning an older one.

### Sizing and cost

`DesiredCount` defaults to 1. The free trial covers **one task for 30 days**. After the
trial ends every running task is billed at the published hourly rate, and a second task is
billed from the moment you start it, trial or not. `TaskCpu` and `TaskMemory` default to
1024 / 2048.

## Step 3 — Connect Microsoft Teams

```shell
aws cloudformation describe-stacks --stack-name nlsql \
  --query 'Stacks[0].Outputs' --region us-east-1
```

| Output | Use |
|---|---|
| `MessagingEndpoint` | paste into the Azure Bot resource's **Messaging endpoint** |
| `ApplicationUrl` | the HTTPS URL to open in a browser |
| `HealthCheck` | returns 200 once the service is in service |
| `LogGroupName` | where container logs land |
| `LoadBalancerUrl` | plain HTTP origin — not valid for Bot Framework |

Then, on the same Azure Bot resource, add the **Microsoft Teams** channel and install the
bot in Teams.

## Step 4 — Confirm it is running

Open `ApplicationUrl` and ask a question about your data. If something is wrong, the
container logs in the `LogGroupName` group are the place to look:

| Symptom | Likely cause |
|---|---|
| Task starts then exits, repeatedly | account not subscribed, or no outbound internet access |
| `Can't connect to DataBase: <...>` | a required database parameter is empty — see [Database settings](#database-settings) |
| Target group never healthy | a custom task definition mapping port 80 instead of 8080 |
| Teams messages never arrive | messaging endpoint not the HTTPS `MessagingEndpoint` value |

## What the template creates, and why

**ExecutionRole** pulls the AWS Marketplace image, writes to CloudWatch Logs, and calls
`secretsmanager:GetSecretValue` on exactly the one secret ARN you supply — nothing else.

**TaskRole** is the running container's own role and grants only
`aws-marketplace:RegisterUsage`, so the task can verify your subscription and report the
hours it runs. Its `Resource` is `*` because `RegisterUsage` takes no resource ARN.

### Where your sensitive data lives

Only in the Secrets Manager secret you create, encrypted at rest by AWS Secrets Manager. ECS
injects the values at task start; they never appear in the task definition, the image, the
CloudFormation template or the logs. The template creates no KMS keys of its own.

### Rotating credentials

Update the secret value, then force a new deployment so tasks pick it up:

```shell
aws ecs update-service --cluster nlsql --service <service-name> --force-new-deployment --region us-east-1
```

## Uninstalling

```shell
aws cloudformation delete-stack --stack-name nlsql --region us-east-1
```

Then unsubscribe in AWS Marketplace, and delete the Secrets Manager secret. The CloudWatch
log group is deleted with the stack.

## Support

info@nlsql.com · https://nlsql.com/contacts
