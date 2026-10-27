"""Exercise the committed launcher alert SQL against PostgreSQL, with a frozen transaction clock."""
import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TELEMETRY = ROOT / 'base/telemetry'


def load_rule(filename):
    return rule_from((TELEMETRY / filename).read_text())


def rule_from(configmap_text):
    config = yaml.safe_load(configmap_text)
    assert config['metadata']['labels']['grafana_alert'] == '1'
    (body,) = config['data'].values()
    (group,) = yaml.safe_load(body)['groups']
    assert group['interval'] == '5m'
    (rule,) = group['rules']
    assert rule['data'][0]['datasourceUid'] == 'FantasyFootballDB'
    assert rule['data'][2]['model']['conditions'][0]['evaluator']['type'] == 'gt'
    return rule


def query(rule):
    return rule['data'][0]['model']['rawSql'].strip().rstrip(';')


def threshold(rule):
    return rule['data'][2]['model']['conditions'][0]['evaluator']['params'][0]


def fires(rule, name, expected):
    """Asserts whether the rule's value is above its threshold (so it would fire)."""
    print(f"DO $$ BEGIN IF (({query(rule)}) > {threshold(rule)}) <> {str(expected).lower()} "
          f"THEN RAISE EXCEPTION 'failed: {name}'; END IF; END $$;")


def run(run_id, job, status, finished='1 minute', end_reason=None, root=None):
    reason = 'NULL' if end_reason is None else f"'{end_reason}'"
    return (f"INSERT INTO run_log (id, job_name, status, end_reason, chain_root_id, finished_at) VALUES "
            f"('{run_id}', '{job}', '{status}', {reason}, {'NULL' if root is None else repr(root)}, "
            f"now() - interval '{finished}');")


def reset():
    print('TRUNCATE run_log; TRUNCATE launcher_heartbeat;')


print('''BEGIN;
CREATE TEMP TABLE run_log (
  id TEXT PRIMARY KEY, job_name TEXT, status TEXT, end_reason TEXT,
  attempt INT DEFAULT 1, chain_root_id TEXT, finished_at TIMESTAMPTZ
);
CREATE TEMP TABLE launcher_heartbeat (id SMALLINT PRIMARY KEY, at TIMESTAMPTZ, queue_depths JSONB);
''')

heartbeat = load_rule('launcher-heartbeat-alert.yaml')
assert heartbeat['noDataState'] == 'Alerting'
reset()
fires(heartbeat, 'missing heartbeat row is stale', True)
for age, expected in (('10 seconds', False), ('299 seconds', False), ('301 seconds', True), ('1 hour', True)):
    reset()
    print(f"INSERT INTO launcher_heartbeat VALUES (1, now() - interval '{age}', '{{}}');")
    fires(heartbeat, f'heartbeat {age} old', expected)

dlq = load_rule('launcher-dlq-alert.yaml')
reset()
fires(dlq, 'no heartbeat row does not look like a DLQ', False)
for depths, expected in (('{"job_runs": 3, "job_runs.dlq": 0}', False),
                         ('{"job_runs": 0, "job_runs.dlq": 2}', True),
                         ('{"job_runs": null, "job_runs.dlq": null}', False)):
    reset()
    print(f"INSERT INTO launcher_heartbeat VALUES (1, now(), '{depths}');")
    fires(dlq, f'dlq depths {depths}', expected)

chain = load_rule('launcher-chain-max-duration-alert.yaml')
reset()
fires(chain, 'no runs', False)
for name, rows, expected in (
    ('chain root past max_duration', [run('c1', 'tuesday-weekly', 'timeout', end_reason='max_duration', root='c1')], True),
    ('step timeout is not a chain root', [run('s1', 'lineup', 'timeout', end_reason='max_duration', root='c1')], False),
    ('root timeout for another reason', [run('c1', 'tuesday-weekly', 'timeout', root='c1')], False),
    ('old chain timeout ages out', [run('c1', 'tuesday-weekly', 'timeout', '11 minutes', 'max_duration', 'c1')], False),
    ('plain run with max_duration but no root', [run('r1', 'job', 'timeout', end_reason='max_duration')], False),
):
    reset()
    print('\n'.join(rows))
    fires(chain, name, expected)

spec = importlib.util.spec_from_file_location('generate_launcher_alerts', ROOT / 'scripts/generate-launcher-alerts.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)
FIXTURE = ROOT / 'tests/fixtures/job-catalog.yaml'
# lineup-optimizer is a critical job in the fixture; sync-league uses the `warning` default.
failed = {level: rule_from(generator.render(FIXTURE, level)) for level in generator.LEVELS}
CRITICAL, WARNING = failed['critical'], failed['warning']

FAILURES = (
    ('failure', None, True), ('timeout', 'orphaned', True), ('skipped', 'dependency_failed', True),
    ('skipped', 'expired', True), ('skipped', None, True),
    ('skipped', 'paused', False), ('skipped', 'chain_step', False),
    ('success', None, False), ('running', None, False), ('queued', None, False), ('cancelled', None, False),
)
for status, reason, expected in FAILURES:
    reset()
    print(run('r1', 'lineup-optimizer', status, end_reason=reason))
    fires(CRITICAL, f'critical {status}/{reason}', expected)
    fires(WARNING, f'critical job never reaches the warning rule ({status}/{reason})', False)
    reset()
    print(run('r1', 'sync-league', status, end_reason=reason))
    fires(WARNING, f'warning {status}/{reason}', expected)
    fires(CRITICAL, f'warning job never reaches the critical rule ({status}/{reason})', False)

reset()
print(run('r1', 'sync-league', 'failure', finished='11 minutes'))
fires(WARNING, 'old failure ages out', False)
reset()
print(run('r1', 'sync-league', 'failure', finished='9 minutes'))
fires(WARNING, 'failure inside the window', True)
reset()
print(run('r1', 'sync-league', 'running') + run('r2', 'sync-league', 'success'))
fires(WARNING, 'running and later success', False)
reset()
print(run('r1', 'not-in-catalog', 'failure'))
fires(WARNING, 'unknown job is ignored', False)
fires(CRITICAL, 'unknown job is ignored', False)

# The committed (empty-catalog) rules match nothing, even with failures present.
reset()
print(run('r1', 'lineup-optimizer', 'failure') + run('r2', 'sync-league', 'timeout'))
for level in generator.LEVELS:
    fires(load_rule(f'launcher-run-failed-{level}-alert.yaml'), f'empty catalog {level}', False)
print('ROLLBACK;')
