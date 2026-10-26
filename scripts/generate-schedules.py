#!/usr/bin/env python3
"""Generate the public job schedule inventory from the catalog and production manifests."""
import argparse
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'docs/schedules.md'
START = '<!-- BEGIN GENERATED SCHEDULES -->'
END = '<!-- END GENERATED SCHEDULES -->'
LEGACY_CATALOG_ALIASES = {
    'draft-prep-refresher': 'projections-refresh',
    'weekly-ingestor': 'sync-league',
    'weekly-waiver-trigger': 'waiver-trigger',
    'sync-actual-scores-reconcile': 'sync-actual-scores',
    'trade-candidates-prescan': 'trade-candidates',
}


def is_catalog_backed_cronjob(name, catalog_jobs):
    normalized = name.removeprefix('fantasy-')
    catalog_name = LEGACY_CATALOG_ALIASES.get(normalized, normalized)
    return catalog_name in catalog_jobs


def render_catalog(path):
    """Render catalog jobs and dependency chains without consulting legacy alerts."""
    catalog = yaml.load(Path(path).read_text(), Loader=yaml.BaseLoader)
    jobs = catalog.get('jobs', {})
    rows = []
    for name, job in sorted(jobs.items()):
        command = ' '.join(job.get('command', [])) or '—'
        schedules = job.get('schedules', [])
        if schedules:
            schedule_text = '<br/>'.join(
                f'`{item["cron"]}` ({item["tz"]})' for item in schedules
            )
        else:
            schedule_text = '—'
        rows.append(f'| `{name}` | {schedule_text} | `{command}` |')

    sections = [
        '### Catalog jobs',
        '| Job | Schedule | Command |',
        '|---|---|---|',
        *rows,
    ]
    for chain_name, chain in sorted(catalog.get('chains', {}).items()):
        schedule = chain.get('schedule', {})
        schedule_text = (f'`{schedule["cron"]}` ({schedule["tz"]})'
                         if schedule else 'Event driven')
        sections.extend(['', f'## {chain_name}', '', f'Schedule: {schedule_text}', '',
                         '```mermaid', 'flowchart LR'])
        steps = chain.get('steps', {})
        for step_name, step in steps.items():
            latest = step.get('latest')
            label = f'{step_name} (latest {latest})' if latest else step_name
            sections.append(f'    {step_name}["{label}"]')
        for step_name, step in steps.items():
            for dependency in step.get('after', []):
                sections.append(
                    f'    {dependency["step"]} -->|{dependency["on"]}| {step_name}'
                )
        sections.append('```')
    return '\n'.join(sections)


def inventory(catalog_only=False, catalog_path=None):
    catalog_path = Path(catalog_path or ROOT / 'base/launcher/catalog.yaml')
    if catalog_only:
        if not catalog_path.exists():
            raise FileNotFoundError(f'catalog not found: {catalog_path}')
        return '\n'.join([START, render_catalog(catalog_path), END])

    catalog_jobs = {}
    if catalog_path.exists():
        catalog_jobs = yaml.load(
            catalog_path.read_text(), Loader=yaml.BaseLoader
        ).get('jobs', {})
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
        if len(alerts) != 2 and not is_catalog_backed_cronjob(name, catalog_jobs):
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
    sections = [START]
    if catalog_path.exists():
        sections.extend([render_catalog(catalog_path), ''])
    if rows:
        sections.extend([
            '### Legacy CronJobs',
            '| CronJob | Cron expression | Timezone | What runs | Alerts |',
            '|---|---|---|---|---|',
            *rows,
        ])
    sections.append(END)
    return '\n'.join(sections)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Fail if the committed table is stale')
    parser.add_argument(
        '--catalog-only', action='store_true',
        help='Generate from the catalog without rendering legacy CronJobs',
    )
    args = parser.parse_args()
    original = PAGE.read_text()
    before, remaining = original.split(START, 1)
    _, after = remaining.split(END, 1)
    updated = before + inventory(catalog_only=args.catalog_only) + after
    if args.check:
        if updated != original:
            parser.exit(1, 'Schedule table is stale; run python3 scripts/generate-schedules.py\n')
        print('Schedule table matches production CronJobs and alert selectors.')
    else:
        PAGE.write_text(updated)


if __name__ == '__main__':
    main()
