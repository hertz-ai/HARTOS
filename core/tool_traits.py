"""Traits a tool declares where it is registered.

A trait is a plain attribute on the tool's function object, set by a decorator
where the builder registers the tool and read by the code that handles the
tool's results. autogen registers a function through functools.wraps, which
copies the attribute, and keeps the original on ``_origin``; has_trait checks
both. An alias registered under a second name is the same function, so it
carries the trait too.

Core owns the traits, so core never imports integrations to declare one (#104
review); integrations read them from here.
"""

#: The tool's result is a read of state HARTOS already persists (MemoryGraph,
#: SimpleMem, agent_data, the chat history). The group chat's write-back does
#: not store such a result again: the store already holds it, and writing a
#: read back made the next read return more (#104, central 2026-09-14).
READS_PERSISTED_STATE = 'reads_persisted_state'


def reads_persisted_state(func):
    """Mark ``func`` as a tool whose result is a read of persisted state."""
    setattr(func, READS_PERSISTED_STATE, True)
    return func


def has_trait(func, trait: str) -> bool:
    """Whether ``func``, or the original autogen wrapped, carries ``trait``."""
    if func is None:
        return False
    return bool(getattr(func, trait, False)
                or getattr(getattr(func, '_origin', None), trait, False))
