# Copilot instructions for fantasy-infra

GitOps Helm chart repo for the fantasy football pipeline's Kubernetes deployment. Synced automatically by Argo CD (registered in `homelab-deployment`) — there's no manual `kubectl apply` step for normal changes.

## System architecture

This is one of three repos that make up the platform:
- **`fantasy-core-pipeline`**: the brain. A FastAPI gateway + Python ingestion/optimization scripts, deployed here as `charts/fantasy/templates/core-pipeline/`. Reads Postgres/Redis/RabbitMQ connection info from env vars whose defaults must match the Service names/namespaces defined in `charts/fantasy/templates/databases/`.
- **`fantasy-browser-worker`**: the hands. A Node/Playwright worker, deployed here as a KEDA-scaled job under `charts/fantasy/templates/browser-worker/`, that consumes RabbitMQ messages published by `fantasy-core-pipeline` and executes them against the ESPN UI.
- **`fantasy-infra`** (this repo): the plumbing. Owns the actual Kubernetes topology (namespaces, Services, PVCs, secrets) both application repos assume exists. A change here that renames a Service, moves a namespace, or changes a port silently breaks whichever application repo's hardcoded/default connection string assumed the old value.
- This repo itself is deployed onto a cluster managed by **`homelab-deployment`** (registers this repo as an Argo CD `Application`, provisions shared cluster infra like KEDA/OpenBao/storage).

## Stack conventions

- Layout: `charts/fantasy/` is the one source for the homelab and self-hosters. `templates/` holds the manifests (`databases/`, `core-pipeline/`, `browser-worker/`, ...), `files/` the verbatim catalog, Grafana alerts/dashboards and Image Updater manifests, and `values.yaml` the self-host defaults. The homelab layers `values-production.yaml` and `values-images.yaml` (digest pins, written only by Image Updater); draft day adds `values-draft-day.yaml` between them.
- Validate a change by rendering both: `helm template fantasy-infra charts/fantasy -n default -f charts/fantasy/values-production.yaml -f charts/fantasy/values-images.yaml` (add `-f charts/fantasy/values-draft-day.yaml` before the images file for draft day), plus `python3 -m unittest discover tests`. Resource names are what Argo CD tracks: renaming one deletes and recreates it, which for the `fantasy-db` StatefulSet or the `fantasy-broker-data` PVC loses data.
- Secrets are never committed in cleartext — they're materialized via `ExternalSecret` from the shared OpenBao `ClusterSecretStore` (see `charts/fantasy/templates/secrets/externalsecrets.yaml` and `secrets.externalSecrets` in `values-production.yaml`). Any manifest introducing a credential/token/API key must go through that path, not a plain `Secret` with inline `data`/`stringData`.
- Playwright worker pods scale via KEDA off queue depth and are expected to scale to zero when idle — don't suggest a fixed `replicas` count or a plain `Deployment` for that workload without checking whether it should stay a KEDA-scaled resource.
- Postgres/TimescaleDB needs a real PVC — don't suggest `emptyDir` or removing volume claims for stateful services.
- Grafana dashboards (`grafana_dashboard: "1"`) and alert rules (`grafana_alert: "1"`, `charts/fantasy/files/telemetry/`) are ConfigMaps in this repo, loaded by sidecars in homelab-deployment's Grafana; datasources are still provisioned in `homelab-deployment/apps/telemetry/`. Alert rule changes must keep `./tests/launcher_alerts/run.sh` passing (runbook: `docs/cronjob-alerts.md`).

## Critical Environment Variables (fantasy-core-pipeline)

**These MUST be set or the app fails silently at runtime**:

- `JOB_CATALOG_PATH`: The job catalog the Jobs API and the draft endpoints read. Draft runs (`POST /v1/draft/start`, `/v1/draft/practice/sync-job`) are launcher service jobs in `charts/fantasy/files/catalog.yaml`; without the catalog they answer 503.

- `DB_HOST`, `DB_NAME`, `DB_USER`: Postgres topology — must match Service names in `charts/fantasy/templates/databases/postgres.yaml`.
- `REDIS_HOST`, `REDIS_PORT`: Redis connection — must match Service in `charts/fantasy/templates/databases/redis-deployment.yaml`.
- `AMQP_URL`: RabbitMQ credentials — from `rabbitmq-credentials` Secret (an ExternalSecret in the production values).

## Things to double-check in review

- **Env var names and default Service DNS hostnames** (`*.svc.cluster.local`) referenced here must match what `fantasy-core-pipeline`/`fantasy-browser-worker` actually read via `os.getenv(...)`/`process.env` — a namespace or name mismatch between a Deployment's env and the app's fallback default fails silently at runtime, not at manifest-render time.
- **Check `metadata.namespace` consistency** across a change — this codebase has previously had mismatches between a manifest's namespace and another repo's hardcoded fallback default (e.g. `default` vs `databases`).
- **The four draft jobs stay in the catalog** (`draft-listener`, `practice-draft-listener`, `draft-connector`, `practice-draft-connector`) — the draft API requests them by name.
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
