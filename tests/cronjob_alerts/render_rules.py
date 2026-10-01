"""Render base/telemetry's Grafana alert ConfigMaps into a promtool rule file.

Each Grafana rule becomes a Prometheus alert named by its uid, with
expr = (<refId A query>) > <threshold from the condition refId>. Every other
Prometheus query in the rule (the summary's context values) becomes a recording
rule grafana_context:<uid>:<refId>, so promtool checks it too. The tests then run
against exactly the expressions that are committed. The dashboard state and grace
queries are rendered as recording rules for the same fixtures.
"""

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ALERT_DIR = ROOT / "base" / "telemetry"


def threshold_expr(rule, query_ref):
    data = {d["refId"]: d for d in rule["data"]}
    cond = data[rule["condition"]]["model"]
    assert cond["type"] == "threshold", f"{rule['uid']}: condition must be a threshold"
    reduce = data[cond["expression"]]["model"]
    assert reduce["type"] == "reduce" and reduce["expression"] == query_ref, (
        f"{rule['uid']}: threshold must reduce refId {query_ref}"
    )
    (evaluator,) = [c["evaluator"] for c in cond["conditions"]]
    op = {"gt": ">", "lt": "<"}[evaluator["type"]]
    return f"{op} {evaluator['params'][0]}"


def render():
    prom_rules, seen_keys = [], set()
    for path in sorted(ALERT_DIR.glob("*-alert.yaml")):
        cm = yaml.safe_load(path.read_text())
        assert cm["metadata"]["labels"]["grafana_alert"] == "1", f"{path.name}: missing grafana_alert label"
        for key, body in cm["data"].items():
            # Keys become file names in Grafana's shared provisioning dir.
            assert key not in seen_keys, f"duplicate data key {key}"
            seen_keys.add(key)
            (group,) = yaml.safe_load(body)["groups"]  # one group per file, see homelab-deployment
            (rule,) = group["rules"]  # one rule per group, same convention
            assert re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", rule["uid"]), f"{path.name}: invalid Grafana UID"
            name = rule["uid"].replace("-", "_")
            for d in rule["data"]:
                if d["datasourceUid"] != "prometheus":
                    continue
                expr = d["model"]["expr"].strip()
                if d["refId"] == "A":
                    prom_rules.append({
                        "alert": rule["uid"],
                        "expr": f"({expr}) {threshold_expr(rule, 'A')}",
                    })
                else:
                    prom_rules.append({"record": f"grafana_context:{name}:{d['refId']}", "expr": expr})
    # Exercise the committed dashboard's state and grace queries with the same fixtures.
    dashboard_cm = yaml.safe_load(
        (ROOT / "base/core-pipeline/fantasy-cronjob-health-dashboard.yaml").read_text()
    )
    dashboard = json.loads(dashboard_cm["data"]["fantasy-cronjob-health.json"])
    watched = dashboard["templating"]["list"][0]["allValue"]
    targets = {target["refId"]: target for target in dashboard["panels"][0]["targets"]}
    for ref, field in [("A", "state"), ("G", "grace")]:
        prom_rules.append({
            "record": f"grafana_dashboard:fantasy_cronjob_health:{field}",
            "expr": targets[ref]["expr"].replace("$cronjob", watched),
        })
    return {"groups": [{"name": "fantasy-cronjob-alerts", "rules": prom_rules}]}


if __name__ == "__main__":
    yaml.safe_dump(render(), sys.stdout, sort_keys=False)
