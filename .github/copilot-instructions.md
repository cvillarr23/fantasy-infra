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
- Grafana dashboards for this pipeline are provisioned from `homelab-deployment/apps/telemetry/`, not from this repo — a PR adding dashboard JSON here is out of place (see `homelab-deployment#1` for the tracked follow-up to change that).

## Things to double-check in review

- Env var names and default Service DNS hostnames (`*.svc.cluster.local`) referenced here must match what `fantasy-core-pipeline`/`fantasy-browser-worker` actually read via `os.getenv(...)`/`process.env` — a namespace or name mismatch between a Deployment's env and the app's fallback default fails silently at runtime, not at manifest-render time.
- Check `metadata.namespace` consistency across a change — this codebase has previously had mismatches between a manifest's namespace and another repo's hardcoded fallback default (e.g. `default` vs `databases`).
- Resource requests/limits should stay conservative (this runs on homelab-scale nodes) — flag any request/limit bump that isn't clearly justified.
