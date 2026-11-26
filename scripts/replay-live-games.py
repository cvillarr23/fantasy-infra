#!/usr/bin/env python3
"""Replay a season of `live-games` through the launcher image's own scheduler and exit rule.

Core's Scheduler reads the catalog and a committed nflverse schedule on a frozen clock, and each
run it starts lasts until core's poller would exit. Fails when a kickoff doesn't get exactly one
start in the 30 minutes before it, a day without games gets one, an unknown kickoff time isn't
covered every 30 minutes, a game isn't watched from kickoff, or a run reaches the job's deadline.

The code under test is the `src` package in the core image, so run it there:
    docker run --rm --network none -v "$PWD:/infra:ro" -w /app -e PYTHONPATH=/app "$LAUNCHER_IMAGE" \\
        python /infra/scripts/replay-live-games.py /infra/charts/fantasy/files/catalog.yaml \\
        /infra/tests/fixtures/nfl-schedule-2026.csv

Rewrite the fixture from nflverse (needs the network):
    docker run --rm "$LAUNCHER_IMAGE" python -c 'import sys, nflreadpy, polars as pl
    games = nflreadpy.load_schedules(2026).filter(pl.col("game_type") == "REG")
    columns = games.select("season", "week", "gameday", "gametime", "game_type")
    sys.stdout.write(columns.sort("gameday", "gametime").write_csv())' > tests/fixtures/nfl-schedule-2026.csv
"""

import logging
import sys
import types
from datetime import date, datetime, time, timedelta, timezone

import polars as pl

JOB = "live-games"
COLUMNS = {"season": pl.Int32, "week": pl.Int32, "gameday": pl.Utf8, "gametime": pl.Utf8, "game_type": pl.Utf8}
# A quick game, and one with a long overtime or delay: short games let a run exit between
# kickoffs, long ones keep it up toward its deadline.
GAME_LENGTHS = (timedelta(hours=2, minutes=45), timedelta(hours=4))
# Days ticked either side of the season, where nothing may start.
MARGIN = timedelta(days=7)
QUIET_STEP = timedelta(minutes=5)
BUSY_STEP = timedelta(minutes=1)
SHOWN_FAILURES = 20


def _stub_database():
    """`src.database` opens a Postgres pool on import. The replay touches no database or Redis,
    so every name imported from it fails when called."""
    stub = types.ModuleType("src.database")
    stub.__getattr__ = _database_name
    sys.modules["src.database"] = stub


def _database_name(name):
    """Any name, so a core module importing another helper doesn't break the replay."""
    if name.startswith("__"):
        raise AttributeError(name)
    return _no_database


def _no_database(*args, **kwargs):
    raise RuntimeError("the replay has no database")


_stub_database()

from src.ingestion import live_games  # noqa: E402
from src.launcher.catalog import load_catalog  # noqa: E402
from src.launcher.scheduler import NflSeasonCalendar, Scheduler, next_slot  # noqa: E402
from src.nfl_schedule import SCHEDULE_TIMEZONE, kickoff_utc  # noqa: E402

# How long before a kickoff the poller stays up for it, so a start any earlier is wasted.
LOOKAHEAD = live_games.KICKOFF_LOOKAHEAD


class MemoryState:
    """What a tick reads and writes for a catalog holding one job: the slots already used.
    Nothing is paused, and there are no chains, fan-outs or approvals."""

    def __init__(self):
        self.starts = {}

    def publish(self, catalog, job, *, dedupe_key, scheduled_for, **_):
        self.starts[dedupe_key] = scheduled_for
        return {"run_id": dedupe_key, "status": "queued"}

    def run_exists(self, dedupe_key):
        return dedupe_key in self.starts

    def paused(self, name, now):
        return False

    def active_chain_roots(self):
        return []

    def active_fan_outs(self):
        return []

    def expire_approvals(self, now):
        return []

    def approved_without_then(self, since):
        return []


def live_games_catalog(path):
    """The catalog cut down to `live-games`, so no other job's slots are ticked."""
    catalog = load_catalog(path)
    catalog.jobs = {JOB: catalog.jobs[JOB]}
    catalog.chains = {}
    return catalog


