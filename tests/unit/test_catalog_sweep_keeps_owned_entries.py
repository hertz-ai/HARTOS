"""A populate must not delete the models it is populating.

MEASURED 2026-09-21 against the owner's live catalogue (40 entries, last
written 2026-08-16): ``populate_from_subsystems()`` added 18 entries and
DELETED 9 -- tts-chatterbox-ml, tts-chatterbox-turbo, tts-cosyvoice3,
tts-espeak, tts-f5-tts, tts-indic-parler, tts-piper, tts-pocket-tts and
vlm-minicpm-v2.  Every one of them is still owned by a populator that ran
in that same call.

The sweep at the end of populate_from_subsystems decides what is stale:

    ids_before = set(self._entries)      # before the populators
    ...
    ids_after  = set(self._entries)      # after
    touched_this_boot = ids_after - ids_before
    # any AUTO_PREFIX entry not in touched_this_boot is popped

``ids_after - ids_before`` is the set of entries that are NEW.  An entry
that already existed is never in it, whichever way its populator handled
it:

  * ``register()`` overwrites in place, so re-registering an existing id
    changes nothing about the key set -- vlm-minicpm-v2 went this way;
  * the TTS and STT populators deliberately SKIP an id they have already
    emitted, to preserve user edits made in the admin UI
    (tts_router.py:2123, whisper_tool.py:871) -- the eight tts-* entries
    went this way.

So the two contracts are exactly opposed: the populator says "already
present, do not touch", the sweep says "not touched, delete".  The code's
own comment says "check timestamps on _entries that weren't touched" and
no timestamp is ever checked.

This has never bitten a user only because a SECOND bug hides it:
``get_catalog()`` runs the populators solely ``if not list_all()``, so a
node that has ever written a catalogue never populates again.  Removing
that guard alone -- the obvious way to let a node learn about a newly
shipped model -- would strip models from every existing install.  The two
have to be fixed together, and this file guards the half that makes the
other half safe.
"""
import pytest

from integrations.service_tools.model_catalog import ModelCatalog, ModelEntry


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """A catalogue with the built-in populators silenced, so a test says
    something about the sweep rather than about what ships today."""
    cat = ModelCatalog(catalog_path=str(tmp_path / 'model_catalog.json'))
    for name in ('_populate_llm_models', '_populate_tts_models',
                 '_populate_stt_models', '_populate_vlm_models',
                 '_populate_embodied_models', '_populate_videogen_models',
                 '_populate_audiogen_models'):
        monkeypatch.setattr(cat, name, lambda: 0)
    return cat


def _entry(model_id, model_type='tts', **kw):
    return ModelEntry(id=model_id, name=model_id, model_type=model_type, **kw)


class TestAnEntryItsPopulatorStillOwnsSurvives:

    def test_reregistered_entry_survives(self, catalog):
        """register() overwrites in place, so the id is not 'new' -- which
        is how vlm-minicpm-v2 was deleted by the populate that emitted it."""
        catalog.register(_entry('tts-kept', model_type='tts'), persist=False)

        def populator(cat):
            cat.register(_entry('tts-kept', model_type='tts'), persist=False)
            return 0

        catalog.register_populator('owner', populator)
        catalog.populate_from_subsystems()
        assert catalog.get('tts-kept') is not None

    def test_entry_claimed_but_deliberately_not_reregistered_survives(self, catalog):
        """The TTS and STT populators skip an id they have already emitted,
        to preserve the owner's admin-UI edits.  Skipping must not read as
        abandoning."""
        catalog.register(_entry('tts-skipped'), persist=False)
        seen = []

        def populator(cat):
            if cat.already_registered('tts-skipped'):
                seen.append('skipped')
                return 0
            cat.register(_entry('tts-skipped'), persist=False)
            return 1

        catalog.register_populator('owner', populator)
        catalog.populate_from_subsystems()
        assert seen == ['skipped'], 'the populator must have taken the skip path'
        assert catalog.get('tts-skipped') is not None

    def test_a_new_entry_still_arrives(self, catalog):
        def populator(cat):
            cat.register(_entry('tts-fresh'), persist=False)
            return 1

        catalog.register_populator('owner', populator)
        assert catalog.populate_from_subsystems() == 1
        assert catalog.get('tts-fresh') is not None


