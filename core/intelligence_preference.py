"""Canonical vocabulary for the node's inference preference.

ONE definition of the values that answer "where does inference run, and does
this node join the hive relay".  Before this module the same vocabulary existed
as bare string literals in at least four places, in TWO different spellings:

  canonical  {'local_only', 'auto', 'hive_preferred'}
      Nunba llama_config._INTELLIGENCE_PREFS, the demopage toggle, the /chat
      validator, hart_intelligence_entry, speculative_dispatcher's user_pref
  legacy     {'local_only', 'hybrid', 'hive'}
      integrations/vlm/qwen3vl_backend.dispatch_inference

Because the two sets only overlap on 'local_only', canonical 'hive_preferred'
matched NO branch in the resolver that spoke the legacy set and silently fell
through to the local-first ordering -- a user who chose Hive still had local
tried first.  That is what a vocabulary defined by scattered literals costs.

``IntelligencePreference`` is a ``str`` enum on purpose: every existing
``pref == 'auto'`` comparison and every JSON/wire payload keeps working
unchanged, so adopting it is not a breaking change at any call site.

NOT handled here, deliberately: the legacy ``llm_mode`` set
{'local', 'cloud', 'hybrid'}.  Migrating that carries GRANT semantics -- an
auto-setup default 'local' must not be read as a deliberate privacy choice --
so it stays in ``LlamaConfig.resolve_intelligence_preference``, which knows
which values were a real user grant.  This module is vocabulary only.
"""
from enum import Enum


class IntelligencePreference(str, Enum):
    """The canonical three. ``str``-valued: ``AUTO == 'auto'`` is True."""

    #: never leave the box; do NOT join the hive relay
    LOCAL_ONLY = 'local_only'
    #: local-first, escalate to an expert when needed (the default)
    AUTO = 'auto'
    #: prefer the hive/hosted expert
    HIVE_PREFERRED = 'hive_preferred'

    @classmethod
    def coerce(cls, value, default=None):
        """The canonical member for a canonical OR legacy spelling.

        Accepts an existing member, a canonical value, or a legacy alias
        ('hybrid' -> AUTO, 'hive' -> HIVE_PREFERRED).  Anything unrecognised
        (None, '', a typo, a value from a newer peer) returns ``default``,
        which is AUTO unless the caller names another -- so an unknown value
        can never silently resolve to something stricter or looser than the
        node's normal posture.
        """
        if isinstance(value, cls):
            return value
        v = value.strip().lower() if isinstance(value, str) else ''
        try:
            return cls(v)
        except ValueError:
            pass
        return _LEGACY_ALIASES.get(v, default if default is not None else cls.AUTO)


#: Spellings that predated the canonical set, mapped to their equivalent.
_LEGACY_ALIASES = {
    'hybrid': IntelligencePreference.AUTO,
    'hive': IntelligencePreference.HIVE_PREFERRED,
}

#: The canonical values as plain strings, for validators and JSON schemas.
VALUES = tuple(p.value for p in IntelligencePreference)
