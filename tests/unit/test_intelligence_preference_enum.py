"""The canonical inference-preference vocabulary, pinned.

This enum is the ONE definition shared by Nunba's llama_config, the /chat
validator and the VLM tier resolver. Two properties are load-bearing and easy
to break by accident:

  - it is a ``str`` enum, so every existing ``pref == 'auto'`` comparison and
    every JSON/wire payload keeps working. Drop ``str`` and callers silently
    stop matching.
  - ``coerce`` maps the legacy spellings and sends anything unrecognised to a
    KNOWN default, so a typo or a value from a newer peer can never resolve to
    something stricter or looser than the node's normal posture.
"""
from core.intelligence_preference import IntelligencePreference as IP, VALUES


def test_is_a_str_enum_so_existing_comparisons_hold():
    assert IP.AUTO == 'auto'
    assert IP.LOCAL_ONLY == 'local_only'
    assert IP.HIVE_PREFERRED == 'hive_preferred'
    assert isinstance(IP.AUTO, str)


def test_values_tuple_is_the_canonical_set():
    assert VALUES == ('local_only', 'auto', 'hive_preferred')


def test_canonical_spellings_coerce_to_themselves():
    for v in VALUES:
        assert IP.coerce(v) == v


def test_legacy_aliases_map_to_canonical():
    # the vocabulary integrations/vlm/qwen3vl_backend.py shipped before this
    assert IP.coerce('hive') is IP.HIVE_PREFERRED
    assert IP.coerce('hybrid') is IP.AUTO


def test_unrecognised_falls_back_to_auto_not_something_stricter():
    for junk in (None, '', '   ', 'nonsense', 'HIVE_PREFERRED_v2', 42, object()):
        assert IP.coerce(junk) is IP.AUTO


def test_caller_can_name_its_own_fallback():
    assert IP.coerce('nonsense', IP.LOCAL_ONLY) is IP.LOCAL_ONLY


def test_coerce_is_idempotent_on_members():
    assert IP.coerce(IP.HIVE_PREFERRED) is IP.HIVE_PREFERRED


def test_coerce_trims_and_casefolds():
    assert IP.coerce('  Hive  ') is IP.HIVE_PREFERRED
    assert IP.coerce('AUTO') is IP.AUTO


def test_llm_mode_vocabulary_is_deliberately_not_aliased():
    """llm_mode {'local','cloud','hybrid'} migration carries GRANT semantics.

    'local' is the auto-setup DEFAULT, not a deliberate privacy choice, so it
    must NOT coerce to LOCAL_ONLY here — that decision belongs to
    LlamaConfig.resolve_intelligence_preference, which knows which values were
    a real user grant. Only 'hybrid' is shared (and means AUTO in both).
    """
    assert IP.coerce('local') is IP.AUTO
    assert IP.coerce('cloud') is IP.AUTO
