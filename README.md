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
| Pricing | Monthly — fixed monthly price, unlimited usage |
| Entitlement | `RegisterUsage` (required for fixed monthly pricing) |
| Registry | `709825985650.dkr.ecr.us-east-1.amazonaws.com/<sellerName>/nlsql` |

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

- **The task role grants `aws-marketplace:RegisterUsage`.** Fixed monthly pricing
  requires the container to verify entitlement at start-up, and it obtains
  credentials from this role. AWS forbids baking credentials into the image.
- **Credentials come from AWS Secrets Manager**, injected by ECS as container
  secrets, never written into the task definition. AWS rejects images and
  templates containing hardcoded secrets. The buyer creates one secret with
  `ApiToken`, `DbPassword` and optionally `AppPassword` as JSON keys.

Target-group stickiness is enabled because NLSQL holds conversation state in the
task, so a follow-up request has to reach the same one.

Validate any change before publishing:

```shell
aws cloudformation validate-template \
  --template-body file://cloudformation/nlsql-ecs-fargate.yaml \
  --region us-east-1
```

## Publishing a version (seller)

1. Create the repository in the AWS Marketplace Management Portal under
   **Server products → Request changes → Add repositories**. Repository names are
   permanent and must be unique across every product in the seller account.
2. Push the image with the commands shown under **View push commands**.
3. **Add new version**, choose the **Container image** delivery option, give it
   the image URI, usage instructions, and a link to the deployment template.

`make push` wraps step 2 once `ECR_REPO` and `VERSION` are set.

## Outstanding before submission

- [ ] `RegisterUsage` integration in the application — required for paid
      products. It must be called from inside the application, not from an
      `ENTRYPOINT` wrapper, which a buyer can override.
- [ ] Non-root container. AWS requires it by default; the image currently runs
      supervisord as root with nginx on port 80.
- [ ] Product code from Producer Portal, needed as input to `RegisterUsage`.
- [ ] Buyer-facing usage instructions for the listing form.

Note `/health` is answered by nginx directly (`return 200`), so it proves the
proxy is up but says nothing about the API behind it. Worth making it a real
check before relying on it for ALB health.
