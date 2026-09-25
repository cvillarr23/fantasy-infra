# ADR 0001: Unified RabbitMQ Launcher for Cron-Triggered Work

## Status

Proposed

## Context

`base/core-pipeline/` currently defines 8 separate `batch/v1` CronJobs, each independently scheduling a piece of pipeline work:

| CronJob | Schedule | Mechanism |
|---|---|---|
| `fantasy-rumor-syncer` | `*/30 * * * *` | `python -m src.ingestion.sync_rumors` — pure ingestion, no queue involvement |
| `fantasy-draft-prep-refresher` | `0 5 * * *` | initContainer `sync_projections` → main `vorp_matrix` (two-phase, ordered) |
| `fantasy-weekly-ingestor` | `0 3 * * 2` | initContainer `sync_league --season 2026` → main `metrics_extractor` (two-phase, ordered) |
| `fantasy-lineup-optimizer` | `0 4 * * 2` | `lineup_solver` POSTs to FastAPI `/v1/trigger/*`, which publishes onto the `lineup_updates` queue consumed by `lineup_bot.js` |
| `fantasy-weekly-waiver-trigger` | `30 4 * * 2` | shell wrapper resolves the current week, then runs `waiver_evaluator`, feeding `waiver_sniper.js` via `waiver_actions` |
| `fantasy-stale-draft-job-sweeper` | `*/15 * * * *` | `sweep_stale_draft_jobs`; already carries `AMQP_URL` |
| `fantasy-rapid-status-syncer` | `*/10 * * * 4,0,1` | `sync_players`; already carries `AMQP_URL` |
| `fantasy-rumor-action-trigger` | `0 12 * * *` | `rumor_action_service`, acts on `rumor_signals` rows written by `rumor-digest-bot` |

Each CronJob needs its own Grafana "did this run" alert (tracked separately in #75) to catch a missed or failed execution — 8 independent surfaces to monitor, each with its own `restartPolicy`/`backoffLimit` retry semantics.

RabbitMQ + KEDA scale-to-zero consumption already exists and is proven: `base/databases/rabbitmq-deployment.yaml` pre-declares durable queues (`lineup_updates`, `waiver_actions`, `draft_targets`), each paired with a DLX/DLQ for failed messages, and `base/browser-worker/keda-scaledjob.yaml` scales a `ScaledJob` from zero based on `QueueLength` on those same queues. Queues must be pre-declared by the broker itself, since KEDA's rabbitmq scaler errors (rather than reporting zero) against a queue that doesn't exist yet and no scaled-to-zero consumer is running to create it.

`fantasy-infra` is manifests-only: none of the actual job logic (`sync_rumors`, `vorp_matrix`, `waiver_evaluator`, `sweep_stale_draft_jobs`, etc.) lives here — it's all in the sibling `fantasy-core-pipeline` (Python) repo, with consumer bots (`lineup_bot.js`, `waiver_sniper.js`) in `fantasy-browser-worker` (JS). A unified launcher needs new scheduler code and adapted/new consumer code in those repos; this ADR evaluates the shape of that work without implementing it there.

## Decision

Recommend a **phased pilot**, not a wholesale migration, starting with the jobs that already touch RabbitMQ.

### Migration fit, by job

**Good fit** — already RabbitMQ-adjacent or naturally event-shaped, smallest consumer-side lift:
- `fantasy-stale-draft-job-sweeper` (already carries `AMQP_URL`)
- `fantasy-rapid-status-syncer` (already carries `AMQP_URL`)
- `fantasy-lineup-optimizer` (already ends in a queue publish downstream)
- `fantasy-rumor-action-trigger` (single-step, acts on rows written by another job)

**Needs care** — two-phase execution via `initContainer` ordering. A launcher would need to either publish two chained messages (phase-1 message triggers a consumer that, on success, publishes phase-2) or have a single consumer run both steps in sequence, which reproduces the current CronJob's ordering guarantee inside a worker instead of a manifest:
- `fantasy-draft-prep-refresher` (`sync_projections` → `vorp_matrix`)
- `fantasy-weekly-ingestor` (`sync_league` → `metrics_extractor`)
- `fantasy-weekly-waiver-trigger` (week resolution → `waiver_evaluator`)

**Questionable fit** — pure scheduled ingestion with no natural "worker reacts to an event" shape. A scheduler firing a message onto a queue vs. a CronJob directly invoking the script is a wash in complexity, with no observability or retry benefit gained:
- `fantasy-rumor-syncer`

Recommendation: do not migrate `fantasy-rumor-syncer` at all under this proposal — it stays a plain CronJob.

### Target architecture (for the pilot batch only)

- One lightweight in-cluster scheduler process — either a new endpoint on the existing `fastapi-deployment` or a small standalone Deployment — replaces the pilot batch's 4 CronJobs. On each job's schedule, it publishes a message onto a dedicated, pre-declared durable queue (following the existing `lineup_updates`/`waiver_actions`/`draft_targets` pattern: one queue per job type, each paired with a DLX/DLQ).
- Consumers are adapted or new workers in `fantasy-core-pipeline`/`fantasy-browser-worker`, scaled via a KEDA `ScaledJob` `rabbitmq` trigger with `mode: QueueLength`, mirroring `base/browser-worker/keda-scaledjob.yaml`. Scale-to-zero stays the default; `overlays/draft-day` can still force a warm minimum the same way it already does for the browser worker.
- This consumer/scheduler code is out of scope for `fantasy-infra` and is not implemented as part of this ADR — see Follow-ups.

## Consequences

- Collapses the pilot batch's 4 per-cron Grafana alerts (#75) into one launcher-health alert plus per-queue consumer-lag alerts — fewer, more meaningful signals.
- Retry becomes centralized via the existing DLQ pattern instead of each CronJob's own `restartPolicy`/`backoffLimit`.
- Loses the current CronJob property that one manifest fully describes one job's lifecycle end-to-end; failure diagnosis now spans a scheduler, a queue, and a separate worker repo.
- The "needs care" batch (two-phase jobs) gains real complexity if migrated later — ordering has to be reconstructed either as chained messages or as a single fatter consumer step. This ADR does not recommend migrating them yet.
- `fantasy-rumor-syncer` sees no benefit from migration and should stay as-is.

## Alternatives considered

- **Migrate all 8 at once**: rejected — the two-phase jobs would need ordering logic invented from scratch under time pressure, and `rumor-syncer` gains nothing from the change; the blast radius doesn't match the issue's own `priority/low` framing.
- **Keep all 8 CronJobs, rely solely on #75's per-cron alerts**: valid fallback if the pilot doesn't show a clear win; #75 already solves the immediate "did this run" visibility gap independent of this migration.

## Follow-ups

Not filed as part of this ADR (cross-repo, and this repo has no way to track work in sibling repos' issue trackers automatically):
- `fantasy-core-pipeline`: build the scheduler/launcher that publishes the pilot batch's 4 job messages onto RabbitMQ.
- `fantasy-core-pipeline` / `fantasy-browser-worker`: adapt or add consumers for the pilot batch's 4 jobs, plus the matching KEDA `ScaledJob` + queue/DLQ manifests in `fantasy-infra` once consumer code exists.