class TestTheSweepStillRemovesWhatNobodyOwns:
    """The sweep exists for a real case -- an engine deleted from the code
    should not linger in the catalogue forever.  Keeping owned entries must
    not cost that."""

    def test_entry_no_populator_claims_is_removed(self, catalog):
        catalog.register(_entry('tts-abandoned'), persist=False)
        catalog.register_populator('owner', lambda cat: 0)
        catalog.populate_from_subsystems()
        assert catalog.get('tts-abandoned') is None

    def test_a_user_customised_entry_is_kept_even_unclaimed(self, catalog):
        """Pre-existing protection: pinned / purposed entries are the
        owner's, not the populator's."""
        catalog.register(_entry('tts-mine', pinned=True), persist=False)
        catalog.register_populator('owner', lambda cat: 0)
        catalog.populate_from_subsystems()
        assert catalog.get('tts-mine') is not None

    def test_a_non_auto_prefix_entry_is_never_swept(self, catalog):
        catalog.register(_entry('custom-thing', model_type='llm'), persist=False)
        catalog.register_populator('owner', lambda cat: 0)
        catalog.populate_from_subsystems()
        assert catalog.get('custom-thing') is not None


class TestClaimsDoNotLeakBetweenRuns:

    def test_a_claim_from_a_previous_run_does_not_save_a_later_orphan(self, catalog):
        """If claims outlived the run that made them, an entry abandoned
        later would be protected by a stale claim forever."""
        catalog.register(_entry('tts-transient'), persist=False)
        claims = {'on': True}

        def populator(cat):
            if claims['on']:
                cat.already_registered('tts-transient')
            return 0

        catalog.register_populator('owner', populator)
        catalog.populate_from_subsystems()
        assert catalog.get('tts-transient') is not None

        claims['on'] = False
        catalog.populate_from_subsystems()
        assert catalog.get('tts-transient') is None

    def test_already_registered_outside_a_populate_is_just_a_question(self, catalog):
        """Asked outside a run it must answer without recording anything,
        so a caller elsewhere cannot accidentally protect an entry."""
        catalog.register(_entry('tts-asked'), persist=False)
        assert catalog.already_registered('tts-asked') is True
        assert catalog.already_registered('tts-absent') is False
        catalog.register_populator('owner', lambda cat: 0)
        catalog.populate_from_subsystems()
        assert catalog.get('tts-asked') is None


class TestTheRealPopulatorsClaimWhatTheySkip:
    """The two populators whose skip idiom the sweep contradicted must use
    the claiming question, or the fix does not reach the entries it was
    written for."""

    @pytest.fixture(autouse=True)
    def _restore_engine_registry(self):
        """populate_tts_catalog overlays ENGINE_REGISTRY from the catalogue
        entries it finds (the #58 snapshot semantics), so driving it with
        stand-in entries mutates a module global that later tests read.
        Measured: it rewrote chatterbox_turbo, cosyvoice3 and espeak, and
        test_tts_router's clone-ladder case then failed -- in THIS file's
        run order only, which is exactly how such a leak hides."""
        from integrations.channels.media import tts_router
        saved = dict(tts_router.ENGINE_REGISTRY)
        yield
        tts_router.ENGINE_REGISTRY.clear()
        tts_router.ENGINE_REGISTRY.update(saved)

    def test_tts_populator_keeps_engines_already_in_the_catalogue(self, catalog):
        from integrations.channels.media.tts_router import (
            populate_tts_catalog, ENGINE_REGISTRY, _engine_id_to_catalog_id,
        )
        present = [_engine_id_to_catalog_id(e) for e in list(ENGINE_REGISTRY)[:3]]
        for cid in present:
            catalog.register(_entry(cid), persist=False)
        catalog.register_populator('tts', populate_tts_catalog)
        catalog.populate_from_subsystems()
        for cid in present:
            assert catalog.get(cid) is not None, (
                f'{cid} was in the catalogue and is still in ENGINE_REGISTRY')
