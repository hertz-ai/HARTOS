"""An installed Hevolve_Database too old for this HARTOS is named, at boot
and on every consent call, never a TypeError (review rv_2c18 of 2c18dc713).

HARTOS reads UserConsent.paired_user_id (#306, v60) and passes it to every
consent row it writes.  The desktop imports the canonical model from
Hevolve_Database (integrations.social.models), and a pip install of HARTOS
pinned a branch last touched 2026-04-25, so an install could carry a model
without the column: then every consent ask of every type failed with
"TypeError: 'paired_user_id' is an invalid keyword argument".  Owner rule:
a refusal says what was wrong.  So the schema check at boot (run_migrations)
names the mismatch, and every consent call refuses with the same words.
"""
import logging
import os
import sys

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from integrations.social import consent_service as cs  # noqa: E402
from integrations.social import migrations  # noqa: E402
from integrations.social.consent_service import ConsentService  # noqa: E402

WORDS = ("the installed Hevolve_Database has no user_consents.paired_user_id; "
         "consent is off until it is updated")


class _ModelBefore08a2e22:
    """UserConsent as Hevolve_Database had it before 08a2e22: every column
    HARTOS reads but paired_user_id."""
    id = user_id = agent_id = consent_type = scope = granted = None
    granted_at = revoked_at = reopened_at = label = None


def test_the_model_this_tree_runs_on_has_the_column():
    assert cs.consent_schema_refusal() is None


def test_a_model_without_the_column_is_named():
    assert cs.consent_schema_refusal(_ModelBefore08a2e22) == WORDS


@pytest.mark.parametrize('call', [
    lambda: ConsentService.request_consent(None, 'owner-1', 'camera_capture'),
    lambda: ConsentService.grant_consent(None, 'owner-1', 'computer_control'),
    lambda: ConsentService.check_consent(None, 'owner-1', 'credential',
                                         scope='secret:MAILBOX'),
    lambda: ConsentService.active_grant(None, 'owner-1', 'device_access',
                                        scope='device:' + 'ab' * 32),
], ids=['ask', 'grant', 'check', 'device'])
def test_every_consent_call_says_what_is_wrong(monkeypatch, call):
    monkeypatch.setattr(cs, 'UserConsent', _ModelBefore08a2e22)
    with pytest.raises(cs.ConsentSchemaStale) as refused:
        call()
    assert str(refused.value) == WORDS


def test_the_boot_schema_check_names_it(monkeypatch, tmp_path, caplog):
    engine = create_engine(
        'sqlite:///' + str(tmp_path / 'hevolve_database.db').replace('\\', '/'))
    monkeypatch.setattr(migrations, 'get_engine', lambda: engine)
    monkeypatch.setattr(cs, 'UserConsent', _ModelBefore08a2e22)
    with caplog.at_level(logging.CRITICAL, logger='hevolve_social'):
        migrations.run_migrations()
    engine.dispose()
    assert [r.getMessage() for r in caplog.records
            if r.levelno == logging.CRITICAL and WORDS in r.getMessage()]


def test_the_boot_schema_check_is_quiet_on_a_model_with_the_column(
        monkeypatch, tmp_path, caplog):
    engine = create_engine(
        'sqlite:///' + str(tmp_path / 'hevolve_database.db').replace('\\', '/'))
    monkeypatch.setattr(migrations, 'get_engine', lambda: engine)
    with caplog.at_level(logging.CRITICAL, logger='hevolve_social'):
        migrations.run_migrations()
    engine.dispose()
    assert not [r for r in caplog.records if WORDS in r.getMessage()]


@pytest.mark.parametrize('manifest', ['setup.py', 'pyproject.toml'])
def test_a_pip_install_of_hartos_pins_hevolve_database_main(manifest):
    """The pin was realistic_intro_video, a branch last touched 2026-04-25
    and an ancestor of main: an April model with no paired_user_id.  The
    Nunba build checks out SIBLING_REF=main; a pip install now gets the
    same."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, manifest), encoding='utf-8') as f:
        text = f.read()
    pins = [line.strip() for line in text.splitlines()
            if 'hevolve-database @' in line]
    assert pins, manifest
    assert all(p.rstrip('",').endswith('Hevolve_Database.git@main') for p in pins), pins
