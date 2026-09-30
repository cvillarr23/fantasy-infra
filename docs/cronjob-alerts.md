# CronJob alerts runbook

Grafana alert rules for the weekly lineup automation ([#97](https://github.com/cvillarr23/fantasy-infra/issues/97), a slice of [#75](https://github.com/cvillarr23/fantasy-infra/issues/75)). The rules live in `base/telemetry/` as ConfigMaps labeled `grafana_alert: "1"`. homelab-deployment's Grafana `alert-sidecar` loads them into the **Fantasy** alert folder. Dashboard: **Fantasy → Fantasy CronJob Health** (`base/core-pipeline/fantasy-cronjob-health-dashboard.yaml`).

| CronJob | Schedule (`America/Chicago`) | Alerts |
|---|---|---|
| `fantasy-weekly-ingestor` | `0 3 * * 2`, Tuesday 03:00 | latest Job failed |
| `fantasy-lineup-optimizer` | `30 5 * * 2`, Tuesday 05:30 | latest Job failed, missed scheduled run |
| `fantasy-weekly-waiver-trigger` | `0 6 * * 2`, Tuesday 06:00 | latest Job failed |

The optimizer's main input is the upcoming week's `vorp_score`, written by `fantasy-draft-prep-refresher` (daily 05:00), so it runs 30 minutes after that. If those rows are missing it falls back to prior weeks' rows, and it fails the Job when last week's rows are missing too (fantasy-core-pipeline#178). So a failed optimizer run usually traces back to that morning's refresher, or last week's.

## Three states, and which ones alert

| State | What the metrics show | Alert |
|---|---|---|
| **Missed schedule** | `kube_cronjob_next_schedule_time` is more than 2h in the past, so the controller never created that run's Job (CronJob suspended, controller down, bad schedule) | *Fantasy CronJob missed its scheduled run* |
| **Failed Job** | The newest Job for the CronJob has `kube_job_failed{condition="true"} == 1` | *Fantasy CronJob latest Job failed* |
| **Succeeded** | The newest Job completed; `kube_cronjob_status_last_successful_time` advanced | none |

A successful optimizer run that publishes **zero swaps** is the "Succeeded" state: the lineup was already optimal. It never alerts. The logs show which kind of success it was: `Lineup already optimal for Team …` means no swaps; `Published N lineup swap(s) …` means it acted.

Missing optimizer input shows up as a **Failed Job** since [fantasy-core-pipeline#178](https://github.com/cvillarr23/fantasy-core-pipeline/issues/178): the optimizer logs `Lineup optimizer cannot evaluate Team … Season … Week …: <reason>` and exits 1. Before that it logged `No active roster metrics found …` and exited 0: the silent 2026-09-22 and 2026-09-29 runs these alerts exist for. Every run also logs `Evaluating Team … from weekly_stats.vorp_score (<mode>, …)`, which shows whether it used the target week's projections or fell back to prior weeks.

## Fantasy CronJob latest Job failed

Fires for the newest Job of each watched CronJob only. Older failures still kept by `failedJobsHistoryLimit` don't count, and the alert resolves once the next run succeeds. The summary names the Job, its creation time and the CronJob's last successful run.

1. Find out why it failed. Everything below is read-only:
   ```bash
   kubectl describe job <job_name> -n default          # conditions: BackoffLimitExceeded, DeadlineExceeded...
   kubectl logs job/<job_name> -n default --all-containers
   # weekly-ingestor has an initContainer (sync-league); a failure there never starts the main container:
   kubectl logs job/<job_name> -n default -c sync-league
   ```
   The Job's pods may already be gone. In that case use Loki: `{namespace="default", pod=~"<job_name>-.*"}`, or the dashboard's log panel for the optimizer.
2. Check what each failure depends on:
   - **Optimizer and waiver trigger** (`… cannot evaluate Team … Season … Week …: <reason>`): both read the target week's `vorp_score` from that morning's `fantasy-draft-prep-refresher` (05:00). Check its latest Job first. The waiver trigger has no prior-week fallback, so a missing or failed refresh fails it directly (`no bench or free-agent players with weekly_stats rows …`).
   - **Ingestor:** its play-by-play metrics don't feed either Tuesday decision directly, but a missing week leaves gaps in `weekly_stats` history.
3. Fix the cause, then either wait for next Tuesday's run or re-run it by hand (`kubectl create job --from=cronjob/<name> ...`). A manual re-run changes the cluster and publishes real lineup or waiver actions, so it's your call, not part of this check.

## Fantasy CronJob missed its scheduled run

`time() - kube_cronjob_next_schedule_time > 7200` for `fantasy-lineup-optimizer`. kube-state-metrics works out the next run from the CronJob's `schedule` **and** `timeZone`, and only moves it forward once a run is scheduled. So this fires about 07:30 Central on a Tuesday whose 05:30 run never started, in CDT and in CST alike. The summary shows how overdue the run is, the last successful run and `suspended`. This alert also fires, deliberately, when kube-state-metrics has no data at all.

1. `kubectl get cronjob fantasy-lineup-optimizer -n default -o wide`: check `SUSPEND`, `LAST SCHEDULE` and `TIMEZONE`.
2. `kubectl describe cronjob fantasy-lineup-optimizer -n default`: look at Events for `MissSchedule`, `FailedCreate`, or too many missed start times.
3. Check that kube-state-metrics is up: `kubectl get deploy kube-state-metrics -n default`.

## Read-only live checks

The CronJobs and their recent Jobs, newest last:

```bash
kubectl get cronjob fantasy-lineup-optimizer fantasy-weekly-ingestor fantasy-weekly-waiver-trigger -n default -o wide
kubectl get jobs -n default --sort-by=.metadata.creationTimestamp | grep -E 'lineup-optimizer|weekly-ingestor|weekly-waiver-trigger'
```

Job names end in the scheduled minute since the Unix epoch. For example, `fantasy-lineup-optimizer-29844540` × 60 = 2026-09-29 09:00 UTC = 04:00 CDT.

Queries can go to Prometheus through the API server's service proxy, with no port-forward. Run the committed alert expression, refId A, like this:

```bash
promq() {
  kubectl get --raw "/api/v1/namespaces/default/services/prometheus:9090/proxy/api/v1/query?query=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$1")"
}
promq "$(python3 tests/cronjob_alerts/render_rules.py | python3 -c 'import sys,yaml;[print(r["expr"]) for r in yaml.safe_load(sys.stdin)["groups"][0]["rules"] if r.get("alert")=="fantasy-cronjob-job-failed"]')"
```

Individual inputs:

```promql
# When each CronJob last ran, last succeeded, and will next run (unix seconds)
kube_cronjob_status_last_schedule_time{cronjob=~"fantasy-lineup-optimizer|fantasy-weekly-ingestor|fantasy-weekly-waiver-trigger"}
kube_cronjob_status_last_successful_time{cronjob=~"fantasy-lineup-optimizer|fantasy-weekly-ingestor|fantasy-weekly-waiver-trigger"}
kube_cronjob_next_schedule_time{cronjob=~"fantasy-lineup-optimizer|fantasy-weekly-ingestor|fantasy-weekly-waiver-trigger"}

# Seconds overdue; the missed-run alert fires above 7200
time() - kube_cronjob_next_schedule_time{cronjob="fantasy-lineup-optimizer"}

# Failed Jobs still in history, per CronJob; the alert only counts the newest Job
kube_job_failed{condition="true"} == 1
  and on (exported_namespace, job_name) kube_job_owner{owner_kind="CronJob", owner_name=~"fantasy-lineup-optimizer|fantasy-weekly-ingestor|fantasy-weekly-waiver-trigger"}
```

`exported_namespace` is the object's namespace; `namespace` is where kube-state-metrics itself runs. Prometheus here keeps only about a week of data, so older runs are only visible through `kubectl` and Loki.

## Testing the rules

```bash
./tests/cronjob_alerts/run.sh
```

`render_rules.py` turns the committed Grafana rules into a Prometheus rule file. `tests.yaml` then runs these scenarios through `promtool test rules` in Docker (`prom/prometheus:v3.15.0`):
- success, including zero swaps
- an old failure followed by success
- the latest Job failed
- a run missed in CDT and in CST

## Adding another CronJob (#75)

Add its name to the `owner_name=~` / `cronjob=~` regexes in `base/telemetry/*-alert.yaml`: both queries in the failed rule, and all three in the missed rule. Adjust the missed rule's 7200s grace if 2h doesn't suit that schedule. Then add a case to `tests/cronjob_alerts/tests.yaml`. The dashboard's CronJob variable already lists every CronJob.
