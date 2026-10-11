#!/usr/bin/env python3
"""Generate the public CronJob inventory from production manifests and alert rules."""
import argparse
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'docs/schedules.md'
START = '<!-- BEGIN GENERATED SCHEDULES -->'
END = '<!-- END GENERATED SCHEDULES -->'


def inventory():
    rendered = subprocess.check_output(
        ['kubectl', 'kustomize', str(ROOT / 'overlays/production')], text=True
    )
    jobs = sorted(
        (d for d in yaml.safe_load_all(rendered) if d and d.get('kind') == 'CronJob'),
        key=lambda d: d['metadata']['name'],
    )
    rules = []
    for path in sorted((ROOT / 'base/telemetry').glob('cronjob-*-alert.yaml')):
        cm = yaml.safe_load(path.read_text())
        for body in cm['data'].values():
            for group in yaml.safe_load(body)['groups']:
                rules.extend(group['rules'])
    rows = []
    for job in jobs:
        name, spec = job['metadata']['name'], job['spec']
        alerts = []
        for rule in rules:
            data = {d['refId']: d['model'] for d in rule['data']}
            expr = data['A']['expr']
            selectors = re.findall(r'(?:cronjob|owner_name)=~"([^"]+)"', expr)
            if not any(re.fullmatch(pattern, name) for pattern in selectors):
                continue
            uid = rule['uid']
            if 'missed-schedule' in uid:
                condition = data[rule['condition']]
                seconds = condition['conditions'][0]['evaluator']['params'][0]
                label = f'Missed > {seconds // 60}m'
            else:
                label = 'Latest Job failed'
            alerts.append(f'{label} (`{uid}`)')
        if len(alerts) != 2:
            raise ValueError(f'{name}: expected failed and missed alerts, got {alerts}')
        pod = spec['jobTemplate']['spec']['template']['spec']
        commands = []
        for container in pod.get('initContainers', []) + pod['containers']:
            command = container.get('command', []) + container.get('args', [])
            if '-m' in command:
                commands.append('`' + command[command.index('-m') + 1] + '`')
            elif command and command[0] == 'node':
                commands.append('`' + ' '.join(command[1:]) + '`')
            elif container['name'] == 'draft-prep-refresher':
                commands.append('Projection → K/D-ST → VORP supervisor (up to four weeks)')
            elif container['name'] == 'waiver-trigger':
                commands.append('`src.optimization.waiver_evaluator` (resolves upcoming week)')
            else:
                raise ValueError(f'{name}: add a public summary for {container["name"]}')
        timezone = spec.get('timeZone', 'Unset (controller timezone; UTC in this deployment)')
        rows.append(f'| `{name}` | `{spec["schedule"]}` | {timezone} | {" → ".join(commands)} | {"<br/>".join(alerts)} |')
    return '\n'.join([
        START,
        '| CronJob | Cron expression | Timezone | What runs | Alerts |',
        '|---|---|---|---|---|',
        *rows,
        END,
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Fail if the committed table is stale')
    args = parser.parse_args()
    original = PAGE.read_text()
    before, remaining = original.split(START, 1)
    _, after = remaining.split(END, 1)
    updated = before + inventory() + after
    if args.check:
        if updated != original:
            parser.exit(1, 'Schedule table is stale; run python3 scripts/generate-schedules.py\n')
        print('Schedule table matches production CronJobs and alert selectors.')
    else:
        PAGE.write_text(updated)


if __name__ == '__main__':
    main()
