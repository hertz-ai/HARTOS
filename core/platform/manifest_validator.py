"""
Manifest Validator — OS-level contracts for AppManifest integrity.

Every AppManifest registered in HART OS must pass validation. This prevents
Frankenstein extensions from polluting the OS with invalid types, dangerous
entries, or arbitrary permissions.

Follows the budget_gate.py pattern: static methods, fail-closed, clear reasons.

Usage:
    from core.platform.manifest_validator import ManifestValidator
    valid, errors = ManifestValidator.validate(manifest)
    if not valid:
        raise ValueError(f"Invalid manifest: {'; '.join(errors)}")
"""

import math
import re
from typing import Any, Dict, List, Tuple

from core.platform.app_manifest import AppType

# ─── Constants (frozen by convention — do not modify) ──────────────

# Valid permissions an app can declare
KNOWN_PERMISSIONS = frozenset({
    'network', 'audio', 'display', 'input', 'filesystem',
    'system_read', 'system_write', 'camera', 'microphone',
    'clipboard', 'notifications', 'bluetooth', 'usb',
    'location', 'process_manage', 'power_manage',
})

# Valid AppType values (derived from enum)
_VALID_TYPES = frozenset(t.value for t in AppType)

# Valid AI capability types
_VALID_AI_CAPABILITY_TYPES = frozenset({
    'llm', 'vision', 'tts', 'stt', 'image_gen', 'embedding', 'code',
})

# Valid model policies
_VALID_MODEL_POLICIES = frozenset({
    'local_only', 'local_preferred', 'any',
})

# Required entry keys per AppType
ENTRY_SCHEMA: Dict[str, Dict[str, Any]] = {
    'nunba_panel':   {'required': ['route']},
    'system_panel':  {'required': ['loader']},
    'dynamic_panel': {'required': ['route']},
    'desktop_app':   {'required': ['exec']},
    'service':       {'any_of': ['http', 'exec']},
    'agent':         {'required': ['prompt_id', 'flow_id']},
    'mcp_server':    {'required': ['mcp']},
    'channel':       {'required': ['adapter']},
    'extension':     {'required': ['module']},
}

# ID format: alphanumeric, hyphens, underscores, 1-64 chars
_ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')

# Version: semver X.Y.Z or 'auto'
_SEMVER_RE = re.compile(r'^\d+\.\d+\.\d+$')


# ─── Validator ─────────────────────────────────────────────────────

