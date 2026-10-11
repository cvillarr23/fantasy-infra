# fantasy-infra

Kubernetes manifests for the fantasy platform: API, UI, browser-worker Jobs,
scheduled pipelines, Postgres/TimescaleDB, Redis, RabbitMQ and telemetry.
Argo CD reconciles the production overlay; Image Updater commits application digest pins.

This project is unofficial and not affiliated with ESPN or Disney. Read the
[disclaimer and scope](https://github.com/cvillarr23/fantasy-project/blob/main/DISCLAIMER.md).

## Documentation

Start with the [infrastructure guide](docs/index.md). The shared docs site imports
this repository's `docs/` under `/infra` and rebuilds after docs changes land on `main`.

| Topic | Guide |
|---|---|
| GitOps, overlays and running digests | [Deployment](docs/deployment.md) |
| Services, access boundaries, queues and DNS | [Networking](docs/networking.md) |
| Storage, backup, restore and reader role | [Databases](docs/databases.md) |
| Generated CronJob inventory | [Schedules](docs/schedules.md) |
| Incident recovery and rollback | [Operations](docs/operations.md) |
| OpenBao and credential delivery | [Secrets](docs/secrets.md) |

## Repository layout

| Path | Purpose |
|---|---|
| `base/` | Application workloads, stateful stores, auth proxy, scaler, dashboards and alerts |
| `overlays/production/` | Configuration, ExternalSecrets and application digest pins |
| `overlays/draft-day/` | Production plus draft-window resource and warm-worker patches |
| `scripts/` | Image refresh, draft environment preflight and schedule generation |
| `tests/` | Offline render checks and isolated broker/alert integration suites |

Shared controllers, model services, DNS and edge routing live in
[homelab-deployment](https://github.com/cvillarr23/homelab-deployment).

## Validate locally

```bash
kubectl kustomize overlays/production/ > /tmp/fantasy-production.yaml
kubectl kustomize overlays/draft-day/ > /tmp/fantasy-draft-day.yaml
./scripts/preflight-draft-sync-env.sh
python3 scripts/generate-schedules.py --check
```

Rendered configuration is deployment-specific; keep it private. For a full docs build,
follow the site's
[local build instructions](https://github.com/cvillarr23/fantasy-project/blob/main/docs/contributing/docs-site-deploys.md).