def scheduled_starts(catalog, schedule, ticks):
    """Every start core's scheduler publishes for `schedule` (a loader of one season's games)
    when ticked at each of `ticks`."""
    state = MemoryState()
    now = ticks[0]

    def clock():
        return now

    calendar = NflSeasonCalendar(load=schedule, clock=clock)
    scheduler = Scheduler(catalog, state, state.publish, seasons=calendar, clock=clock)
    for now in ticks:
        scheduler.tick(now)
    return sorted(state.starts.values())


def season_of(games):
    """A loader like nflverse's: the games of the asked season, none for an unpublished one."""
    return lambda year: games.filter(pl.col("season") == year)


def unreadable(year):
    raise RuntimeError("nflverse is unreachable")


def kickoffs_of(games):
    return sorted({kickoff_utc(g["gameday"], g["gametime"]) for g in games.iter_rows(named=True)})


def game_days(games):
    return {date.fromisoformat(day) for day in games["gameday"]}


def eastern_day(moment):
    return moment.astimezone(SCHEDULE_TIMEZONE).date()


def midnight(day):
    """The start of an Eastern day, in UTC."""
    return datetime.combine(day, time(), SCHEDULE_TIMEZONE).astimezone(timezone.utc)


def every(step, start, end):
    return [start + i * step for i in range((end - start) // step)]


def after_cron_slots(schedules, start, end):
    """A tick just after each cron slot. One with no `catch_up` fires only within 2 minutes of
    its time, which the quiet steps would miss."""
    for schedule in schedules:
        slot = next_slot(schedule, start)
        while slot < end:
            yield slot + BUSY_STEP
            slot = next_slot(schedule, slot)


def season_ticks(schedules, kickoffs, days):
    """Every 5 minutes through the season, every minute in the hour before each kickoff, and
    after each cron slot. Ticking every minute throughout takes far longer and finds nothing
    more."""
    start, end = midnight(min(days) - MARGIN), midnight(max(days) + MARGIN)
    ticks = set(every(QUIET_STEP, start, end))
    for kickoff in kickoffs:
        ticks.update(every(BUSY_STEP, kickoff - timedelta(hours=1), kickoff))
    ticks.update(after_cron_slots(schedules, start, end))
    return sorted(ticks)


def _just_before(start, kickoff):
    return kickoff - LOOKAHEAD <= start < kickoff


def kickoff_start_failures(starts, kickoffs):
    for kickoff in kickoffs:
        count = sum(_just_before(start, kickoff) for start in starts)
        if count != 1:
            yield f"kickoff {kickoff:%F %R}Z: {count} starts in the {LOOKAHEAD} before it, expected 1"


def idle_day_failures(starts, days):
    for day in sorted({eastern_day(start) for start in starts} - days):
        yield f"{day} has no game but gets a start"


def uncovered_stretches(starts, day):
    """The stretches of an Eastern day longer than the lookahead with no start: a game at the end
    of one would have none in time."""
    first, last = midnight(day), midnight(day + timedelta(days=1))
    marks = [first, *(s for s in starts if first <= s < last), last]
    return [(a, b) for a, b in zip(marks, marks[1:]) if b - a > LOOKAHEAD]


def coverage_failures(starts, day, why):
    for a, b in uncovered_stretches(starts, day):
        yield f"{why}: no start between {a:%F %R}Z and {b:%F %R}Z"


def unreadable_schedule_failures(catalog, day):
    ticks = every(BUSY_STEP, midnight(day), midnight(day + timedelta(days=1)))
    starts = scheduled_starts(catalog, unreadable, ticks)
    yield from coverage_failures(starts, day, "unreadable schedule")


def _without_a_time(games, day):
    """The games with the first one on `day` given no kickoff time."""
    first = games["gameday"].to_list().index(day.isoformat())
    times = games["gametime"].to_list()
    times[first] = None
    return games.with_columns(pl.Series("gametime", times, dtype=pl.Utf8))


def unset_time_failures(catalog, games, day):
    """That day is covered throughout; the days around it start only for their timed kickoffs."""
    untimed = _without_a_time(games, day)
    ticks = every(BUSY_STEP, midnight(day - timedelta(days=1)), midnight(day + timedelta(days=2)))
    starts = scheduled_starts(catalog, season_of(untimed), ticks)
    yield from coverage_failures(starts, day, "unset kickoff time")
    kickoffs = kickoffs_of(untimed.drop_nulls("gametime"))
    for start in starts:
        if eastern_day(start) != day and not any(_just_before(start, k) for k in kickoffs):
            yield f"unset kickoff time on {day} also starts a run at {start:%F %R}Z, with no kickoff due"


def _game_at(kickoff, length, now):
    """A game as the poller's exit rule reads it."""
    if now < kickoff:
        status = live_games.NOT_STARTED
    elif now < kickoff + length:
        status = live_games.IN_PROGRESS
    else:
        status = live_games.FINAL
    return {"status": status, "kickoff": kickoff.isoformat()}


def run_end(start, kickoffs, length, deadline):
    """When a run started at `start` exits, ticking at the poller's slow pace. It stops counting
    at the deadline."""
    now = start
    while now - start < deadline:
        if live_games.should_exit([_game_at(k, length, now) for k in kickoffs], now):
            break
        now += timedelta(seconds=live_games.SLOW_TICK_SECONDS)
    return now


def poller_runs(starts, kickoffs, length, deadline):
    """(start, end) of each run. A start that finds a run still going is skipped, as the
    catalog's overlap rule does."""
    runs = []
    for start in starts:
        if runs and start < runs[-1][1]:
            continue
        runs.append((start, run_end(start, kickoffs, length, deadline)))
    return runs


def run_failures(starts, tracked, length, deadline):
    """Failures for one set of tracked games: the poller watches only those with a matchup
    player, so any game may be the only one."""
    runs = poller_runs(starts, tracked, length, deadline)
    for kickoff in tracked:
        if not any(start <= kickoff < end for start, end in runs):
            yield f"kickoff {kickoff:%F %R}Z not watched ({len(tracked)} tracked, games last {length})"
    for start, end in runs:
        if end - start >= deadline:
            yield f"run from {start:%F %R}Z reaches its {deadline} deadline (games last {length})"


def poller_failures(starts, kickoffs, deadline):
    """Each game tracked alone, then all of them together."""
    for length in GAME_LENGTHS:
        for tracked in [*([k] for k in kickoffs), kickoffs]:
            yield from run_failures(starts, tracked, length, deadline)


def replay(catalog, games):
    """Every failure, across the season replay, both fallbacks and the poller."""
    kickoffs = kickoffs_of(games.drop_nulls("gametime"))
    days = game_days(games)
    ticks = season_ticks(catalog.jobs[JOB].schedules, kickoffs, days)
    starts = scheduled_starts(catalog, season_of(games), ticks)
    print(f"Replayed {len(games)} games, {len(kickoffs)} kickoff times and {len(starts)} starts.")
    sample = sorted(days)[len(days) // 2]
    yield from kickoff_start_failures(starts, kickoffs)
    yield from idle_day_failures(starts, days)
    yield from unreadable_schedule_failures(catalog, sample)
    yield from unset_time_failures(catalog, games, sample)
    yield from poller_failures(starts, kickoffs, catalog.resolve(JOB).deadline)


def _report(failures):
    for failure in failures[:SHOWN_FAILURES]:
        print(f"FAIL {failure}")
    if len(failures) > SHOWN_FAILURES:
        print(f"... and {len(failures) - SHOWN_FAILURES} more")


def main():
    catalog_path, fixture_path = sys.argv[1:]
    # The scheduler logs a failed slot and carries on: show those errors, not a line per start.
    logging.disable(logging.WARNING)
    games = pl.read_csv(fixture_path, schema=COLUMNS)
    failures = list(replay(live_games_catalog(catalog_path), games))
    _report(failures)
    print(f"{JOB} replay: {len(failures)} failure(s)." if failures else f"{JOB} replay passed.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