class ManifestValidator:
    """Validates AppManifest fields against OS contracts.

    All methods are static — no state needed.
    Returns (valid, errors) tuples following budget_gate.py pattern.
    """

    @staticmethod
    def validate(manifest) -> Tuple[bool, List[str]]:
        """Full validation of an AppManifest.

        Returns (is_valid, [error_messages]).
        """
        errors = []

        # ID
        ok, msg = ManifestValidator.validate_id(manifest.id)
        if not ok:
            errors.append(msg)

        # Type
        ok, msg = ManifestValidator.validate_type(manifest.type)
        if not ok:
            errors.append(msg)

        # Version
        ok, msg = ManifestValidator.validate_version(manifest.version)
        if not ok:
            errors.append(msg)

        # Entry (only if type is valid)
        if manifest.type in _VALID_TYPES:
            ok, msg = ManifestValidator.validate_entry(
                manifest.type, manifest.entry)
            if not ok:
                errors.append(msg)

        # Permissions
        ok, msg = ManifestValidator.validate_permissions(manifest.permissions)
        if not ok:
            errors.append(msg)

        # AI Capabilities
        ok, cap_errors = ManifestValidator.validate_ai_capabilities(
            manifest.ai_capabilities)
        if not ok:
            errors.extend(cap_errors)

        # Size
        ok, msg = ManifestValidator.validate_size(manifest.default_size)
        if not ok:
            errors.append(msg)

        valid = len(errors) == 0

        # Emit event + audit log on failure (non-blocking, best-effort)
        if not valid:
            try:
                from core.platform.events import emit_event
                emit_event('manifest.validation_failed', {
                    'app_id': manifest.id,
                    'errors': errors,
                })
            except Exception:
                pass
            try:
                from security.immutable_audit_log import get_audit_log
                get_audit_log().log_event(
                    'security', 'manifest_validator',
                    f"Rejected manifest '{manifest.id}'",
                    detail={'errors': errors})
            except Exception:
                pass

        return (valid, errors)

    @staticmethod
    def validate_id(app_id: str) -> Tuple[bool, str]:
        """ID must be alphanumeric/-/_, 1-64 chars, start with alphanumeric."""
        if not app_id:
            return (False, 'id must not be empty')
        if not _ID_RE.match(app_id):
            return (False,
                    f"id '{app_id}' must be 1-64 chars, alphanumeric/-/_, "
                    f"starting with alphanumeric")
        return (True, '')

    @staticmethod
    def validate_type(app_type: str) -> Tuple[bool, str]:
        """Type must be a valid AppType enum value."""
        if app_type not in _VALID_TYPES:
            return (False,
                    f"type '{app_type}' not in valid types: "
                    f"{sorted(_VALID_TYPES)}")
        return (True, '')

    @staticmethod
    def validate_version(version: str) -> Tuple[bool, str]:
        """Version must be semver X.Y.Z or 'auto'."""
        if version == 'auto':
            return (True, '')
        if not _SEMVER_RE.match(version):
            return (False,
                    f"version '{version}' must be semver (X.Y.Z) or 'auto'")
        return (True, '')

    @staticmethod
    def validate_entry(app_type: str, entry: dict) -> Tuple[bool, str]:
        """Entry must contain required keys for the given AppType."""
        schema = ENTRY_SCHEMA.get(app_type)
        if schema is None:
            return (True, '')  # Unknown type already caught by validate_type

        required = schema.get('required', [])
        any_of = schema.get('any_of', [])

        for key in required:
            if key not in entry:
                return (False,
                        f"entry for type '{app_type}' missing required "
                        f"key '{key}'")

        if any_of and not any(k in entry for k in any_of):
            return (False,
                    f"entry for type '{app_type}' must have at least one "
                    f"of: {any_of}")

        return (True, '')

    @staticmethod
    def validate_permissions(permissions: list) -> Tuple[bool, str]:
        """All permissions must be in KNOWN_PERMISSIONS."""
        if not permissions:
            return (True, '')
        unknown = [p for p in permissions if p not in KNOWN_PERMISSIONS]
        if unknown:
            return (False,
                    f"unknown permissions: {unknown}. "
                    f"Valid: {sorted(KNOWN_PERMISSIONS)}")
        return (True, '')

    @staticmethod
    def validate_ai_capabilities(capabilities: list) -> Tuple[bool, List[str]]:
        """Validate AI capability declarations."""
        if not capabilities:
            return (True, [])

        errors = []
        for i, cap in enumerate(capabilities):
            if not isinstance(cap, dict):
                errors.append(f'ai_capabilities[{i}] must be a dict')
                continue

            cap_type = cap.get('type', '')
            if cap_type not in _VALID_AI_CAPABILITY_TYPES:
                errors.append(
                    f"ai_capabilities[{i}].type '{cap_type}' not in "
                    f"{sorted(_VALID_AI_CAPABILITY_TYPES)}")

            # Numeric bounds
            for field in ('min_accuracy', 'max_latency_ms', 'max_cost_spark'):
                val = cap.get(field, 0)
                if isinstance(val, (int, float)):
                    if math.isnan(val) or math.isinf(val):
                        errors.append(
                            f'ai_capabilities[{i}].{field} must not be '
                            f'NaN or Inf')
                    elif val < 0:
                        errors.append(
                            f'ai_capabilities[{i}].{field} must be >= 0')

            # Accuracy bounds
            accuracy = cap.get('min_accuracy', 0)
            if isinstance(accuracy, (int, float)) and not (
                    math.isnan(accuracy) or math.isinf(accuracy)):
                if accuracy > 1.0:
                    errors.append(
                        f'ai_capabilities[{i}].min_accuracy must be <= 1.0')

        return (len(errors) == 0, errors)

    @staticmethod
    def validate_size(size: tuple) -> Tuple[bool, str]:
        """Width/height must be positive integers, max 7680x4320."""
        if not isinstance(size, (tuple, list)) or len(size) != 2:
            return (False, 'default_size must be a (width, height) tuple')
        w, h = size
        if not isinstance(w, int) or not isinstance(h, int):
            return (False, 'default_size width/height must be integers')
        if w <= 0 or h <= 0:
            return (False, 'default_size width/height must be positive')
        if w > 7680 or h > 4320:
            return (False,
                    f'default_size {w}x{h} exceeds max 7680x4320')
        return (True, '')
