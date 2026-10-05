# Listing texts for the AWS Marketplace Management Portal

Each file is pasted into one portal field as is. `make listing-check` enforces the portal's
length and character limits. Before pasting, replace `<commit>` with the commit that
`make template-url` pins.

## Product information

**Products → Server products → NLSQL AI Agent → Update product information**

| Field | Source | Limit |
|---|---|---|
| Product title | `product/title.txt` | 72 characters |
| Short description | `product/short-description.txt` | 1000 |
| Long description | `product/long-description.txt` | 5000 |
| Highlights | `product/highlight-1.txt`, `-2`, `-3` | 250 each |
| Search keywords | `product/keywords.txt`, one per line | 15 keywords, 50 characters each, 250 combined |
| Categories | Collaboration & Productivity, Data Analysis, and a generative AI or machine learning category from the portal's list | 1 to 3 |
| Support information | `product/support-description.txt` | 2000 |
| Additional resources | **Deployment guide**: `https://github.com/denissa4/nlsql-gcp-marketplace/blob/<commit>/docs/aws-deployment-guide.md`<br>**CloudFormation template**: `https://raw.githubusercontent.com/denissa4/nlsql-gcp-marketplace/<commit>/cloudformation/nlsql-ai-agent-ecs-fargate.yaml` | |
| Logo | keep the current one | |

Why these replace the draft's texts:
- The draft describes a website assistant for external users and "Azure infrastructure". This
  listing delivers neither.
- Its keywords offer Graph RAG, which the template does not enable.
- AWS requires two disclosures in the description or the usage instructions: the add-on sentence
  for Microsoft Teams, and the list of ongoing external services. The long description carries
  both, as do the usage instructions.

## Region availability

**Update region availability**: the 16 Regions the template accepts:

us-east-1, us-east-2, us-west-1, us-west-2, ca-central-1, sa-east-1, eu-west-1, eu-west-2,
eu-west-3, eu-central-1, eu-north-1, ap-south-1, ap-northeast-1, ap-northeast-2,
ap-southeast-1, ap-southeast-2.

- Remove ap-east-1 and me-south-1. Not every listed Claude model is a Bedrock global inference
  profile there, and the template refuses them.
- Leave future Regions off.

## Offer

`offer-kqidah5l5ufi2`:

| Setting | Value |
|---|---|
| Pricing | Monthly, **$99.00 per month** (`RecurringPaymentTerm`; the draft carries a $0.001 placeholder) |
| Free trial | None. AWS offers container free trials with hourly pricing only |
| EULA | Standard Contract for AWS Marketplace (as now) |
| Refund policy | Replace "30 days notice after deployment - 100% refund" with a clear rule, for example: "Full refund of the first month's fee if you cancel within 30 days of subscribing. Email info@nlsql.com with your AWS account ID." |

## Version

**Add new version**:

| Field | Value |
|---|---|
| Version title and release notes | `release-notes/1.0.0.md` |
| Delivery option type | Container image |
| Delivery option title | NLSQL AI Agent on Amazon ECS (AWS Fargate) |
| Container image | `709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-agent:1.0.0` |
| Supported services | **ECS** only (untick EKS) |
| Delivery option description | `delivery-option-description.txt` |
| Usage instructions | `usage-instructions.txt` |

Deployment resources:
- **CloudFormation template (Amazon ECS on AWS Fargate)**:
  `https://raw.githubusercontent.com/denissa4/nlsql-gcp-marketplace/<commit>/cloudformation/nlsql-ai-agent-ecs-fargate.yaml`
- **Deployment guide**:
  `https://github.com/denissa4/nlsql-gcp-marketplace/blob/<commit>/docs/aws-deployment-guide.md`
