# Deploying the hosted server (Google Cloud Run)

The hosted server is the same package run as `spain-open-data-mcp http`: Streamable HTTP,
stateless, JSON responses, at `/mcp`, with a health check at `/health` (Cloud Run reserves paths ending in `z`, such as `/healthz`). The image
([Dockerfile](../Dockerfile)) includes the data files (OpenStreetMap snapshot, census tract
shapes, walking graphs), so a new instance answers at once and never writes large files to its
in-memory disk.

The plan below stays inside Cloud Run's free tier for portfolio-level traffic: at most two
instances, scale to zero, 512 MiB memory, EU region. Set a budget alert before deploying.

## What you need

- A Google Cloud project with billing enabled (the free tier still needs a billing account).
- The `gcloud` CLI, logged in: `gcloud auth login`.

## 1. Budget alert (do this first)

In the console: Billing → Budgets & alerts → Create budget. Scope it to the project, amount
€1, alerts at 50%, 90% and 100%. Cloud Run has no hard spending cap; the alert and the instance
limit below are the safeguards.

## 2. Build and push the image

The [image workflow](../.github/workflows/image.yml) builds the image, starts it, checks that it
answers MCP and pushes it to `ghcr.io/digvijaysingh-ai/spain-open-data-mcp` on each version tag
or manual run. Cloud Run pulls from Artifact Registry, so copy it there (or build there with
`gcloud builds submit`):

```sh
PROJECT=your-project-id
REGION=europe-west1
VERSION=0.1.1
gcloud config set project "$PROJECT"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com
gcloud artifacts repositories create mcp --repository-format=docker --location="$REGION"
# Artifact Registry's free storage is 0.5 GB and the image is about 300 MB compressed, so keep
# only the newest image.
cat > policy.json <<'JSON'
[{"name":"keep-latest","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":1}},
 {"name":"delete-old","action":{"type":"Delete"},"condition":{"tagState":"any","olderThan":"1d"}}]
JSON
gcloud artifacts repositories set-cleanup-policies mcp --location="$REGION" --policy=policy.json --no-dry-run
```

Copy the image with Cloud Build (no local Docker needed; well inside its free build minutes).
On a new project, Cloud Build runs as the Compute Engine default service account, which first
needs permission to write logs and push images:

```sh
NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
for role in roles/logging.logWriter roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding "$PROJECT" --condition=None \
    --member="serviceAccount:$NUMBER-compute@developer.gserviceaccount.com" --role="$role"
done

IMAGE="$REGION-docker.pkg.dev/$PROJECT/mcp/spain-open-data-mcp:$VERSION"
cat > copy.yaml <<YAML
steps:
  - name: gcr.io/cloud-builders/docker
    args: [pull, ghcr.io/digvijaysingh-ai/spain-open-data-mcp:$VERSION]
  - name: gcr.io/cloud-builders/docker
    args: [tag, ghcr.io/digvijaysingh-ai/spain-open-data-mcp:$VERSION, $IMAGE]
images: [$IMAGE]
options:
  logging: CLOUD_LOGGING_ONLY
YAML
gcloud builds submit --no-source --config=copy.yaml --region="$REGION"
```

## 3. Deploy

```sh
gcloud run deploy spain-open-data-mcp \
  --image="$IMAGE" --region="$REGION" \
  --allow-unauthenticated \
  --min-instances=0 --max-instances=2 \
  --memory=512Mi --cpu=1 --concurrency=20 --timeout=120 \
  --cpu-throttling \
  --set-env-vars=SPAIN_MCP_ALLOWED_HOSTS=PLACEHOLDER
```

Cloud Run gives the service two host names (`spain-open-data-mcp-<number>.<region>.run.app`
and `spain-open-data-mcp-<hash>-<region code>.a.run.app`). Allow both, without `https://`. The
`^@^` prefix tells gcloud the value contains commas:

```sh
HOSTS=$(gcloud run services describe spain-open-data-mcp --region="$REGION" \
  --format=json | jq -r '[.status.url, (.metadata.annotations["run.googleapis.com/urls"] | fromjson[])] | unique | map(sub("https://"; "")) | join(",")')
gcloud run services update spain-open-data-mcp --region="$REGION" \
  --update-env-vars="^@^SPAIN_MCP_ALLOWED_HOSTS=$HOSTS"
```

Requests with any other `Host` header are refused (421), which blocks DNS rebinding. On Cloud Run,
Google's front end already drops unknown hosts with a 404 before they reach the server.

## 4. Check it

```sh
URL_HOST=${HOSTS%%,*}
curl -s "https://$URL_HOST/health"           # ok
curl -s "https://$URL_HOST/mcp" -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

Then add `https://$URL_HOST/mcp` as a custom connector in Claude (Settings → Connectors).

## Settings

| Variable | Image default | Meaning |
|---|---|---|
| `SPAIN_MCP_ALLOWED_HOSTS` | (none; local hosts only) | Host names the server answers, comma-separated |
| `SPAIN_MCP_ALLOWED_ORIGINS` | (none) | Browser origins allowed; MCP clients send none |
| `SPAIN_MCP_RATE_PER_MIN` | 60 | Requests per minute per client address |
| `SPAIN_MCP_SHARED_RATE_PER_MIN` | 600 | Per address in 160.79.104.0/21, which Claude's connectors share |
| `SPAIN_MCP_GLOBAL_RATE_PER_MIN` | 1200 | All requests to one instance |
| `SPAIN_MCP_PROXY_HOPS` | 1 | Proxies in front (Cloud Run adds one) |
| `SPAIN_MCP_HTTP_CACHE_MB` | 64 | Cap on cached upstream responses (in memory on Cloud Run) |
| `LOG_LEVEL` | INFO | One line per tool call: tool, outcome, duration. Never the arguments |

## Updating data

Data files are refreshed by rebuilding the image: the OpenStreetMap snapshot monthly, the crime
figures quarterly (merge the crime-data workflow's branch first), the rest yearly. A running
instance also checks the release manifests daily and downloads newer OpenStreetMap or geometry
files into memory, so redeploying monthly keeps memory use low.

## Custom domain (optional)

To serve from your own domain, map it in Cloud Run (Domain mappings), add it to
`SPAIN_MCP_ALLOWED_HOSTS`, and update the `remotes` URL in [server.json](../server.json).
