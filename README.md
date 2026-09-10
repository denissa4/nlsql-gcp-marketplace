# NLSQL on Google Kubernetes Engine

[![Marketplace](https://img.shields.io/badge/Google%20Cloud-Marketplace-4285F4)](https://console.cloud.google.com/marketplace/product/nlsql/nlsql-kubernetes)

This repository contains everything needed to deploy **NLSQL** to a Google Kubernetes
Engine (GKE) cluster from the command line. It is the source package behind the
[NLSQL listing on Google Cloud Marketplace](https://console.cloud.google.com/marketplace/product/nlsql/nlsql-kubernetes).

---

## Overview

NLSQL turns plain-English questions into SQL against a relational database you already
run, and returns the answer in your chat tool or web front end. A business user asks
*"what were our top five products by revenue last quarter?"*; NLSQL translates that to
SQL, runs it read-only against your database, and formats the result.

Key points for anyone evaluating a deployment:

- **Your data stays in your cluster.** NLSQL connects directly to the database you
  configure at install time. Query results are not sent to NLSQL's servers.
- **Read-only by design.** NLSQL issues `SELECT` statements. Give it a read-only
  database user.
- **Stateless.** The container holds no persistent state, so there is nothing to back
  up beyond your own database and the install configuration. See
  [Back up and restore](#back-up-and-restore).
- **Supported engines:** Microsoft SQL Server, MySQL, PostgreSQL, Oracle, Snowflake.

### About NLSQL

NLSQL is developed by NLSQL. Product information is at
[www.nlsql.com](https://www.nlsql.com); support contacts are in
[Support](#support).

### Architecture

```
                 ┌──────────────────────────────────────────┐
   users ──────► │  Ingress (Google Cloud Load Balancer)     │   optional
                 └───────────────────┬──────────────────────┘
                                     │
                 ┌───────────────────▼──────────────────────┐
                 │  Service  <release>            :80        │
                 └───────────────────┬──────────────────────┘
                                     │
                 ┌───────────────────▼──────────────────────┐
                 │  Deployment  <release>                    │
                 │    container: nlsql            :80        │
                 │    envFrom: ConfigMap + Secret            │
                 │    health:  GET /health                   │
                 └──────┬─────────────────────────┬──────────┘
                        │                         │
        ┌───────────────▼───────┐   ┌─────────────▼─────────────┐
        │ your database         │   │ api.nlsql.com             │
        │ (in VPC / Cloud SQL)  │   │ (language model service)  │
        └───────────────────────┘   └───────────────────────────┘
```

The chart creates a `Deployment`, `Service`, `ConfigMap`, `Secret`, `ServiceAccount`,
an optional `Ingress` and `HorizontalPodAutoscaler`, and the
`app.k8s.io/v1beta1` **`Application`** resource that Google Cloud console uses to
render the app's page.

---

## Installation

### Quick install with Google Cloud Marketplace

Deploy from the Google Cloud console in a few clicks:

[Open NLSQL on Google Cloud Marketplace](https://console.cloud.google.com/marketplace/product/nlsql/nlsql-kubernetes)

The console form collects the same settings documented under
[Configuration options](#configuration-options).

### Command line instructions

The rest of this section is the command-line equivalent of that console flow. Every
setting available in the UI can be set here.

You can follow these steps from [Cloud Shell](https://cloud.google.com/shell), which
already has `gcloud`, `kubectl` and `helm` installed.

#### Prerequisites

##### Set up command line tools

Install and authenticate the tools:

```shell
# gcloud — https://cloud.google.com/sdk/docs/install
gcloud auth login
gcloud components install kubectl

# helm 3 — https://helm.sh/docs/intro/install/
helm version   # expect v3.x
```

Select the project you will deploy into and enable the required APIs:

```shell
export PROJECT_ID=your-project-id

gcloud config set project "$PROJECT_ID"
gcloud services enable \
  container.googleapis.com \
  containerregistry.googleapis.com \
  cloudresourcemanager.googleapis.com
```

##### Create a Google Kubernetes Engine cluster

Skip this if you already have a cluster. NLSQL runs on any GKE cluster running
Kubernetes 1.29 or later, Standard or Autopilot.

```shell
export CLUSTER=nlsql-cluster
export ZONE=us-central1-c

gcloud container clusters create "$CLUSTER" \
  --zone "$ZONE" \
  --num-nodes 2 \
  --machine-type e2-standard-2
```

> **Network access.** The cluster must be able to reach your database. If the database
> is Cloud SQL, either enable a private IP on the same VPC or run the
> [Cloud SQL Auth Proxy](https://cloud.google.com/sql/docs/mysql/sql-proxy). If it is
> on-premises, the cluster needs a VPN or Interconnect route to it.

##### Configure kubectl to connect to the cluster

```shell
gcloud container clusters get-credentials "$CLUSTER" --zone "$ZONE"
kubectl cluster-info
```

##### Clone this repository

```shell
git clone https://github.com/denissa4/nlsql-gcp-marketplace.git
cd nlsql-gcp-marketplace
```

##### Install the Application resource definition

The `Application` CRD lets Google Cloud console group and display the app's resources.
It is installed once per cluster:

```shell
kubectl apply -f \
  "https://raw.githubusercontent.com/GoogleCloudPlatform/marketplace-k8s-app-tools/master/crd/app-crd.yaml"
```

Verify:

```shell
kubectl get crd applications.app.k8s.io
```

##### Acquire the usage reporting Secret

NLSQL is a commercial listing, so usage is reported to Cloud Marketplace through a
reporting Secret. Create the NLSQL instance once from the
[Marketplace listing](https://console.cloud.google.com/marketplace/product/nlsql/nlsql-kubernetes) to have Google generate it, then copy its name:

```shell
kubectl get secrets --namespace "$NAMESPACE" \
  -o custom-columns=NAME:.metadata.name | grep license
```

Pass that name as `reportingSecret` in the install below. If you are deploying under a
bring-your-own-license agreement, leave `reportingSecret` empty.

#### Install the Application

##### Configure the installation with environment variables

Set the identity of this install:

```shell
export APP_INSTANCE_NAME=nlsql-1
export NAMESPACE=nlsql
export TAG=1.4.0
```

Set the connection details for your database and NLSQL account:

```shell
# NLSQL account
export NLSQL_API_ENDPOINT="https://api.nlsql.com/googlesheet"
export NLSQL_API_TOKEN="<your NLSQL API token>"

# Public URL users will reach NLSQL on — see the note below
export STATIC_ENDPOINT="https://nlsql.example.com/"

# Database
export DB_TYPE="MSSQL"          # MSSQL | MySQL | PostgreSQL | Oracle | Snowflake
export DB_HOST="db.internal.example.com"
export DB_NAME="analytics"
export DB_PORT="1433"
export DB_SCHEMA=""             # empty = engine default
export DB_USER="nlsql_ro"
export DB_PASSWORD="<database password>"

# Usage reporting Secret from the previous step, or "" for BYOL
export REPORTING_SECRET=""
```

> **`STATIC_ENDPOINT` matters.** NLSQL uses it to build links back to itself, so it
> must be the URL your users actually reach — the hostname you will point at the
> Ingress, including the scheme and a trailing slash. If you are only testing through
> `kubectl port-forward`, use `http://localhost:8080/`.

Create the namespace:

```shell
kubectl create namespace "$NAMESPACE"
```

##### Create the credentials Secret

Keep credentials out of your shell history and out of the Helm release by creating the
Secret yourself:

```shell
kubectl create secret generic "${APP_INSTANCE_NAME}-credentials" \
  --namespace "$NAMESPACE" \
  --from-literal=ApiToken="$NLSQL_API_TOKEN" \
  --from-literal=DbPassword="$DB_PASSWORD" \
  --from-literal=AppPassword=""
```

> If you use the Microsoft Teams channel, set `AppPassword` to the Bot Framework
> application password instead of leaving it empty.

##### Pin the image to an immutable digest

Cloud Marketplace recommends deploying by digest rather than by tag, so that a
redeploy can never pick up a different image:

```shell
export IMAGE_REPO="us-docker.pkg.dev/nlsql-public/nlsql/nlsql"

export IMAGE_DIGEST=$(gcloud artifacts docker images describe "${IMAGE_REPO}:${TAG}" \
  --format='value(image_summary.digest)')

echo "Deploying ${IMAGE_REPO}@${IMAGE_DIGEST}"
```

##### Install the chart

```shell
helm install "$APP_INSTANCE_NAME" chart/nlsql \
  --namespace "$NAMESPACE" \
  --set image.repo="$IMAGE_REPO" \
  --set image.digest="$IMAGE_DIGEST" \
  --set credentials.existingSecret="${APP_INSTANCE_NAME}-credentials" \
  --set nlsql.ApiEndPoint="$NLSQL_API_ENDPOINT" \
  --set nlsql.StaticEndPoint="$STATIC_ENDPOINT" \
  --set database.DatabaseType="$DB_TYPE" \
  --set database.DataSource="$DB_HOST" \
  --set database.DbName="$DB_NAME" \
  --set database.DbPort="$DB_PORT" \
  --set database.DbSchema="$DB_SCHEMA" \
  --set database.DbUser="$DB_USER" \
  --set reportingSecret="$REPORTING_SECRET"
```

<details>
<summary>Alternative: render manifests and apply them with kubectl</summary>

If you would rather not keep Helm release state in the cluster, render the same
manifests and apply them directly. Use the identical `--set` flags:

```shell
helm template "$APP_INSTANCE_NAME" chart/nlsql \
  --namespace "$NAMESPACE" \
  --set image.repo="$IMAGE_REPO" \
  --set image.digest="$IMAGE_DIGEST" \
  --set credentials.existingSecret="${APP_INSTANCE_NAME}-credentials" \
  --set nlsql.StaticEndPoint="$STATIC_ENDPOINT" \
  --set database.DataSource="$DB_HOST" \
  --set database.DbName="$DB_NAME" \
  --set database.DbUser="$DB_USER" \
  > "${APP_INSTANCE_NAME}_manifest.yaml"

kubectl apply -f "${APP_INSTANCE_NAME}_manifest.yaml" --namespace "$NAMESPACE"
```

</details>

##### Confirm the deployment

```shell
kubectl wait --namespace "$NAMESPACE" \
  --for=condition=available --timeout=300s \
  deployment/"$APP_INSTANCE_NAME"

kubectl get pods,svc,application --namespace "$NAMESPACE"
```

##### View the app in the Google Cloud console

```shell
echo "https://console.cloud.google.com/kubernetes/application/${ZONE}/${CLUSTER}/${NAMESPACE}/${APP_INSTANCE_NAME}"
```

---

## Using the app

### Check that NLSQL is healthy

```shell
kubectl port-forward --namespace "$NAMESPACE" \
  "svc/${APP_INSTANCE_NAME}" 8080:80 &

curl -sS http://localhost:8080/health
```

A `200` response means NLSQL started and reached its configured database.

### Ask a question

With the port-forward still running, post a question to the NLSQL endpoint:

```shell
curl -sS -X POST http://localhost:8080/api/messages \
  -H 'Content-Type: application/json' \
  -d '{"type": "message", "text": "how many rows are in the orders table?"}'
```

### Connect a chat channel

To use NLSQL from Microsoft Teams, register a Bot Framework app and reinstall with the
`teams.*` values set — see [Configuration options](#configuration-options). Point the
bot's messaging endpoint at `${STATIC_ENDPOINT}api/messages`.

### Change the database user or password

Update the Secret and restart the pods:

```shell
kubectl create secret generic "${APP_INSTANCE_NAME}-credentials" \
  --namespace "$NAMESPACE" \
  --from-literal=ApiToken="$NLSQL_API_TOKEN" \
  --from-literal=DbPassword="$NEW_DB_PASSWORD" \
  --from-literal=AppPassword="" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/"$APP_INSTANCE_NAME" --namespace "$NAMESPACE"
```

To change the database *user*, or any other non-secret setting, run `helm upgrade`
with the new `--set` value; the pods roll automatically because the Deployment
checksums its ConfigMap.

---

## Configuration options

These are the same options the Cloud Marketplace console form presents. Set each one
with `--set <name>=<value>` on `helm install` or `helm upgrade`.

### Required

| Helm value | Console field | Description |
|---|---|---|
| `nlsql.StaticEndPoint` | Public application URL | URL users reach NLSQL on, with scheme and trailing slash |
| `credentials.ApiToken` | NLSQL API token | Token issued with your NLSQL subscription |
| `database.DataSource` | Database host | Hostname or IP reachable from the cluster |
| `database.DbName` | Database name | |
| `database.DbUser` | Database user | Use a read-only user |
| `credentials.DbPassword` | Database password | |

`credentials.ApiToken` and `credentials.DbPassword` are not needed on the command line
if you set `credentials.existingSecret` instead — the recommended path above.

### Application

| Helm value | Default | Description |
|---|---|---|
| `nlsql.ApiEndPoint` | `https://api.nlsql.com/googlesheet` | NLSQL API endpoint for your channel |
| `replicaCount` | `1` | Number of NLSQL pods |
| `image.repo` | `us-docker.pkg.dev/nlsql-public/nlsql/nlsql` | Image repository including registry |
| `image.tag` | `1.4.0` | Image tag; ignored when `image.digest` is set |
| `image.digest` | `""` | Immutable `sha256:...` digest — preferred |
| `image.pullPolicy` | `IfNotPresent` | |

### Database

| Helm value | Default | Description |
|---|---|---|
| `database.DatabaseType` | `MSSQL` | `MSSQL`, `MySQL`, `PostgreSQL`, `Oracle` or `Snowflake` |
| `database.DbPort` | `1433` | MSSQL 1433, MySQL 3306, PostgreSQL 5432, Oracle 1521 |
| `database.DbSchema` | `""` | Empty uses the engine default schema |
| `database.ssl` | `"True"` | Require TLS on the database connection |

### Microsoft Teams channel (optional)

| Helm value | Default | Description |
|---|---|---|
| `teams.MicrosoftAppId` | `""` | Bot Framework application ID; empty disables the channel |
| `teams.MicrosoftAppTenantId` | `""` | Required when the app type is `SingleTenant` |
| `teams.MicrosoftAppType` | `MultiTenant` | `MultiTenant`, `SingleTenant` or `UserAssignedMSI` |
| `credentials.AppPassword` | `""` | Bot Framework application password |

### Networking

| Helm value | Default | Description |
|---|---|---|
| `service.type` | `ClusterIP` | |
| `service.port` | `80` | |
| `ingress.enabled` | `false` | Create a Google Cloud Load Balancer Ingress |
| `ingress.className` | `gce` | |
| `ingress.hosts[0].host` | `""` | Hostname to serve on |
| `ingress.annotations` | `{}` | For example a static IP or managed certificate |

### Resources and scaling

| Helm value | Default | Description |
|---|---|---|
| `resources.requests.cpu` | `500m` | |
| `resources.requests.memory` | `256Mi` | |
| `resources.limits.cpu` | `1000m` | |
| `resources.limits.memory` | `512Mi` | |
| `autoscaling.enabled` | `false` | Create a HorizontalPodAutoscaler |
| `autoscaling.minReplicas` | `1` | |
| `autoscaling.maxReplicas` | `5` | |
| `autoscaling.targetCPUUtilizationPercentage` | `70` | |

### Cloud Marketplace

| Helm value | Default | Description |
|---|---|---|
| `reportingSecret` | `""` | Usage reporting Secret name; empty for BYOL |

### Legacy variables

The container still reads six environment variables from NLSQL's previous Azure App
Service deployment (`DEBUG`, `PYTHONUNBUFFERED`, `DOCKER_ENABLE_CI`,
`DOCKER_REGISTRY_SERVER_URL`, `WEBSITE_HTTPLOGGING_RETENTION_DAYS`,
`WEBSITES_ENABLE_APP_SERVICE_STORAGE`). They have no effect on GKE and are set under
`legacyEnv` in `chart/nlsql/values.yaml` only so the container's environment is
identical across platforms. You should not need to change them; `DEBUG=1` will raise
log verbosity if you are troubleshooting.

---

## Expose the application

By default NLSQL is only reachable inside the cluster. To publish it:

Reserve a static IP and create a Google-managed certificate:

```shell
gcloud compute addresses create nlsql-ip --global

cat <<EOF | kubectl apply --namespace "$NAMESPACE" -f -
apiVersion: networking.gke.io/v1
kind: ManagedCertificate
metadata:
  name: nlsql-cert
spec:
  domains:
    - nlsql.example.com
EOF
```

Then enable the Ingress:

```shell
helm upgrade "$APP_INSTANCE_NAME" chart/nlsql \
  --namespace "$NAMESPACE" \
  --reuse-values \
  --set ingress.enabled=true \
  --set 'ingress.hosts[0].host=nlsql.example.com' \
  --set 'ingress.annotations.kubernetes\.io/ingress\.global-static-ip-name=nlsql-ip' \
  --set 'ingress.annotations.networking\.gke\.io/managed-certificates=nlsql-cert' \
  --set nlsql.StaticEndPoint="https://nlsql.example.com/"
```

Point your DNS A record at the reserved IP:

```shell
gcloud compute addresses describe nlsql-ip --global --format='value(address)'
```

Provisioning the load balancer and certificate takes 10–20 minutes. Watch progress:

```shell
kubectl describe managedcertificate nlsql-cert --namespace "$NAMESPACE"
kubectl get ingress "$APP_INSTANCE_NAME" --namespace "$NAMESPACE" --watch
```

To use your own certificate instead, create a TLS Secret and set `ingress.tls`.

---

## Scaling

NLSQL is stateless, so replicas scale horizontally without coordination.

Scale manually:

```shell
helm upgrade "$APP_INSTANCE_NAME" chart/nlsql \
  --namespace "$NAMESPACE" --reuse-values \
  --set replicaCount=3
```

Or enable the HorizontalPodAutoscaler:

```shell
helm upgrade "$APP_INSTANCE_NAME" chart/nlsql \
  --namespace "$NAMESPACE" --reuse-values \
  --set autoscaling.enabled=true \
  --set autoscaling.minReplicas=2 \
  --set autoscaling.maxReplicas=10 \
  --set autoscaling.targetCPUUtilizationPercentage=70
```

The HPA needs the metrics server, which is enabled by default on GKE. Confirm it is
reading metrics:

```shell
kubectl get hpa "$APP_INSTANCE_NAME" --namespace "$NAMESPACE"
```

A `TARGETS` column showing `<unknown>` means metrics are not available yet; give it a
minute after the pods start.

Scale vertically by raising the container limits:

```shell
helm upgrade "$APP_INSTANCE_NAME" chart/nlsql \
  --namespace "$NAMESPACE" --reuse-values \
  --set resources.limits.cpu=2000m \
  --set resources.limits.memory=1Gi
```

---

## Back up and restore

NLSQL stores no persistent state in the cluster: it creates no PersistentVolumeClaims,
and every query is answered from your database. There is nothing in the workload itself
to back up.

Two things are worth preserving.

### Back up your database

Your own database is the system of record. Back it up with whatever tooling you already
use — for Cloud SQL:

```shell
gcloud sql backups create --instance=YOUR_INSTANCE
```

### Back up the install configuration

Save the release configuration so an install can be reproduced exactly:

```shell
mkdir -p nlsql-backup

# Non-secret configuration
helm get values "$APP_INSTANCE_NAME" --namespace "$NAMESPACE" \
  > nlsql-backup/values.yaml

# Rendered manifests
helm get manifest "$APP_INSTANCE_NAME" --namespace "$NAMESPACE" \
  > nlsql-backup/manifest.yaml

# Credentials Secret — this file contains secrets. Store it encrypted.
kubectl get secret "${APP_INSTANCE_NAME}-credentials" \
  --namespace "$NAMESPACE" -o yaml > nlsql-backup/credentials.yaml
```

> `nlsql-backup/credentials.yaml` contains base64-encoded credentials in cleartext.
> Encrypt it, or store the credentials in
> [Secret Manager](https://cloud.google.com/secret-manager) and skip this file.

### Restore

Recreate the namespace and Secret, then reinstall from the saved values:

```shell
kubectl create namespace "$NAMESPACE"
kubectl apply -f nlsql-backup/credentials.yaml --namespace "$NAMESPACE"

helm install "$APP_INSTANCE_NAME" chart/nlsql \
  --namespace "$NAMESPACE" \
  --values nlsql-backup/values.yaml
```

---

## Updating

NLSQL releases are published to Cloud Marketplace as new image tags. To move to a new
release:

Check the current version:

```shell
kubectl get application "$APP_INSTANCE_NAME" --namespace "$NAMESPACE" \
  -o jsonpath='{.spec.descriptor.version}'
```

Resolve the digest of the new tag:

```shell
export NEW_TAG=1.4.0

export NEW_DIGEST=$(gcloud artifacts docker images describe "${IMAGE_REPO}:${NEW_TAG}" \
  --format='value(image_summary.digest)')
```

Pull the matching chart revision and upgrade:

```shell
git fetch --tags
git checkout "v${NEW_TAG}"

helm upgrade "$APP_INSTANCE_NAME" chart/nlsql \
  --namespace "$NAMESPACE" \
  --reuse-values \
  --set image.digest="$NEW_DIGEST"
```

Watch the rollout, which is a rolling update with no downtime at `replicaCount` ≥ 2:

```shell
kubectl rollout status deployment/"$APP_INSTANCE_NAME" --namespace "$NAMESPACE"
```

Roll back if the new release misbehaves:

```shell
helm rollback "$APP_INSTANCE_NAME" --namespace "$NAMESPACE"
```

---

## Uninstall the application

### Using the Google Cloud console

1. Open [Kubernetes Engine > Applications](https://console.cloud.google.com/kubernetes/application).
2. Select the NLSQL instance.
3. Click **Delete**.

### Using the command line

Set the instance you want to remove:

```shell
export APP_INSTANCE_NAME=nlsql-1
export NAMESPACE=nlsql
```

Delete the release. Because the `Application` resource owns every component
(`addOwnerRef: true`), this removes the Deployment, Service, ConfigMap, ServiceAccount,
Ingress and HorizontalPodAutoscaler with it:

```shell
helm uninstall "$APP_INSTANCE_NAME" --namespace "$NAMESPACE"
```

If you installed with `kubectl apply` instead of Helm:

```shell
kubectl delete -f "${APP_INSTANCE_NAME}_manifest.yaml" --namespace "$NAMESPACE"
```

Confirm nothing is left:

```shell
kubectl get all,application,configmap,secret,ingress --namespace "$NAMESPACE"
```

### Clean up resources that are intentionally left behind

A Secret you created yourself is not owned by the release and is deliberately kept, so
that an accidental uninstall does not destroy your credentials. Remove it explicitly:

```shell
kubectl delete secret "${APP_INSTANCE_NAME}-credentials" --namespace "$NAMESPACE"
```

NLSQL creates no PersistentVolumeClaims, but check for any left by other workloads
before deleting a shared namespace:

```shell
kubectl get pvc --namespace "$NAMESPACE"
```

Then remove the namespace, the reserved IP, and the certificate if you created them:

```shell
kubectl delete namespace "$NAMESPACE"

gcloud compute addresses delete nlsql-ip --global
```

### Delete the cluster

Only if you created it solely for NLSQL:

```shell
gcloud container clusters delete "$CLUSTER" --zone "$ZONE"
```

---

## Troubleshooting

| Symptom | Likely cause | What to check |
|---|---|---|
| Pods stuck `CrashLoopBackOff` | NLSQL cannot reach the database | `kubectl logs deployment/$APP_INSTANCE_NAME -n $NAMESPACE`; confirm `database.DataSource` resolves from inside the cluster |
| `/health` returns non-200 | Bad database credentials | Confirm the Secret keys are exactly `ApiToken`, `DbPassword`, `AppPassword` |
| Ingress has no address after 20 min | Certificate not yet provisioned | `kubectl describe managedcertificate nlsql-cert -n $NAMESPACE` |
| Links in NLSQL responses point at the wrong host | `nlsql.StaticEndPoint` not updated | Re-run `helm upgrade` with the correct URL |
| `ImagePullBackOff` | Digest or repo wrong | `gcloud artifacts docker images describe "${IMAGE_REPO}:${TAG}"` |

Collect logs for a support request:

```shell
kubectl logs --namespace "$NAMESPACE" \
  --selector app.kubernetes.io/instance="$APP_INSTANCE_NAME" \
  --tail=500 > nlsql-logs.txt
```

---

## Support

- Product and deployment support: **info@nlsql.com**
- Website: [www.nlsql.com](https://www.nlsql.com)
- Cloud Marketplace listing: [NLSQL on Google Cloud Marketplace](https://console.cloud.google.com/marketplace/product/nlsql/nlsql-kubernetes)

Billing and subscription questions are handled through Google Cloud Marketplace; see
[Cloud Marketplace billing](https://cloud.google.com/marketplace/docs/billing).

## Licensing

The contents of this repository — the Helm chart, deployer, and documentation — are
licensed under the [Apache License 2.0](LICENSE).

The NLSQL application image is commercial software licensed separately under the terms
presented on the [Cloud Marketplace listing](https://console.cloud.google.com/marketplace/product/nlsql/nlsql-kubernetes). Deploying the image
constitutes acceptance of those terms.

---

## Repository layout

```
.
├── README.md                  this document
├── LICENSE                    Apache 2.0
├── Makefile                   build, verify and release targets for the publisher
├── schema.yaml                Cloud Marketplace UI form and parameter contract
├── chart/nlsql/               the Helm chart that is deployed
├── scripts/validate-schema.py offline Marketplace schema validator
├── deployer/Dockerfile        Cloud Marketplace deployer image
└── apptest/deployer/          integration test run by `mpdev verify`
```

### Building and verifying the package (publisher only)

Images for this listing live in Artifact Registry at
`us-docker.pkg.dev/nlsql-public/nlsql` — the app at `…/nlsql` and the deployer at
`…/deployer`, the folder name Cloud Marketplace requires.

Google's build tooling is no longer anonymously pullable, so authenticate first.
You also need [crane](https://github.com/google/go-containerregistry/tree/main/cmd/crane)
(`brew install crane`) to write the required image annotation.

```shell
gcloud auth login
gcloud auth configure-docker gcr.io,us-docker.pkg.dev
```

Then:

```shell
export SERVICE_NAME=<service name from Producer Portal > Overview>

make schema-lint         # validate schema.yaml offline — no Docker needed
make lint                # helm lint the chart
make no-secrets          # fail if a credential is committed
make schema-check        # prove Producer Portal can extract /data/schema.yaml
make promote-image       # push the app image on both tags
make deployer-image      # build and push the deployer on both tags
make annotate            # write the Marketplace annotation into both manifests
make check-annotations   # fail unless every tag carries it, with the right service
make check-tags          # confirm what is published matches the portal
make verify              # mpdev install -> test -> uninstall
```

#### Versions and tags

Every image must carry **two** tags. Google's requirement:

> All of your app's images must be tagged with the release track and the current
> version. For example, if you're releasing version `2.0.5` on the `2.0` release
> track, all the images must be tagged with `2.0` and `2.0.5`.

`TRACK` is the `MAJOR.MINOR` prefix of `VERSION`, derived in the Makefile so the
two cannot drift. This release is **1.4.0 on track 1.4**; cut a new one with:

```shell
make promote-image deployer-image VERSION=1.5.0    # TRACK becomes 1.5
```

`schema.yaml`'s `publishedVersion` must equal the chart's `appVersion` —
`make schema-lint` fails if they diverge.

#### The image annotation

Every image must carry
`com.googleapis.cloudmarketplace.product.service.name=services/$SERVICE_NAME`, the
deployer included. It must be in the image **manifest** — a Dockerfile `LABEL`
only reaches the image *config*, which Marketplace does not read, and the portal
rejects the release with *"Missing annotation … in manifest of image"*. `make
annotate` writes it with `crane mutate`; `make check-annotations` fails if any
published tag lacks it or carries a different service name.

Because `crane mutate` rewrites the manifest, **the digest changes**. The track
tag is re-pointed automatically, but you must re-select the release in Producer
Portal afterwards so it picks up the new digest.

#### If Producer Portal cannot extract the schema

`make schema-check` reproduces what the portal does — it builds the deployer, then
reads `/data/schema.yaml` back out of the image and parses it. Common causes when
the portal still fails:

- the deployer was never pushed at the tag the portal points at (`make check-tags`);
- `publishedVersion` disagrees with the release tag;
- a property in `schema.yaml` names a chart value that does not exist, so the
  substitution lands nowhere (`make schema-lint` catches this).
