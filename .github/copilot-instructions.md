# Copilot instructions for fantasy-infra

GitOps Kustomize repo for the fantasy football pipeline's Kubernetes deployment. Synced automatically by Argo CD (registered in `homelab-deployment`) — there's no manual `kubectl apply` step for normal changes.

## System architecture

This is one of three repos that make up the platform:
- **`fantasy-core-pipeline`**: the brain. A FastAPI gateway + Python ingestion/optimization scripts, deployed here as `base/core-pipeline/`. Reads Postgres/Redis/RabbitMQ connection info from env vars whose defaults must match the Service names/namespaces defined in `base/databases/`.
- **`fantasy-browser-worker`**: the hands. A Node/Playwright worker, deployed here as a KEDA-scaled job under `base/browser-worker/`, that consumes RabbitMQ messages published by `fantasy-core-pipeline` and executes them against the ESPN UI.
- **`fantasy-infra`** (this repo): the plumbing. Owns the actual Kubernetes topology (namespaces, Services, PVCs, secrets) both application repos assume exists. A change here that renames a Service, moves a namespace, or changes a port silently breaks whichever application repo's hardcoded/default connection string assumed the old value.
- This repo itself is deployed onto a cluster managed by **`homelab-deployment`** (registers this repo as an Argo CD `Application`, provisions shared cluster infra like KEDA/OpenBao/storage).

## Stack conventions

- Layout: `base/` holds environment-agnostic manifests (`databases/`, `core-pipeline/`, `browser-worker/`); `overlays/production` and `overlays/draft-day` hold environment-specific patches. Validate any manifest change with `kubectl kustomize overlays/production/` (and `overlays/draft-day/`) rather than assuming a base-only change is sufficient.
- Secrets are never committed in cleartext — they're materialized via `ExternalSecret` from the shared OpenBao `ClusterSecretStore` (see `overlays/production/externalsecrets.yaml`). Any manifest introducing a credential/token/API key must go through that path, not a plain `Secret` with inline `data`/`stringData`.
- Playwright worker pods scale via KEDA off queue depth and are expected to scale to zero when idle — don't suggest a fixed `replicas` count or a plain `Deployment` for that workload without checking whether it should stay a KEDA-scaled resource.
- Postgres/TimescaleDB needs a real PVC — don't suggest `emptyDir` or removing volume claims for stateful services.
- Grafana dashboards (`grafana_dashboard: "1"`) and alert rules (`grafana_alert: "1"`, `base/telemetry/`) are ConfigMaps in this repo, loaded by sidecars in homelab-deployment's Grafana; datasources are still provisioned in `homelab-deployment/apps/telemetry/`. Alert rule changes must keep `./tests/cronjob_alerts/run.sh` passing (runbook: `docs/cronjob-alerts.md`).

## Critical Environment Variables (fantasy-core-pipeline)

**These MUST be set or the app fails silently at runtime**:

- `DRAFT_SYNC_IMAGE`: Container image for the draft_matrix.py listener Job spawned by `POST /v1/draft/start`.
  - **Issue fantasy-web-ui#7**: If missing, practice draft start fails with `"DRAFT_SYNC_IMAGE is not configured..."`.
  - Must match a published image ref from `fantasy-core-pipeline` repo (e.g., `ghcr.io/cvillarr23/fantasy-core-pipeline:073aad9a8b4db94f8fdf7da23bcfc8d29e9d06e6`)
  - Set in `base/core-pipeline/fastapi-deployment.yaml` under `containers[0].env`
  - **After adding/updating**: Push to main → ArgoCD syncs within 3–5 min (or force via `argocd app sync fantasy-infra --force`)

- `BROWSER_WORKER_IMAGE`: Draft room sync Job image (already configured — keep in sync with fantasy-browser-worker releases).

- `DB_HOST`, `DB_NAME`, `DB_USER`: Postgres topology — must match Service names in `base/databases/postgres.yaml`.
- `REDIS_HOST`, `REDIS_PORT`: Redis connection — must match Service in `base/databases/redis-deployment.yaml`.
- `AMQP_URL`: RabbitMQ credentials — from `rabbitmq-credentials` Secret (configured in overlays).

## Things to double-check in review

- **Env var names and default Service DNS hostnames** (`*.svc.cluster.local`) referenced here must match what `fantasy-core-pipeline`/`fantasy-browser-worker` actually read via `os.getenv(...)`/`process.env` — a namespace or name mismatch between a Deployment's env and the app's fallback default fails silently at runtime, not at manifest-render time.
- **Check `metadata.namespace` consistency** across a change — this codebase has previously had mismatches between a manifest's namespace and another repo's hardcoded fallback default (e.g. `default` vs `databases`).
- **DRAFT_SYNC_IMAGE is set and valid** — don't merge a PR that removes or empties this env var without confirming with fantasy-core-pipeline maintainers.
- **Resource requests/limits should stay conservative** (this runs on homelab-scale nodes) — flag any request/limit bump that isn't clearly justified.
- **Image refs** must exist in ghcr.io — if bumping a commit SHA, verify it was published (check GitHub Actions in the source repo).

## Deployment Checklist (ArgoCD + Manual Sync)

After merging to `main`:

1. **Wait for ArgoCD** (3–5 min), OR force sync:
   ```bash
   argocd app sync fantasy-infra --force
   kubectl rollout status deployment/fantasy-core-pipeline -n default
   ```

2. **Verify Pods are healthy**:
   ```bash
   kubectl get pods -n default -l app=fantasy-core-pipeline
   # Expected: READY 1/1, STATUS Running
   ```

3. **Test critical endpoints** (if applicable):
   ```bash
   kubectl port-forward svc/fantasy-core-pipeline 8000:8000 -n default
   curl http://localhost:8000/v1/health  # Should return 200 OK
   ```

4. **If Pods crash**:
   ```bash
   kubectl logs -n default <pod-name> --previous  # See startup error
   kubectl describe pod <pod-name> -n default     # See events
   ```

## Related Repos (Cross-Repo Changes)

When making breaking changes, coordinate with:

- **fantasy-core-pipeline** (`src/api/draft.py`, `src/database.py`): reads env vars, defaults to env-vars or K8s Service names
  - If renaming a Secret or env var here, update `fantasy-core-pipeline` to match
  - Verify image bump SHAs are published to ghcr.io

- **fantasy-browser-worker** (`src/jobs/draft_sync.js`, etc.): KEDA-scaled, consumes RabbitMQ
  - If changing KEDA config or RabbitMQ topology, verify worker can still connect

- **fantasy-web-ui** (React UI): calls `/v1/draft/*` endpoints
  - If API changes, coordinate with UI release

- **homelab-deployment** (cluster infra owner): provisions KEDA, OpenBao, storage
  - If needing new CRDs or cluster-wide resources, file an issue there first
