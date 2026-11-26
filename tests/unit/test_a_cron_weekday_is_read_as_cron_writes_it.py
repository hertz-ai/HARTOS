"""A cron weekday is read the way cron writes it: 1 is Monday, 0 and 7 are Sunday.

MEASURED 2026-11-26 on APScheduler 3.11.0: CronTrigger.from_crontab hands the
weekday field to APScheduler, which counts 0 as Monday, so "0 9 * * 1" (cron:
Monday 09:00) fired on Tuesday, "0 9 * * 0" (cron: Sunday) on Monday, and
"*/2" picked Monday, Wednesday, Friday and Sunday instead of Sunday, Tuesday,
Thursday and Saturday.  Every saved timed job (reuse_recipe.create_schedule,
timed and camera) and the REUSE create_scheduled_jobs tool read their cron
that way, and the channel cron matcher compared the field with
datetime.weekday(), the same 0 = Monday.  Caught by #290 commit 1's review
test ("next on Monday"); commit 1's CREATE schedule check and next-run
words read their cron through the one reader from their first line.

Now one reader, integrations.channels.automation.cron: standard_weekdays
reads the field as cron does, and crontab_trigger hands APScheduler explicit
day names (never numbers, never a name range: APScheduler counts mon..sun,
so cron's 0-5 written as "sun-fri" would run backwards).  Everything that
reads a cron goes through it; what it cannot read is refused with a hint.

    python -m pytest tests/unit/test_a_cron_weekday_is_read_as_cron_writes_it.py -q -p no:cacheprovider --noconftest
"""
import json
from datetime import datetime

import pytest

from integrations.channels.automation.cron import (
    CronExpression, crontab_trigger, standard_weekdays)

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)   # datetime.weekday()


@pytest.mark.parametrize('field, days', [
    ('1', {MON}),
    ('0', {SUN}),
    ('7', {SUN}),
    ('1-5', {MON, TUE, WED, THU, FRI}),
    ('0-5', {SUN, MON, TUE, WED, THU, FRI}),
    ('*/2', {SUN, TUE, THU, SAT}),
    ('1-5/2', {MON, WED, FRI}),
    ('1,3,5', {MON, WED, FRI}),
    ('MON', {MON}),
    ('mon,3', {MON, WED}),
    ('mon-fri', {MON, TUE, WED, THU, FRI}),
    ('mon-sun', {MON, TUE, WED, THU, FRI, SAT, SUN}),
    ('5-7', {FRI, SAT, SUN}),
    ('*', {MON, TUE, WED, THU, FRI, SAT, SUN}),
])
def test_the_weekday_field_names_the_days_cron_means(field, days):
    assert standard_weekdays(field) == frozenset(days), field


@pytest.mark.parametrize('field', ['funday', '8', 'fri-mon', '1/0', '', 'mon,,tue'])
def test_a_weekday_cron_does_not_know_is_refused_with_what_it_takes(field):
    with pytest.raises(ValueError) as refused:
        standard_weekdays(field)
    hint = str(refused.value)
    assert field in hint or 'empty' in hint or 'backwards' in hint, hint
    assert ('0-7' in hint or 'sun=0' in hint or 'whole number' in hint), hint


@pytest.mark.parametrize('expression, names', [
    ('0 9 * * 1', 'mon'),
    ('0 9 * * 0-5', 'mon,tue,wed,thu,fri,sun'),
    ('0 9 * * */2', 'tue,thu,sat,sun'),
    ('30 7 * * MON', 'mon'),
    ('0 9 * * *', '*'),
])
def test_the_trigger_gets_explicit_day_names_never_numbers_or_a_name_range(
        expression, names):
    trigger = crontab_trigger(expression)
    shown = str(trigger)
    assert f"day_of_week='{names}'" in shown, shown


def test_monday_at_nine_fires_on_monday_from_a_sunday():
    """The real APScheduler: from Sunday 29 November 2026 at noon, cron's
    '0 9 * * 1' next fires on Monday 30 November at 09:00 (from_crontab:
    Tuesday 1 December)."""
    trigger = crontab_trigger('0 9 * * 1')
    sunday = datetime(2026, 11, 29, 12, 0, tzinfo=trigger.timezone)
    assert sunday.weekday() == SUN
    when = trigger.get_next_fire_time(None, sunday)
    assert (when.year, when.month, when.day, when.hour, when.minute) == (2026, 11, 30, 9, 0)
    assert when.weekday() == MON


def test_an_expression_without_five_fields_is_refused():
    with pytest.raises(ValueError) as refused:
        crontab_trigger('every morning')
    assert '5 fields' in str(refused.value)
    with pytest.raises(ValueError):
        crontab_trigger('61 * * * *')   # a bad minute, refused as from_crontab did


def test_the_channel_cron_matcher_reads_the_weekday_as_cron_writes_it():
    weekdays = CronExpression.parse('0 9 * * 1-5')
    assert weekdays.matches(datetime(2026, 11, 30, 9, 0))        # Monday
    assert weekdays.matches(datetime(2026, 12, 4, 9, 0))         # Friday
    assert not weekdays.matches(datetime(2026, 12, 5, 9, 0))     # Saturday
    assert not CronExpression.parse('0 9 * * 1').matches(datetime(2026, 12, 1, 9, 0))


def test_reuse_registers_a_saved_job_for_the_day_cron_means(tmp_path, monkeypatch):
    """reuse_recipe.create_schedule, the registration of a flow's saved jobs,
    timed and camera, goes through the reader."""
    import hartos.helper as helper
    import hartos.reuse_recipe as rr
    monkeypatch.setattr(helper, 'PROMPTS_DIR', str(tmp_path))
    (tmp_path / '456_0_recipe.json').write_text(json.dumps({
        'actions': [{'action_id': 1, 'action': 'Summarise the week'}],
        'scheduled_tasks': [{'cron_expression': '0 9 * * 1', 'persona': 'Parent',
                             'action_entry_point': 1, 'action_exit_point': 1,
                             'job_description': 'Weekly review'}],
        'visual_scheduled_tasks': [{'cron_expression': '0 18 * * 0', 'persona': 'Parent',
                                    'job_description': 'Look at the desk'}]}),
        encoding='utf-8')
    added = []

    class _Scheduler:
        def add_job(self, func, trigger=None, id=None, args=None, **kw):
            added.append((id, str(trigger)))
    monkeypatch.setattr(rr, 'scheduler', _Scheduler())
    monkeypatch.setattr(rr, 'get_flow_number', lambda uid, pid: (0, 'Parent'))
    monkeypatch.setattr(rr, 'ACTION_API', '')
    monkeypatch.setitem(rr.recipes, '123_456', None)   # create_schedule writes it

    rr.create_schedule(456, 123)

    jobs = dict(added)
    assert "day_of_week='mon'" in jobs['sched_123_456_0'], jobs
    assert "day_of_week='sun'" in jobs['visual_task_123_456_0'], jobs
