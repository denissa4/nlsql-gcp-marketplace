# NLSQL on AWS Marketplace — ECS container product

This branch packages NLSQL as an **AWS Marketplace container product** with a
**Container image** delivery option, deployed on Amazon ECS / AWS Fargate.

It is deliberately independent of `main`, which packages the same application for
**Google Cloud Marketplace** as a Kubernetes app (Helm chart, `mpdev` deployer,
`schema.yaml`). None of that machinery has an AWS equivalent, so it is not
carried here.

The application image itself is shared: it is built from
[`denissa4/ainlbot`](https://github.com/denissa4/ainlbot).

## Listing shape

| | |
|---|---|
| Product type | Container product |
| Delivery option | Container image (Amazon ECS / AWS Fargate) |
| Pricing | Hourly, per Amazon ECS task — metered to the second, billed per hour |
| Free trial | 30 days, covering one running task |
| Entitlement and metering | `RegisterUsage` |
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

Two things in it are load-bearing:

- **The task role grants `aws-marketplace:RegisterUsage`.** Under hourly pricing
  that call is not only an entitlement check at start-up — it is what AWS meters
  the running task on, so it is the billing path itself. The container obtains
  credentials for it from this role; AWS forbids baking credentials into the
  image.
- **Credentials come from AWS Secrets Manager**, injected by ECS as container
  secrets, never written into the task definition. AWS rejects images and
  templates containing hardcoded secrets. The buyer creates one secret with
  `ApiToken`, `DbPassword` and optionally `AppPassword` as JSON keys.

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

- [ ] `RegisterUsage` integration in the application. Under hourly pricing this
      is the metering path, not just an entitlement check: without it no usage is
      recorded, buyers run free, and the trial has nothing to convert into. It
      must be called from inside the application, not from an `ENTRYPOINT`
      wrapper, which a buyer can override, and the AWS Region must be resolved at
      runtime rather than hardcoded.
- [ ] Non-root container. AWS requires it by default; the image currently runs
      supervisord as root with nginx on port 80.
- [ ] Buyer-facing usage instructions for the listing form.
- [ ] The real hourly price. The offer currently carries the nominal test price
      AWS expects during limited-visibility testing, and it has to be raised
      before the product goes public.

Note `/health` is answered by nginx directly (`return 200`), so it proves the
proxy is up but says nothing about the API behind it. Worth making it a real
check before relying on it for ALB health.
