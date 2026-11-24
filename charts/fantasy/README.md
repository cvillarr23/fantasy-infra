# fantasy Helm chart

Installs the fantasy platform into one namespace:
- the core API and the job launcher, with its catalog;
- the KEDA-scaled browser worker;
- the web UI behind oauth2-proxy;
- the Postgres/TimescaleDB, Redis and RabbitMQ stores they share.

The step-by-step guide is [Self-host on Kubernetes](https://fantasy-docs.roo-net.cc/installation/kubernetes). This page is the reference for the chart itself.

```bash
helm install fantasy oci://ghcr.io/cvillarr23/charts/fantasy \
  --namespace fantasy --create-namespace -f my-values.yaml
```

## Helm in this repo, briefly

- **A chart is templates plus values.** `templates/` holds Kubernetes manifests with `{{ }}`
  placeholders. `values.yaml` holds the defaults those placeholders read. You never edit the
  templates to configure an install: you pass your own values file with `-f`.
- **Values layer.** `-f a.yaml -f b.yaml` merges left to right, so later files win key by key.
  Maps merge, but lists (such as `postgres.args`) are replaced whole. `--set key=value`
  overrides a single key. `--set-file key=path` reads a file into a string value, which is how
  you pass a job catalog.
- **`helm template` renders and `helm install` applies.** `helm template fantasy charts/fantasy -f
  my-values.yaml` prints the manifests and touches nothing, so use it to review a change.
  `helm install` applies the manifests and records a release (a Secret in the namespace). After
  that, `helm upgrade` applies changes and `helm rollback` returns to an earlier revision.
- **Argo CD only renders.** An Argo CD Application with a Helm source runs `helm template` and
  applies the output itself. There is no Helm release, so `helm list` shows nothing and rollback
  is Argo CD's.
- **Values are checked.** `values.schema.json` rejects unknown keys and wrong types at install
  time, so a typo such as `tailscal.enabled` fails instead of being silently ignored.

## Prerequisites

- **Kubernetes 1.27 or later**, with a default StorageClass for ReadWriteOnce volumes.
- **[KEDA](https://keda.sh/docs/latest/deploy/)**, which starts browser-worker pods from the
  RabbitMQ queues.
- **External Secrets Operator**, only with `secrets.provider: externalSecrets`.
- **An ingress controller with TLS** for public access, or a port-forward for a trial.
- **A Google OAuth client**, for sign-in through oauth2-proxy.

## Secrets

With the default `secrets.provider: existing`, create these Secrets in the release namespace
before installing. Names and keys are fixed, because the launcher's job profiles reference them
too.

| Secret | Keys | Required |
|---|---|---|
| `database-credentials` | `postgres-password` | Yes |
| `rabbitmq-credentials` | `rabbitmq-user`, `rabbitmq-password`, `amqp-url` | Yes. `amqp-url` is `amqp://<user>:<password>@fantasy-broker.<namespace>.svc.cluster.local:5672/%2f`. |
| `espn-session-secrets` | `espn_s2`, `swid`, `ESPN_LEAGUE_ID`, `ESPN_TEAM_ID` | Yes |
| `trigger-api-key` | `trigger-api-key`, any long random string | Yes |
| `groq-api-key` | `groq_api_key` | Yes, for the API; the job also needs `gemini-api-key` for the LLM profiles |
| `gemini-api-key` | `gemini_api_key` | For jobs on the `worker-scan` and `worker-llm` profiles |
| `oauth2-proxy-credentials` | `client-id`, `client-secret`, `cookie-secret` | Yes. `openssl rand -base64 32 \| tr -- '+/' '-_'` makes a cookie secret. |
| `oauth2-proxy-deployment-config` | `authenticated-emails-list` (one email per line), `redirect-url`, `cookie-domain` | Yes |
| `espn-session-state` | `state.json`, the saved browser session | No; workers fall back to the cookies |
| `ntfy-credentials` | `NTFY_TOKEN` | No |
| `readonly-database-credentials` | `readonly-password` | With `postgres.readonlyRole.enabled` |

With `secrets.provider: externalSecrets`, the chart renders an ExternalSecret for each Secret
whose store path is set in `secrets.externalSecrets.remoteKeys`, reading from
`secrets.externalSecrets.storeRef`. Each path holds the same properties as the keys above, with
these exceptions:
- `amqp-url` is composed from the RabbitMQ user and password.
- The ESPN IDs come from the separate `deployment` path.
- The trigger key's property is `trigger_api_key`.
- The ntfy token's property is `token`.

`values-production.yaml` is a complete example.

## Values

The defaults suit a first self-hosted install: every write to ESPN is off, the stores run
in-cluster, and nothing from the original homelab is rendered. `values.yaml` documents every key.
These are the ones you're most likely to set:

| Key | Default | Purpose |
|---|---|---|
| `league.currentYear` | `"2026"` | The season you play |
| `league.dryRun` | `"true"` | Every bot dry-runs. Set `null` once you trust the install. |
| `worker.tradeDryRun`, `worker.tradeSubmitEnabled` | `"true"`, `"false"` | Live trade submission from the queue worker |
| `launcher.catalog` | bundled | Your job catalog, via `--set-file launcher.catalog=catalog.yaml`. The bundled schedules use `America/Chicago`. |
| `ingress.enabled`, `.host`, `.className`, `.tls` | off | An Ingress to oauth2-proxy |
| `images.<core\|worker\|webUi>.digest` | empty (`latest`) | Pin an image digest |
| `images.<core\|worker\|webUi>.tag` | `latest` | A tag, or `<tag>@sha256:...` as Argo CD Image Updater writes it, which pins that digest |
| `postgres.enabled` / `redis.enabled` | `true` | `false` plus `.host` to use your own |
| `postgres.storage`, `rabbitmq.storage`, `*.storageClassName` | 10Gi, 2Gi, default class | Volumes |
| `otel.httpEndpoint`, `otel.grpcEndpoint` | off | OTLP trace export |
| `notify.ntfyUrl`, `notify.webUiBaseUrl` | off | Phone pushes through ntfy |
| `secrets.provider` | `existing` | `externalSecrets` to have ESO create the Secrets |
| `tailscale.enabled`, `telemetry.*`, `imageUpdater.enabled`, `postgres.readonlyRole.enabled` | off | Homelab integrations: Tailscale ingresses, Grafana alerts and dashboards, Argo CD Image Updater, a Grafana read-only DB role |

Resource names are fixed, such as `fantasy-db` and `fantasy-job-launcher`, so install one
release per namespace.

## Upgrade and uninstall

```bash
helm upgrade fantasy oci://ghcr.io/cvillarr23/charts/fantasy -n fantasy -f my-values.yaml
helm uninstall fantasy -n fantasy
```

A catalog change rolls the launcher and the API through a `checksum/catalog` pod annotation,
because both read the catalog only at startup. `helm uninstall` keeps the Postgres volume, which
comes from a StatefulSet volume claim template. It deletes the RabbitMQ volume and everything
else.

## The original homelab

The homelab's production install is `values-production.yaml`, and the draft-day window layers
`values-draft-day.yaml` on top of it. Until Argo CD deploys the chart, it still deploys
`overlays/production`, and `scripts/compare-renders.py` fails CI if the two renders differ.

Files under `files/`, namely the catalog, Grafana alerts, dashboards and Image Updater manifests,
are verbatim copies from `base/`. After editing those in `base/`, run
`scripts/sync-chart-files.sh`.

```bash
helm template fantasy charts/fantasy -n default -f charts/fantasy/values-production.yaml
python3 scripts/compare-renders.py
python3 -m unittest tests.test_chart
```

## Known limits

- The RabbitMQ broker always runs in-cluster: it owns the queue topology that the KEDA triggers
  and the launcher expect.
