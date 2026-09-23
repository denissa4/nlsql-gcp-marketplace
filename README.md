# NLSQL on AWS Marketplace — ECS container product

This branch packages NLSQL as an **AWS Marketplace container product** with a
**Container image** delivery option, deployed on Amazon ECS / AWS Fargate.

It is deliberately independent of `main`, which packages the same application for
**Google Cloud Marketplace** as a Kubernetes app (Helm chart, `mpdev` deployer,
`schema.yaml`). None of that machinery has an AWS equivalent, so it is not
carried here.

The application is built from [`denissa4/ainlbot`](https://github.com/denissa4/ainlbot),
branch **`aws-marketplace-offer`** — not `master`. That branch carries the changes AWS
requires and Azure App Service cannot take: a non-root container, nginx on port 8080
instead of 80, and the `RegisterUsage` entitlement call. `master` keeps building what
Azure needs, and is merged into the AWS branch rather than the other way round.

## Listing shape

| | |
|---|---|
| Product type | Container product |
| Delivery option | Container image (Amazon ECS / AWS Fargate) |
| Pricing | Hourly, per Amazon ECS task — metered to the second, billed per hour |
| Free trial | 30 days, covering one running task |
| Entitlement and metering | `RegisterUsage`, called from inside the application |
| Container port | 8080 — the container runs as an unprivileged user |
| Registry | `709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/nlsql` |

## Layout

```
cloudformation/nlsql-ecs-fargate.yaml   deployment template buyers launch
README.md                               this document
LICENSE                                 Apache 2.0
```

## The deployment template

`cloudformation/nlsql-ecs-fargate.yaml` creates a Fargate service behind an
Application Load Balancer in a VPC and subnets the buyer already has. It is what
the listing's **Deployment templates** field points at.

Four things in it are load-bearing:

- **The task role grants `aws-marketplace:RegisterUsage`.** Under hourly pricing
  that call is not only an entitlement check at start-up — it is what AWS meters
  the running task on, so it is the billing path itself. The container obtains
  credentials for it from this role; AWS forbids baking credentials into the
  image.
- **`AWS_MARKETPLACE_PRODUCT_CODE` must reach the container.** The entitlement
  check no-ops without it, which means AWS meters nothing and an hourly product
  earns nothing. The application validates the value against a trusted list, so
  editing it in a downloaded template does not buy unmetered use.
- **The target group points at 8080, not 80.** nginx binds 8080 because the
  container runs as an unprivileged user, and Fargate cannot grant
  `NET_BIND_SERVICE` back. Only the load balancer listens on 80.
- **Credentials come from AWS Secrets Manager**, injected by ECS as container
  secrets, never written into the task definition. AWS rejects images and
  templates containing hardcoded secrets. The buyer creates one secret with
  `ApiToken`, `DbPassword` and `AppPassword` as JSON keys — all three required,
  since the Microsoft Teams channel is not optional.

`DesiredCount` defaults to 1, which is exactly what the free trial covers. A
buyer who scales to two tasks is billed for the second one during the trial.

Target-group stickiness is enabled because NLSQL holds conversation state in the
task, so a follow-up request has to reach the same one.

Validate any change before publishing:

```shell
aws cloudformation validate-template \
  --template-body file://cloudformation/nlsql-ecs-fargate.yaml \
  --region us-east-1
```

## Publishing a version (seller)

The Marketplace ECR repository already exists as `nlsql/nlsql` — repository names
are permanent and unique across every product in the seller account, so it is not
created again. New repositories, if ever needed, come from **Server products →
Request changes → Add repositories**.

1. Build the image from the application repository as **linux/amd64**; Marketplace
   is x86-only.
2. Tag it `nlsql:$(VERSION)` locally and run `make push`. `check-vars` refuses to
   push unless the template's `ImageUri` default names the same tag.
3. **Add new version** → **Container image** delivery option: the image URI,
   buyer-facing usage instructions, **Supported services** set to ECS and
   Fargate, and a **Deployment templates** entry pointing at
   `cloudformation/nlsql-ecs-fargate.yaml`.

The deployment template URL has to be publicly fetchable, and it should name a
commit rather than a branch — a branch URL serves whatever was pushed last, which
changes the template under buyers and under review:

```
https://raw.githubusercontent.com/denissa4/nlsql-gcp-marketplace/<commit>/cloudformation/nlsql-ecs-fargate.yaml
```

The product code, needed as input to `RegisterUsage`, is
`80vvvie6oupr9etlhimecwmkj`, paired with public key version 1.

## Outstanding before submission

- [x] `RegisterUsage` integration, in `api/nlsql/aws_marketplace.py` on the
      application's `aws-marketplace-offer` branch. Called from inside the
      application rather than an `ENTRYPOINT` wrapper a buyer could override, with
      the Region resolved at runtime from the ECS task ARN.
- [x] Non-root container: uid 10001, nginx on 8080, writable paths pre-chowned at
      build time.
- [ ] Buyer-facing usage instructions for the listing form.
- [ ] The real hourly price. The offer currently carries the nominal test price
      AWS expects during limited-visibility testing, and it has to be raised
      before the product goes public.

Note `/health` is answered by nginx directly (`return 200`), so it proves the
proxy is up but says nothing about the API behind it. Worth making it a real
check before relying on it for ALB health.
