---
title: Application outcomes and trade jobs
description: The rumor-action outcome alert, its provisioning handoff, and how the trade scan and trade inbox jobs behave.
sidebar_position: 10
---

Use this page when the rumor-action outcome alert fires, or when you need the schedule and safety settings of `trade-scan` and `trade-inbox`. For launcher run alerts, see [Job alerts](cronjob-alerts.md).

## Rumor-action application outcome

**Rumor action incomplete or failed processing** (`rumor-action-trigger-failure`) complements the launcher's run-failed alert. The launcher runs `python -m src.services.rumor_action_service` daily at 21:45 and as the last step of Tuesday's `tuesday-weekly` chain, not the HTTP endpoint. Both paths record one `execution_log` row with transaction type `rumor_action_trigger` and bot `rumor_action_service`.

| Property | Value |
|---|---|
| Rule file | `base/telemetry/rumor-action-outcome-alert.yaml` |
| Evaluation | Every 5m |
| Fires | Immediately when any `failure` or `partial_failure` was recorded in the last 26h |
| Dashboard | **Fantasy → Fantasy Execution Log Overview → Rumor Action Outcomes** (panel 6) |

:::note[Why it doesn't select only the latest run]
Until core-pipeline#192 fixes retry and watermark semantics, an empty retry could otherwise erase evidence of lost work. A recovered retry stays visible until the original failure ages out. This is an observation window, not proof that the error remains unresolved.
:::

### What counts as healthy

| Healthy | Alerts |
|---|---|
| No new signals after a successful fetch | Missing identity mappings for proposed actions |
| Low-confidence or non-actionable signals | Incomplete accounting |
| Superseded signals | Evaluation failures |
| No beneficial move | Publish failures |
| Already-queued actions | |

Signals and actions have separate counts, since one signal may produce multiple actions. “Accepted” means queued by the core API, not executed in ESPN.

### Investigate

1. Open panel 6 and check `reason` and the signal reasons.
2. Check the proposed, accepted, duplicate, missing and failed counts.
3. Check the watermark range and `error_message`.
4. Use `job_name` for pod logs.
5. Look for `rumor_action_outcome` in Loki if PostgreSQL logging failed.

| Situation | What happens |
|---|---|
| Empty query | Returns zero. |
| Database query error | Uses Grafana's Error state. |
| Run couldn't persist its outcome | This SQL rule can't detect it. Inspect the log fallback and Kubernetes alerts. |
| Missing runs | Covered by #118, not by a zero-action threshold. |

:::danger[Don't blindly rerun]
#192 is still responsible for retry safety and watermark recovery. Investigate unresolved ESPN identities or input data before retrying.
:::

The app signal also covers HTTP invocations, and its failure count is per invocation, not per launcher run.

### Provisioning ownership and rollout

```mermaid
flowchart LR
    Core[1. core-pipeline migration<br/>outcome_details JSONB] --> Remove[2. homelab-deployment drops<br/>old rule ConfigMap]
    Remove --> Infra[3. fantasy-infra deploys rule<br/>same UID, group, folder, filename]
    Infra --> Verify[4. Verify rule and panel 6]
```

1. Deploy core-pipeline's additive `outcome_details JSONB` migration and service changes. Migrations run at API startup, so confirm the column exists before expecting job outcome writes or opening the new dashboard panel.
2. Remove homelab-deployment's old `rumor-action-trigger-failure.yaml` from its alerting ConfigMap generator and deployed ConfigMap. Don't provision `deleteRules`: the existing Grafana rule is retained until fantasy-infra takes ownership.
3. Deploy this fantasy-infra rule, retaining the old UID/group, folder and provisioning filename.
4. Verify the Grafana rule shows the 26h SQL, 5m cadence and panel 6 link, and that the new table displays structured outcomes. No live Jobs need to be triggered.

:::warning[One owner per rule]
Never leave two ConfigMaps supplying the same file or rule.
:::

Run `./tests/rumor_action_alert/run.sh` (Python/PyYAML and Docker). It tests the committed SQL on isolated PostgreSQL 16 for:

- no data and a healthy no-op
- total and partial failure, and failure then success
- unrelated rows and legacy rows
- exact window boundaries

The launcher's own rules are tested in `./tests/launcher_alerts/run.sh`.

## Outgoing trade scan

`trade-scan` runs in the worker image and stages proposals for separate human approval only.

| Setting | Value |
|---|---|
| Schedule | Wednesday and Friday 13:00 America/Chicago |
| Attempts | One per league/team/Chicago calendar date, shared with manual runs |
| Process bound / run deadline | 15 minutes / 16 minutes |
| Late start | Up to 30 minutes (`catch_up`), counting time queued behind the pod cap |
| Retries | None: `max_attempts` stays 1 |
| Profile | `worker-scan`: the worker secrets and config, plus the LLM keys |
| Image | `WORKER_IMAGE`, the digest the ScaledJob runs |
| Producers | No `trade_proposals` producer/scaler pair, on purpose |

- Off-season, duplicate and no-target outcomes are successful no-ops.
- Failures have no automatic retry. Inspect `trade_scan` logs and `execution_log` claims before recovery.
- Only scheduled runs take the daily claim (`RUN_TRIGGER=schedule`). A manual run skips it, which is why the catalog marks the job `acts: true`, so the UI confirms first.
- Pause this job after the configured ESPN league trade deadline, with `PUT /v1/jobs/trade-scan/pause`. Paused slots end `skipped (paused)` and don't alert. `DELETE` the same path to resume.
- See the worker README's outgoing scan section for read-only checks, claim retention and failure recovery.

## Incoming trade inbox

`trade-inbox` runs `node src/bots/trade_inbox.js --refresh`. It only reads ESPN and persists incoming offers in Postgres.

| Setting | Value |
|---|---|
| Schedule | Eight times daily, 08:00 through 22:00 America/Chicago |
| Weekly runs | 56 inbox runs, plus two refreshes from the Wednesday/Friday 13:00 trade scan (between the noon and 14:00 inbox runs) |
| Persistence | The worker CLI defaults to `--check` (no database writes). The scheduled command selects persistence explicitly. |
| Bounds | Ten-minute process, eleven-minute run deadline, thirty-minute late-start allowance (`catch_up`) |
| Concurrency | `overlap: forbid` and no automatic retry (`max_attempts` stays 1). Manual or delayed runs aren't globally serialized with the trade scan. |
| Core URL | Worker default `http://fantasy-core-pipeline.default.svc.cluster.local:8000`, matching trade-scan |
| Score timeout | Defaults to 20000 ms. `TRADE_SCORE_TIMEOUT_MS` stays unset. |

Each active-season refresh does the following:

- reads pending transactions once
- optionally reads history for vanished offers
- makes at most ten sequential core score calls, and reuses unchanged successful scores

Core can read ESPN for cold scoring context. Offseason runs are successful no-ops.

:::warning[No responses are enabled]
`TRADE_RESPONSE_ENABLED` and both shadow thresholds stay unset, and unset `DRY_RUN` remains true. No accept, decline or counter is enabled by this rollout.

The launcher's trade jobs carry no `TRADE_DRY_RUN` or `TRADE_SUBMIT_ENABLED` either, because no catalog profile or bundle sets them, so scans keep dry-filling. Live trade submission is armed only on the queue worker (the `fantasy-browser-worker-scaler` ScaledJob) and still needs a matching human approval.
:::

### Verify

1. After an Argo CD sync, wait for the next slot, then inspect the run: `kubectl logs -n default -l fantasy.job=trade-inbox --tail=200`, and its `run_log` row with `job_name = 'trade-inbox'`.
2. Confirm the image, command, deadline and `Refreshed` outcome (including scoreCalls/exits).
3. A failed run or a `skipped (expired)` slot reports through the [launcher run-failed alert](cronjob-alerts.md#launcher-run-failed). The overnight gap is intentional.

:::note[Don't create test traffic]
Don't start a manual refresh or send test queue messages for verification. Core inbox API endpoints aren't a prerequisite for this job.
:::
