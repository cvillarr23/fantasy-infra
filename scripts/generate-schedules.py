#!/usr/bin/env python3
"""Generate the public job schedule inventory from the job catalog."""
import argparse
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'docs/schedules.md'
START = '<!-- BEGIN GENERATED SCHEDULES -->'
END = '<!-- END GENERATED SCHEDULES -->'


def render_catalog(path):
    """Render catalog jobs and dependency chains."""
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
                         if schedule else 'Manual only')
        sections.extend(['', f'### Chain: {chain_name}', '', f'Schedule: {schedule_text}', '',
                         '```mermaid', 'flowchart LR'])
        steps = chain.get('steps', {})
        for step_name, step in steps.items():
            latest = step.get('latest')
            label = f'{step_name} (latest {latest})' if latest else step_name
            sections.append(f'    {step_name}["{label}"]')
        for step_name, step in steps.items():
            for dependency in step.get('after', []):
                sections.append(
                    f'    {dependency["step"]} -->|{dependency.get("on", "success")}| {step_name}'
                )
        sections.append('```')
    return '\n'.join(sections)


def inventory():
    return '\n'.join([START, render_catalog(ROOT / 'base/launcher/catalog.yaml'), END])


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
        print('Schedule table matches the job catalog.')
    else:
        PAGE.write_text(updated)


if __name__ == '__main__':
    main()
