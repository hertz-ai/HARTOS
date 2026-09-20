"""
capability_setup.py — setting up a capability the moment a task needs it.

A turn asks for something this node cannot do yet: a voiced reply wants a
cloning engine and none is installed.  The answer is neither to fail
silently nor to install behind the owner's back, but to offer the setup, on
that task's demand, and do the work only once the owner says yes.

Every piece of that already exists; this module is the wire between them and
owns no path of its own:

  * the ASK -- ``ConsentService.check_or_request`` files ONE pending consent
    row and emits ``consent.request``.  AgentOverlay and the floating
    companion render that as a card, and the ``reason`` passed here is what
    the card says, so the offer explains itself in the owner's words.  Their
    Allow writes a grant for the ask's exact scope, which the next call
    reads.  Re-asking is free: request_consent dedupes to one card and stops
    once the answer is on file either way.
  * the WORK -- ``core.error_advice.handle_exception(agent_remediation=True)``
    raises the self_heal goal the agent daemon paces, and
    ``integrations.coding_agent.backend_repair_tools.repair_backend_venv`` is
    the tool that goal's prompt routes to for a TTS backend: it wraps Nunba's
    ``install_backend_full``, the same function the "Set up TTS" UI calls.
    error_advice throttles per failure shape in memory and skips a
    fingerprint that already has an active goal, so offering on every turn
    still provisions exactly once.
  * the SCOPE -- one capability per grant (``tts:f5_tts``), because a yes to
    a 2.5 GB voice is not a yes to a 12 GB one.

Nothing here installs anything itself, nothing here waits, and nothing here
can fail the turn that triggered it: that reply has already gone out in the
default voice.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

#: The consent an owner gives once per capability (consent_service
#: CONSENT_TYPES).  Named here so the ask and any future reader agree.
SETUP_CONSENT_TYPE = 'capability_setup'

#: A consent scope is 100 chars (UserConsent.scope); a capability id is a
#: short '<family>:<id>' and anything longer is a caller's bug, not a row.
_MAX_CAPABILITY = 100


class CapabilityNotInstalled(RuntimeError):
    """A capability a task needed is not installed on this node.

    Raised nowhere and caught nowhere: it is built to carry the fact into
    ``error_advice``, whose throttle fingerprints on the exception, so the
    message stays stable per capability and one offer per capability
    survives a turn that repeats.
    """


def request_capability_setup(capability: str, *, reason: str, category: str,
                             context: Optional[dict] = None,
                             severity: str = 'low') -> str:
    """Offer to set ``capability`` up, and raise the work once the owner has
    said yes.

    Args:
        capability: what is being set up, as ``'<family>:<id>'``
            (``'tts:f5_tts'``).  It becomes the consent scope, so the grant
            covers this capability and no other.
        reason: what the card says.  Written for the person reading it.
        category: the error_advice category the remediation goal carries;
            ``repair_backend_venv`` documents the TTS ones it answers to
            (``tts.probe`` / ``tts.install`` / ``subprocess.tool_load``).
        context: extra fields for the goal.  ``backend`` is what the TTS
            repair tool reads.
        severity: error_advice severity, driving the goal's spark budget.
            Missing a capability is not an incident; 'low' by default.

    Returns what happened, for the caller's log:
        ``'provisioning'`` consent is on file and the work was raised (or
                           was already raised -- error_advice dedupes).
        ``'asked'``        the card is filed and waiting for an answer.
        ``'declined'``     the owner said no.  Nothing was asked again.
        ``'unavailable'``  nobody could be asked, or consent could not be
                           reached at all.
    """
    if not isinstance(capability, str) or not capability.strip():
        logger.warning("capability setup: a capability must be named")
        return 'unavailable'
    capability = capability.strip()
    if len(capability) > _MAX_CAPABILITY:
        logger.warning("capability setup: %r is too long to scope a consent",
                       capability[:40])
        return 'unavailable'

    # Whose machine this is.  The disk, the bandwidth and the install are the
    # desktop owner's, exactly as for computer_control (vlm.safety) and the
    # screen capture loop, which read the same variable Nunba exports at
    # boot.  With no owner there is nobody to ask, so nothing is offered.
    owner = os.environ.get('HEVOLVE_OWNER_USER_ID')
    if not owner:
        logger.info(
            "capability setup for %s not offered: this node has no owner to "
            "ask (HEVOLVE_OWNER_USER_ID is not set)", capability)
        return 'unavailable'

    try:
        from integrations.social.models import db_session
        from integrations.social.consent_service import ConsentService
        with db_session(commit=True) as db:
            if ConsentService.check_or_request(
                    db, owner, SETUP_CONSENT_TYPE, scope=capability,
                    reason=reason):
                answer = 'granted'
            elif ConsentService.declined(db, owner, SETUP_CONSENT_TYPE,
                                         scope=capability):
                answer = 'declined'
            else:
                answer = 'asked'
    except Exception as e:
        logger.warning("capability setup for %s could not be offered: %s",
                       capability, e)
        return 'unavailable'

    if answer != 'granted':
        logger.info("capability setup for %s: %s", capability, answer)
        return answer

    try:
        from core.error_advice import handle_exception
        handle_exception(
            CapabilityNotInstalled(f"{capability} is not installed"),
            category=category, severity=severity, agent_remediation=True,
            context={'capability': capability, **(context or {})},
        )
    except Exception as e:
        logger.warning("capability setup for %s: the owner allowed it but "
                       "the work could not be raised: %s", capability, e)
        return 'unavailable'
    logger.info("capability setup for %s: allowed, provisioning raised",
                capability)
    return 'provisioning'


def _engine_label(engine_id: str) -> str:
    """The engine as a person would name it: Nunba's own display name when
    that install path is importable (it is on a desktop), else the id."""
    try:
        from tts.package_installer import BACKEND_DISPLAY_NAMES  # type: ignore
        return BACKEND_DISPLAY_NAMES.get(engine_id) or engine_id
    except Exception:
        return engine_id


def offer_voice_clone_setup(language: Optional[str] = None) -> str:
    """A voiced turn ended in the default voice: offer to set up the best
    cloning engine this machine can actually run.

    Returns ``request_capability_setup``'s outcome, or ``'unavailable'``
    when there is nothing to offer -- no cloner is missing (the turn failed
    for some other reason, and an offer would be a lie), or none of the
    missing ones fits this card.
    """
    try:
        from integrations.channels.media.tts_router import (
            clone_engines_not_installed,
        )
        candidates = clone_engines_not_installed(language)
    except Exception as e:
        logger.debug("voice-clone setup offer skipped: %s", e)
        return 'unavailable'
    if not candidates:
        return 'unavailable'

    engine_id = candidates[0]
    return request_capability_setup(
        f'tts:{engine_id}',
        reason=(
            f"Speaking in a recorded voice needs a cloning engine, and none "
            f"is installed on this computer. {_engine_label(engine_id)} is "
            f"the one that fits this machine. Set it up? It downloads and "
            f"installs here; until then replies use the default voice."
        ),
        category='tts.probe',
        context={'backend': engine_id,
                 'language': (language or 'en')[:2].lower()},
    )
