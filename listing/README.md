# Listing texts for the AWS Marketplace Management Portal

Each file is pasted into one portal field as is. `make listing-check` enforces the portal's
length and character limits, and that the usage instructions name the template's Regions.
Before pasting, replace `<commit>` with the commit that `make template-url` pins.

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
| Categories | Collaboration & Productivity, Business Applications or Business Intelligence, and a generative AI or machine learning category from the portal's list | 1 to 3 |
| Support information | `product/support-description.txt` | 2000 |
| Additional resources | **Deployment guide**: `https://github.com/denissa4/nlsql-gcp-marketplace/blob/<commit>/docs/aws-deployment-guide.md`<br>**CloudFormation template**: `https://raw.githubusercontent.com/denissa4/nlsql-gcp-marketplace/<commit>/cloudformation/nlsql-ai-employee-ecs-fargate.yaml` | |
| Logo | the product's logo (the Teams app's `teams/color.png` is a 192 x 192 source) | |

AWS requires two disclosures in the description or the usage instructions:
- the add-on sentence for Microsoft Teams, which the short and the long description carry;
- the list of ongoing external services, which the long description and the usage instructions
  carry. It names Azure Bot Service with its Microsoft Teams channel, plus any other channel the
  buyer enables, and describes Google Gemini only as optional, with the buyer's own key. The
  Google setup steps stay in the guide.

Keep both when editing, and keep every service they name in the guide's **External
dependencies** section. The listing names Microsoft and Google services only as dependencies;
it must not point buyers to another cloud platform.

## Regions

There is nothing to set in the portal. For container products, "You don't choose Regions" (AWS
Marketplace Seller Guide, *Regions and countries*): AWS hosts the image in us-east-1, and buyers
can deploy it in any Region. The template's `SupportedRegion` rule is the only Region control.
It accepts 16 Regions and names them in its error message; line 5 of `usage-instructions.txt`
names the same 16, abbreviated, and `make listing-check` compares the two.

## Offer

The product's public offer (**Offers** tab):

| Setting | Value |
|---|---|
| Pricing | Monthly, **`$<PRICE>` per month** (`RecurringPaymentTerm`). **PLACEHOLDER: the amount is not decided yet.** Replace `<PRICE>` here once it is; until then the offer keeps a nominal test price |
| Free trial | None. AWS offers container free trials with hourly pricing only |
| EULA | Standard Contract for AWS Marketplace |
| Refund policy | A clear rule, also still to be decided, for example: "Full refund of the first month's fee if you cancel within 30 days of subscribing. Email info@nlsql.com with your AWS account ID." |

Both are under **Request changes → Update public offer**: **Update pricing terms** and **Update
refund policy**. A price increase locks pricing for 90 days. Raise the test price to the decided
amount once, after your own test subscription and before **Update visibility**.

## Version

**Request changes → Add new version**:

| Field | Value |
|---|---|
| Version title and release notes | `release-notes/1.0.0.md` |
| Delivery option type | Container image |
| Delivery option title | NLSQL AI Employee on Amazon ECS (AWS Fargate) |
| Container image | `709825985650.dkr.ecr.us-east-1.amazonaws.com/nlsql/ai-employee:1.0.0` |
| Supported services | **ECS** only (untick EKS) |
| Delivery option description | `delivery-option-description.txt` |
| Usage instructions | `usage-instructions.txt` |

Deployment resources:
- **CloudFormation template (Amazon ECS on AWS Fargate)**:
  `https://raw.githubusercontent.com/denissa4/nlsql-gcp-marketplace/<commit>/cloudformation/nlsql-ai-employee-ecs-fargate.yaml`
- **Deployment guide**:
  `https://github.com/denissa4/nlsql-gcp-marketplace/blob/<commit>/docs/aws-deployment-guide.md`
