# CronJob alerts runbook

All ten CronJobs in `base/` are covered by [#75](https://github.com/cvillarr23/fantasy-infra/issues/75). The first slices were [#97 / PR #98](https://github.com/cvillarr23/fantasy-infra/pull/98) and [#101](https://github.com/cvillarr23/fantasy-infra/issues/101). Rules live in `base/telemetry/` as ConfigMaps labeled `grafana_alert: "1"`; homelab-deployment's Grafana alert sidecar loads them into **Fantasy**. Dashboard: **Fantasy → Fantasy CronJob Health**.

Every row has **latest Job failed** and **missed scheduled run** alerts. Schedule fields, not comments that assume UTC, determine local run times.

| CronJob | Schedule | `spec.timeZone` | Missed-run grace |
|---|---|---|---|
| `espn-session-healthcheck` | `0 6 * * 1`, Monday 06:00 UTC | unset; controller uses UTC here | 2h |
| `fantasy-draft-prep-refresher` | `0 5 * * *`, daily 05:00 | America/Chicago | 2h |
| `fantasy-lineup-optimizer` | `30 5 * * 2`, Tuesday 05:30 | America/Chicago | 2h |
| `fantasy-rapid-status-syncer` | `*/10 * * * 4,0,1`, every 10m Thu/Sun/Mon | America/Chicago | 20m |
| `fantasy-rumor-action-trigger` | `0 12 * * *`, daily 12:00 | America/Chicago | 2h |
| `fantasy-rumor-syncer` | `*/30 * * * *`, every 30m | America/Chicago | 60m |
| `fantasy-stale-draft-job-sweeper` | `*/15 * * * *`, every 15m | America/Chicago | 30m |
| `fantasy-weekly-ingestor` | `0 3 * * 2`, Tuesday 03:00 | America/Chicago | 2h |
| `fantasy-weekly-waiver-trigger` | `0 6 * * 2`, Tuesday 06:00 | America/Chicago | 2h |
| `rumor-digest-bot` | `0 11 * * *`, daily 11:00 | America/Chicago | 2h |

Daily/weekly runs retain the original 2h scheduling grace. Frequent jobs get two cadences: 20m for rapid status, 30m for the high-impact sweeper, and 60m for rumor ingestion. Waiting 2h would hide 12, 8, and 4 missed runs respectively. Rules evaluate every 5m and fire **strictly above** the grace; expect up to another 5m before evaluation. Grace concerns Job creation, not runtime or completion.

## States and alert meaning

| State | Evidence | Alert |
|---|---|---|
| Missed schedule | Expected next run is older than that job's grace | Fantasy CronJob missed its scheduled run (daily/weekly or cadence-specific rule) |
| Failed Job | Newest owned Job has `kube_job_failed{condition="true"} == 1` | Fantasy CronJob latest Job failed |
| Succeeded | Latest Job completed and last-success time advanced | none |

The table shows last scheduled/successful/next times, latest Job, active count, and grace for all ten jobs by default. A running Job is not proven successful merely because no failure condition exists: check **Active Jobs** and **Last successful**. Missing input series may leave fields blank; inspect telemetry before interpreting the state.

A successful optimizer with zero swaps is a normal no-op. Its log panel distinguishes `Lineup already optimal` from `Published N lineup swap(s)`. Missing input fails the Job after [core-pipeline#178](https://github.com/cvillarr23/fantasy-core-pipeline/issues/178); empty waiver pools fail after [#181](https://github.com/cvillarr23/fantasy-core-pipeline/issues/181).

## Fantasy CronJob latest Job failed

Only the newest Job per CronJob counts, selected by creation time and joined on `exported_namespace`/`job_name`. Old failures retained by history limits do not keep firing. A newer successful run clears the alert; a newer active run also supersedes an older failure. The summary includes Job name, creation time, and last successful run.

Read-only investigation:

```bash
kubectl describe job <job_name> -n default
kubectl logs job/<job_name> -n default --all-containers
# Init containers can fail before the main container starts:
kubectl logs job/<job_name> -n default -c sync-league       # weekly ingestor
kubectl logs job/<job_name> -n default -c sync-projections  # draft refresher
```

If pods are gone, use Loki `{namespace="default", pod=~"<job_name>-.*"}`. Fix the dependency and allow the next scheduled run to recover. This verification procedure does not create Jobs or rerun workloads.

### What each job depends on

| CronJob | Check first |
|---|---|
| `espn-session-healthcheck` | ESPN reachability and `espn-session-secrets` (`espn_s2`/`swid`), league configuration, Postgres execution logging. Cookies require human refresh when expired. |
| `fantasy-draft-prep-refresher` | `sync-projections` init container, external projections, season/week/team configuration, Postgres credentials, then `vorp-matrix`. It supplies target-week VORP for optimizer and waiver decisions. |
| `fantasy-lineup-optimizer` | That morning's 05:00 refresher, roster/weekly-stat rows, target-week VORP and prior-week fallback, Postgres, core API, trigger API key and downstream lineup processing. Zero swaps is success. |
| `fantasy-rapid-status-syncer` | Player-status source, Postgres, Redis and RabbitMQ credentials/connectivity; Thu/Sun/Mon are the only scheduled days. |
| `fantasy-rumor-action-trigger` | Rumor sync and today's 11:00 digest, rumor-signals rows, team/season config, Postgres, Redis, core API and downstream action processing. A successful Job is not proof of endpoint-level success; see the application alert below. |
| `fantasy-rumor-syncer` | External rumor sources, ingestion logs, Postgres and Redis; ingestion feeds the digest/action path. |
| `fantasy-stale-draft-job-sweeper` | `fantasy-core-pipeline` service account and draft-sync-orchestrator RBAC for listing/deleting Jobs, Postgres, RabbitMQ, Redis, and core API teardown. Verification only reads these resources. |
| `fantasy-weekly-ingestor` | `sync-league` init container, ESPN cookies and league config, external play-by-play source, Postgres. It refreshes rosters and historical metrics; it does not replace projection/VORP refresh. |
| `fantasy-weekly-waiver-trigger` | That morning's refresher and exact-week bench/free-agent pools, Postgres, core API and trigger API key, downstream RabbitMQ/browser-worker claims. It has no prior-week fallback. |
| `rumor-digest-bot` | Rumor inputs in Postgres, database/execution-log credentials and the Groq/Gemini LLM fallback credentials. No ESPN cookies or browser required. It runs one hour before the action trigger. |

[core-pipeline#140](https://github.com/cvillarr23/fantasy-core-pipeline/issues/140) owns application success/failure for `POST /v1/trigger/rumor-action`. homelab-deployment already provisions `rumor-action-trigger-failure` and `rumor-digest-bot-failure` from `execution_log`. These Kubernetes alerts complement those signals; they do not duplicate endpoint-level monitoring or validate partially processed actions.

## Fantasy CronJob missed its scheduled run

Each missed rule evaluates `time() - kube_cronjob_next_schedule_time` against its group's grace. The seven daily/weekly jobs retain UID `fantasy-cronjob-missed-schedule`; three additional rules use suffixes `rapid`, `sweeper`, and `rumor` (UIDs stay within Grafana's 40-character limit). Each provisioning file/group has one rule, matching the Grafana convention and test renderer.

kube-state-metrics calculates the next timestamp from the schedule and timezone, advancing from the last scheduled run. The optimizer's missing 05:30 Tuesday run alerts after 07:30 Central in both CDT and CST. Rapid status naturally advances to the next Thu/Sun/Mon slot: a long inactive-day gap alone does not alert. A missed active-day slot remains overdue even if the grace crosses midnight.

Suspended jobs intentionally alert once overdue. Summaries show overdue duration, last success and suspension. All missed rules use `noDataState: Alerting` when the group's query has no series and `execErrState: Error`; missing one member of a multi-job group does not itself create a per-job no-data alert. A Job that starts but hangs needs separate runtime/completion monitoring.

```bash
kubectl get cronjob <cronjob> -n default -o wide
kubectl describe cronjob <cronjob> -n default
kubectl get deploy kube-state-metrics -n default
```

Check SUSPEND, LAST SCHEDULE, TIMEZONE and events (`MissSchedule`, `FailedCreate`, too many missed start times).

## Read-only live checks

```bash
kubectl get cronjobs -n default -o wide
kubectl get jobs -n default --sort-by=.metadata.creationTimestamp

promq() {
  kubectl get --raw "/api/v1/namespaces/default/services/prometheus:9090/proxy/api/v1/query?query=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$1")"
}
# Run each exact committed alert expression; empty results can mean healthy.
python3 tests/cronjob_alerts/render_rules.py
```

For each name in the inventory, query:

```promql
kube_cronjob_next_schedule_time{exported_namespace="default",cronjob="<cronjob>"}
kube_cronjob_status_last_schedule_time{exported_namespace="default",cronjob="<cronjob>"}
kube_cronjob_status_last_successful_time{exported_namespace="default",cronjob="<cronjob>"}
kube_job_owner{exported_namespace="default",owner_kind="CronJob",owner_name="<cronjob>"}
kube_job_created * on (exported_namespace,job_name) group_left (owner_name)
  kube_job_owner{exported_namespace="default",owner_kind="CronJob",owner_name="<cronjob>"}
kube_job_failed * on (exported_namespace,job_name) group_left (owner_name)
  kube_job_owner{exported_namespace="default",owner_kind="CronJob",owner_name="<cronjob>"}
```

Successful Jobs can have **no** `kube_job_failed` series. Confirm their Job conditions read-only and record this as expected absence, not a broken scrape. Never manufacture a failed Job to get a series. `exported_namespace` is the object's namespace; `namespace` is kube-state-metrics' namespace. Retention limits historical verification.

After merge, let ArgoCD apply the PR. Verify read-only that Grafana lists all five rule UIDs, their query groups match this inventory, and the dashboard shows all ten rows with the right grace. Only then tick the remaining #75 checkboxes. Do not close #75 until all ten are verified.

## Testing and adding coverage

Run `./tests/cronjob_alerts/run.sh`. It renders committed alert and dashboard queries and runs Docker promtool fixtures covering all ten failures, success after historical failures, grace boundaries, suspended/resumed runs, CDT/CST, UTC healthcheck, rapid-status inactive days and unwatched jobs. Dashboard state and grace queries are checked against the same fixtures. Grafana's no-data policy requires live provisioning verification; promtool tests PromQL, not Grafana's evaluator.

For a new CronJob, update all failure selectors/context, select or add a missed-rule grace group, register new ConfigMaps, update dashboard inventory/grace and this table, then add firing/non-firing fixtures. Keep each rule's provisioning key and UID unique. Validate both production and draft-day renders.
