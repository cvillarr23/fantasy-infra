# fantasy-infra

Kubernetes manifests for the fantasy platform: API, UI, browser-worker Jobs,
scheduled pipelines, Postgres/TimescaleDB, Redis, RabbitMQ and telemetry.
Argo CD deploys the `charts/fantasy` Helm chart with its production values; Image Updater commits application digest pins.

This project is unofficial and not affiliated with ESPN or Disney. Read the
[disclaimer and scope](https://github.com/cvillarr23/fantasy-project/blob/main/DISCLAIMER.md).

## Documentation

Start with the [infrastructure guide](docs/index.md). The [shared docs site](https://fantasy-docs.roo-net.cc) imports
this repository's `docs/` under `/infra` and rebuilds after docs changes land on `main`.

| Topic | Guide |
|---|---|
| GitOps, values files and running digests | [Deployment](docs/deployment.md) |
| Services, access boundaries, queues and DNS | [Networking](docs/networking.md) |
| Storage, backup, restore and reader role | [Databases](docs/databases.md) |
| Generated job inventory | [Schedules](docs/schedules.md) |
| Incident recovery and rollback | [Operations](docs/operations.md) |
| OpenBao and credential delivery | [Secrets](docs/secrets.md) |

## Repository layout

| Path | Purpose |
|---|---|
| `charts/fantasy/` | The Helm chart: application workloads, stateful stores, auth proxy, scaler, dashboards and alerts. Also for [self-hosting](https://fantasy-docs.roo-net.cc/installation/kubernetes). |
| `charts/fantasy/values-production.yaml` | The homelab's configuration, ExternalSecrets, ingress and telemetry |
| `charts/fantasy/values-draft-day.yaml` | Production plus draft-window resources and a warm worker |
| `charts/fantasy/values-images.yaml` | Application digest pins, written by Image Updater |
| `scripts/` | Image refresh, draft environment preflight and schedule generation |
| `tests/` | Offline render checks and isolated broker/alert integration suites |

Shared controllers, model services, DNS and edge routing live in
[homelab-deployment](https://github.com/cvillarr23/homelab-deployment).

## Validate locally

```bash
cd charts/fantasy
helm lint . -f values-production.yaml -f values-images.yaml
helm template fantasy-infra . -n default -f values-production.yaml -f values-images.yaml > /tmp/fantasy-production.yaml
helm template fantasy-infra . -n default -f values-production.yaml -f values-draft-day.yaml \
  -f values-images.yaml > /tmp/fantasy-draft-day.yaml
cd ../..
python3 scripts/generate-schedules.py --check
python3 -m unittest discover tests
```

Provision [private deployment values](docs/deployment.md#supply-private-deployment-values)
before syncing: league/team IDs, the OAuth allow-list and domain are Secret-backed.
Public renders contain references only; keep live values and private overrides private. For a full docs build,
follow the site's
[local build instructions](https://github.com/cvillarr23/fantasy-project/blob/main/docs/contributing/docs-site-deploys.md).

## License

Code, configuration and documentation are licensed under the [MIT License](LICENSE).
Third-party dependencies retain their own licenses.
