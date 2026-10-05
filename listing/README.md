# Listing texts for the AWS Marketplace Management Portal

Each file is pasted into one portal field as is. `make listing-check` enforces the portal's
length and character limits. Before pasting, replace `<commit>` with the commit that
`make template-url` pins.

Every change below is a request: **Server products → select the product → Request changes**,
then **Submit**, and it shows on the **Requests** tab. A request locks the product (or the offer)
until it finishes, so wait for **Succeeded** before the next request on the same one.

## Product information

**Request changes → Update product information**

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

## Regions

There is nothing to set in the portal. For container products, "You don't choose Regions" (AWS
Marketplace Seller Guide, *Regions and countries*): AWS hosts the image in us-east-1, and buyers
can deploy it in any Region. The template's `SupportedRegion` rule is the only Region control.
It accepts 16 Regions and names them in its error message.

## Offer

`offer-kqidah5l5ufi2`:

| Setting | Value |
|---|---|
| Pricing | Monthly, **$99.00 per month** (`RecurringPaymentTerm`; the draft carries a $0.001 placeholder) |
| Free trial | None. AWS offers container free trials with hourly pricing only |
| EULA | Standard Contract for AWS Marketplace (as now) |
| Refund policy | Replace "30 days notice after deployment - 100% refund" with a clear rule, for example: "Full refund of the first month's fee if you cancel within 30 days of subscribing. Email info@nlsql.com with your AWS account ID." |

Both are under **Request changes → Update public offer**: **Update pricing terms** and **Update
refund policy**. A price increase locks pricing for 90 days. Raise the placeholder to $99.00
once, after your own test subscription and before **Update visibility**.

## Version

**Request changes → Add new version**:

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
