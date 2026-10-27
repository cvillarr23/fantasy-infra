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
    """Asserts whether any series of the rule is above its threshold (so it would fire)."""
    print(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM ({query(rule)}) q WHERE value > {threshold(rule)}) <> {str(expected).lower()} "
          f"THEN RAISE EXCEPTION 'failed: {name}'; END IF; END $$;")


def fires_for(rule, name, jobs):
    """Asserts exactly which jobs have a series above the threshold: one series per job."""
    array = ', '.join(f"'{job}'" for job in sorted(jobs))
    print(f"DO $$ BEGIN IF ARRAY(SELECT job_name FROM ({query(rule)}) q WHERE value > {threshold(rule)} ORDER BY 1) "
          f"IS DISTINCT FROM ARRAY[{array}]::text[] THEN RAISE EXCEPTION 'failed: {name}'; END IF; END $$;")


def run(run_id, job, status, finished='1 minute', end_reason=None, root=None, attempt=1):
    reason = 'NULL' if end_reason is None else f"'{end_reason}'"
    return (f"INSERT INTO run_log (id, job_name, status, end_reason, chain_root_id, attempt, finished_at) VALUES "
            f"('{run_id}', '{job}', '{status}', {reason}, {'NULL' if root is None else repr(root)}, {attempt}, "
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
assert heartbeat['labels'] == {'severity': 'critical'}
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

unknown = load_rule('launcher-queue-depth-unknown-alert.yaml')
assert unknown['for'] == '2m'
reset()
fires(unknown, 'no heartbeat row is the heartbeat alert, not this one', False)
for depths, age, expected in (('{"job_runs": 0, "job_runs.dlq": 0}', '10 seconds', False),
                              ('{"job_runs": null, "job_runs.dlq": 0}', '10 seconds', True),
                              ('{"job_runs": 0, "job_runs.dlq": null}', '10 seconds', True),
                              ('{"job_runs": 0}', '10 seconds', True),
                              ('{"job_runs": null, "job_runs.dlq": null}', '10 minutes', False)):
    reset()
    print(f"INSERT INTO launcher_heartbeat VALUES (1, now() - interval '{age}', '{depths}');")
    fires(unknown, f'depths {depths} written {age} ago', expected)

backlog = load_rule('launcher-job-runs-backlog-alert.yaml')
# `for` is Grafana's, not SQL's: the query fires on any backlog and the rule waits 15m.
assert backlog['for'] == '15m'
reset()
fires(backlog, 'no heartbeat row', False)
for depths, expected in (('{"job_runs": 0, "job_runs.dlq": 0}', False),
                         ('{"job_runs": 4, "job_runs.dlq": 0}', True),
                         ('{"job_runs": null, "job_runs.dlq": null}', False)):
    reset()
    print(f"INSERT INTO launcher_heartbeat VALUES (1, now(), '{depths}');")
    fires(backlog, f'job_runs depths {depths}', expected)

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
# In the fixture lineup-optimizer is critical with max_attempts 3; sync-league is warning with 2.
failed = {level: rule_from(generator.render(FIXTURE, level)) for level in generator.LEVELS}
CRITICAL, WARNING = failed['critical'], failed['warning']
assert CRITICAL['labels'] == {'severity': 'critical'} and WARNING['labels'] == {'severity': 'warning'}
assert generator.jobs_at_level(generator._load_catalog(FIXTURE), 'critical')[0] == ('lineup-optimizer', 3)

# Final attempts (attempt >= max_attempts) alert; skips alert unless they are expected or covered elsewhere.
FINAL = (
    ('failure', None, True), ('timeout', 'orphaned', True), ('skipped', 'dependency_failed', True),
    ('skipped', 'expired', True), ('skipped', None, True),
    ('skipped', 'paused', False), ('skipped', 'chain_step', False),
    ('skipped', 'max_duration', False), ('skipped', 'chain_ended', False),
    ('success', None, False), ('running', None, False), ('queued', None, False), ('cancelled', None, False),
)
for status, reason, expected in FINAL:
    reset()
    print(run('r1', 'lineup-optimizer', status, end_reason=reason, attempt=3))
    fires_for(CRITICAL, f'critical {status}/{reason}', ['lineup-optimizer'] if expected else [])
    fires_for(WARNING, f'critical job never reaches the warning rule ({status}/{reason})', [])
    reset()
    print(run('r1', 'sync-league', status, end_reason=reason, attempt=2))
    fires_for(WARNING, f'warning {status}/{reason}', ['sync-league'] if expected else [])
    fires_for(CRITICAL, f'warning job never reaches the critical rule ({status}/{reason})', [])

# A failure the watcher will retry is not paged; the retry's own final failure is.
for status in ('failure', 'timeout'):
    reset()
    print(run('r1', 'lineup-optimizer', status, attempt=1) + run('r2', 'lineup-optimizer', status, attempt=2))
    fires_for(CRITICAL, f'retryable {status} attempts 1 and 2 of 3', [])
    reset()
    print(run('r1', 'lineup-optimizer', status, attempt=3))
    fires_for(CRITICAL, f'last {status} attempt', ['lineup-optimizer'])
# A chain step can't be retried once its root ended, so that failure is final at any attempt.
reset()
print(run('root', 'tuesday-weekly', 'timeout', end_reason='max_duration', root='root'))
print(run('s1', 'lineup-optimizer', 'failure', attempt=1, root='root'))
fires_for(CRITICAL, 'step failure under an ended root', ['lineup-optimizer'])
reset()
print(run('root', 'tuesday-weekly', 'running', root='root'))
print(run('s1', 'lineup-optimizer', 'failure', attempt=1, root='root'))
fires_for(CRITICAL, 'retryable step failure under a running root', [])

# One series per job, so the page names the job.
reset()
print(run('r1', 'sync-league', 'failure', attempt=2) + run('r2', 'sync-league', 'timeout', attempt=2)
      + run('r3', 'projections-refresh', 'failure', attempt=2) + run('r4', 'metrics-extractor', 'success'))
fires_for(WARNING, 'each failing job is its own series', ['projections-refresh', 'sync-league'])

reset()
print(run('r1', 'sync-league', 'failure', finished='11 minutes', attempt=2))
fires_for(WARNING, 'old failure ages out', [])
reset()
print(run('r1', 'sync-league', 'failure', finished='9 minutes', attempt=2))
fires_for(WARNING, 'failure inside the window', ['sync-league'])
reset()
print(run('r1', 'sync-league', 'running') + run('r2', 'sync-league', 'success'))
fires_for(WARNING, 'running and later success', [])
reset()
print(run('r1', 'not-in-catalog', 'failure', attempt=9))
fires_for(WARNING, 'unknown job is ignored', [])
fires_for(CRITICAL, 'unknown job is ignored', [])

# The committed (empty-catalog) rules match nothing, even with failures present.
reset()
print(run('r1', 'lineup-optimizer', 'failure', attempt=9) + run('r2', 'sync-league', 'timeout', attempt=9))
for level in generator.LEVELS:
    fires(load_rule(f'launcher-run-failed-{level}-alert.yaml'), f'empty catalog {level}', False)
print('ROLLBACK;')
