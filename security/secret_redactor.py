"""
Secret Redactor - Deterministic PII & Secret Detection for Hive Privacy

Prevents cross-user secret leakage:
  User A shares an API key in a conversation → it MUST NOT appear in
  User B's responses via the shared world model.

Design principle (from project steward):
  "The hive being hive should not get secrets from one person
   and reveal to another."

THREE-LAYER DEFENSE:
  Layer 1 - SECRET REDACTION (deterministic regex):
    Pattern-based, zero false negatives on known structured secrets.
    API keys, tokens, passwords, PEM keys, connection strings.
    Regex is correct here: structured secrets have known formats.

  Layer 2 - PER-USER ISOLATION (model-based PII + anonymization):
    Local LLM (Hevolve-Core / llama.cpp) semantically detects PII in
    free text: names, addresses, medical info, financial details.
    Falls back to regex for emails, phones, URLs, @mentions.
    user_id/prompt_id/node_id anonymized via SHA-256.

  Layer 3 - DIFFERENTIAL PRIVACY (statistical noise):
    Gaussian noise on latency, timestamp quantization, text truncation.

Why model-based PII (Layer 2)?
  Regex cannot catch semantic PII in free text: "I live at 123 Oak Lane"
  or "Dr. Smith diagnosed me with diabetes". The local LLM understands
  natural language and catches what regex misses.

Why regex for secrets (Layer 1)?
  Structured secrets (API keys, JWTs, PEM) have deterministic formats.
  Regex is faster, auditable, and has zero false negatives on known
  patterns. No model needed for these.

Patterns detected:
  - API keys (OpenAI, AWS, Google, Anthropic, Stripe, etc.)
  - Generic bearer/auth tokens
  - Passwords in connection strings or plaintext assignments
  - Credit card numbers (Luhn-validated)
  - Private keys (PEM format)
  - SSH private keys
  - JWT tokens
  - AWS access key IDs + secret keys
  - Connection strings (database, Redis, MongoDB)
  - Email + password combos
  - Generic hex/base64 secrets (64+ char)
"""

import json
import os
import re
import logging
import time
from typing import List, Tuple

logger = logging.getLogger('hevolve_social')

# Model-based PII detection - retry cooldown after failure
_model_last_failure = 0.0
_MODEL_RETRY_INTERVAL = 60  # Don't retry model for 60s after failure

# ─── Pattern definitions ─────────────────────────────────────────

# Each pattern: (name, compiled_regex, replacement_tag)
_SECRET_PATTERNS: List[Tuple[str, 're.Pattern', str]] = []


def _add(name: str, pattern: str, tag: str = None):
    _SECRET_PATTERNS.append((
        name,
        re.compile(pattern, re.IGNORECASE | re.DOTALL),
        tag or f'[REDACTED:{name}]',
    ))


# ── API Keys (vendor-specific) ──
_add('openai_key', r'\bsk-[A-Za-z0-9]{20,}T3BlbkFJ[A-Za-z0-9]{20,}\b')
_add('openai_key_v2', r'\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b')
_add('anthropic_key', r'\bsk-ant-[A-Za-z0-9_-]{40,}\b')
_add('aws_access_key', r'\bAKIA[0-9A-Z]{16}\b')
_add('aws_secret_key', r'(?:aws_secret_access_key|secret_key)\s*[=:]\s*["\']?([A-Za-z0-9/+=]{40})["\']?')
_add('google_api_key', r'\bAIza[A-Za-z0-9_-]{35}\b')
_add('stripe_key', r'\b[sr]k_(?:live|test)_[A-Za-z0-9]{24,}\b')
_add('github_token', r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b')
_add('slack_token', r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b')
_add('discord_token', r'[MN][A-Za-z\d]{23,}\.[\w-]{6}\.[\w-]{27,}')
_add('twilio_key', r'\bSK[a-f0-9]{32}\b')
_add('sendgrid_key', r'\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b')
_add('mailgun_key', r'\bkey-[A-Za-z0-9]{32}\b')
_add('heroku_key', r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')

# ── Generic bearer/auth tokens ──
_add('bearer_token',
     r'(?:bearer|authorization|token|auth)[\s:=]+["\']?([A-Za-z0-9._~+/=-]{32,})["\']?')

# ── Passwords ──
_add('password_assignment',
     r'(?:password|passwd|pwd|secret|api_key|apikey|access_token|auth_token)'
     r'\s*[=:]\s*["\']([^"\']{8,})["\']')
_add('password_plaintext',
     r'(?:password|passwd|pwd)\s*[=:]\s*(\S{8,})')

# ── PEM private keys ──
_add('pem_private_key',
     r'-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----'
     r'[\s\S]*?'
     r'-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----')

# ── SSH private keys ──
_add('ssh_private_key',
     r'-----BEGIN OPENSSH PRIVATE KEY-----[\s\S]*?-----END OPENSSH PRIVATE KEY-----')

# ── JWT tokens ──
_add('jwt_token',
     r'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')

# ── Connection strings ──
_add('connection_string',
     r'(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp|mssql)'
     r'://[^\s"\'<>]{10,}')

# ── Credit card numbers (basic format - 4 groups of 4 digits) ──
_add('credit_card',
     r'\b(?:\d{4}[-\s]?){3}\d{4}\b')

# ── Generic long hex secrets (64+ chars, likely SHA/HMAC) ──
_add('hex_secret',
     r'(?:secret|key|token|hash|signature)\s*[=:]\s*["\']?([0-9a-fA-F]{64,})["\']?')

# ── Generic long base64 secrets (48+ chars) ──
_add('base64_secret',
     r'(?:secret|key|token|private)\s*[=:]\s*["\']?([A-Za-z0-9+/]{48,}={0,3})["\']?')


# ─── Luhn check for credit cards ─────────────────────────────────

def _luhn_check(number_str: str) -> bool:
    """Validate credit card number using Luhn algorithm."""
    digits = [int(d) for d in number_str if d.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# ─── Main redaction function ─────────────────────────────────────

def redact_secrets(text: str) -> Tuple[str, int]:
    """Scan text and replace detected secrets with [REDACTED:<type>] tokens.

    Returns:
        (redacted_text, count_of_redactions)

    Thread-safe: uses only compiled regex (immutable) and local variables.
    """
    if not text:
        return text, 0

    count = 0
    result = text

    for name, pattern, tag in _SECRET_PATTERNS:
        matches = pattern.findall(result)
        if matches:
            # For credit cards, validate with Luhn before redacting
            if name == 'credit_card':
                for match in matches:
                    digits_only = ''.join(c for c in match if c.isdigit())
                    if _luhn_check(digits_only):
                        result = result.replace(match, tag)
                        count += 1
            else:
                new_result = pattern.sub(tag, result)
                if new_result != result:
                    count += len(matches)
                    result = new_result

    return result, count


def redact_experience(experience: dict) -> dict:
    """Full privacy pipeline for shared world model ingestion.

    THREE LAYERS applied in sequence:

    Layer 1 - SECRET REDACTION (deterministic regex):
      API keys, tokens, passwords, PEM keys, etc. → [REDACTED:<type>]

    Layer 2 - PER-USER ISOLATION:
      - user_id → anonymized hash (not reversible)
      - prompt_id → anonymized hash (not reversible)
      - Quoted text stripped (verbatim content from other systems)
      - Email addresses stripped
      - Phone numbers stripped
      - Names/handles removed from text body

    Layer 3 - DIFFERENTIAL PRIVACY (statistical noise):
      - Gaussian noise on latency_ms (ε=1.0)
      - Timestamp quantized to 5-minute buckets (k-anonymity)
      - node_id anonymized (learn patterns, not node identity)
      - Text truncated to 500 chars (reduce memorization surface)

    Returns a NEW dict (does not mutate input).
    """
    import hashlib
    import random

    redacted = dict(experience)

    # ── Layer 1: Secret redaction ──
    total_redactions = 0
    for field in ('prompt', 'response'):
        if field in redacted and redacted[field]:
            redacted[field], n = redact_secrets(str(redacted[field]))
            total_redactions += n

    if total_redactions > 0:
        logger.info(
            f"[SecretRedactor] Redacted {total_redactions} secret(s) "
            f"from experience (prompt_id={redacted.get('prompt_id', '?')})")

    # ── Layer 2: Per-user isolation ──

    # Anonymize user_id - the world model learns PATTERNS, not who said what
    if 'user_id' in redacted and redacted['user_id']:
        uid_hash = hashlib.sha256(
            str(redacted['user_id']).encode()).hexdigest()[:8]
        redacted['user_id'] = f'anon_{uid_hash}'

    # Anonymize prompt_id - prevent cross-session correlation
    if 'prompt_id' in redacted and redacted['prompt_id']:
        pid_hash = hashlib.sha256(
            str(redacted['prompt_id']).encode()).hexdigest()[:8]
        redacted['prompt_id'] = f'prompt_{pid_hash}'

    # Strip PII from text fields - model-based detection with regex fallback.
    # The model catches semantic PII (names, addresses, medical info) that
    # regex cannot. Falls back to regex-only if model is unavailable.
    for field in ('prompt', 'response'):
        if field in redacted and redacted[field]:
            redacted[field] = _model_detect_pii(str(redacted[field]))

    # ── Layer 3: Differential privacy ──

    # Gaussian noise on latency (ε ≈ 1.0, sensitivity = 100ms)
    if 'latency_ms' in redacted and redacted['latency_ms']:
        noise = random.gauss(0, 50)  # σ=50ms
        redacted['latency_ms'] = max(0, round(redacted['latency_ms'] + noise, 1))

    # Quantize timestamp to 5-minute buckets (k-anonymity)
    if 'timestamp' in redacted and redacted['timestamp']:
        bucket = 300  # 5 minutes
        redacted['timestamp'] = (redacted['timestamp'] // bucket) * bucket

    # Anonymize node_id - learn compute patterns, not node identity
    if 'node_id' in redacted and redacted['node_id']:
        nid_hash = hashlib.sha256(
            str(redacted['node_id']).encode()).hexdigest()[:8]
        redacted['node_id'] = f'node_{nid_hash}'

    # Truncate text for shared learning - reduces memorization surface.
    # The world model needs PATTERNS, not full conversations.
    for field in ('prompt', 'response'):
        if field in redacted and redacted[field]:
            redacted[field] = redacted[field][:500]

    return redacted


# ─── Layer 2: PII stripping patterns ────────────────────────────

# Email addresses
_EMAIL_PATTERN = re.compile(
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')

# Phone numbers (various formats)
_PHONE_PATTERN = re.compile(
    r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b')

# Quoted text (content from other systems/people - high risk of verbatim leak)
_QUOTED_PATTERN = re.compile(
    r'(?:^|\n)\s*>.*(?:\n\s*>.*)*', re.MULTILINE)

# URLs with query params (may contain tokens/session IDs)
_URL_WITH_PARAMS = re.compile(
    r'https?://\S+[?&]\S+')

# @mentions / handles
_MENTION_PATTERN = re.compile(r'@[A-Za-z0-9_]{2,30}\b')


def _strip_pii(text: str) -> str:
    """Strip PII patterns from text for per-user isolation.

    Removes emails, phone numbers, quoted content, URL params, @mentions.
    Deterministic and fast (regex-only).
    """
    if not text:
        return text

    result = _EMAIL_PATTERN.sub('[EMAIL]', text)
    result = _PHONE_PATTERN.sub('[PHONE]', result)
    result = _QUOTED_PATTERN.sub('\n[QUOTED_CONTENT_REMOVED]', result)
    result = _URL_WITH_PARAMS.sub('[URL_REDACTED]', result)
    result = _MENTION_PATTERN.sub('[HANDLE]', result)

    return result


def _model_detect_pii(text: str) -> str:
    """Use local LLM to semantically detect PII in free text.

    Catches PII that regex cannot: names, addresses, medical info,
    financial details, biographical information.

    Applies regex FIRST (fast, catches emails/phones/URLs), then
    enhances with model-detected entities (names, addresses, etc.).

    Falls back to regex-only if model is unavailable.
    """
    global _model_last_failure

    if not text or len(text) < 20:
        return _strip_pii(text)

    # Skip model call if recently failed (avoid repeated timeouts)
    if time.time() - _model_last_failure < _MODEL_RETRY_INTERVAL:
        return _strip_pii(text)

    # Apply regex first (always runs, sub-ms)
    regex_result = _strip_pii(text)

    try:
        import requests as _req

        # Use whatever local LLM endpoint is available
        llm_url = os.environ.get(
            'HEVOLVE_LOCAL_LLM_URL',
            os.environ.get('HEVOLVEAI_API_URL', 'http://localhost:8080')
        )

        resp = _req.post(
            f'{llm_url.rstrip("/")}/v1/chat/completions',
            json={
                'model': 'local',
                'messages': [{
                    'role': 'user',
                    'content': (
                        "Extract ALL personally identifiable information (PII) "
                        "from the text below. PII includes: full names, "
                        "street addresses, dates of birth, government IDs "
                        "(SSN, passport), medical conditions/diagnoses, "
                        "financial account numbers, IP addresses, "
                        "biometric data, and any other info that could "
                        "identify a specific person.\n"
                        "Do NOT include generic words, technical terms, "
                        "or non-identifying information.\n"
                        "Return ONLY a JSON array of the exact PII strings "
                        "found. Return [] if none found.\n\n"
                        f"Text: {text[:1000]}\n\nPII:"
                    ),
                }],
                'max_tokens': 256,
                'temperature': 0.0,
            },
            timeout=3,
        )

        if resp.status_code == 200:
            result = resp.json()
            content = result.get('choices', [{}])[0].get(
                'message', {}).get('content', '')

            # Parse JSON array from response
            start = content.find('[')
            end = content.rfind(']')
            if start != -1 and end != -1:
                pii_items = json.loads(content[start:end + 1])
                if isinstance(pii_items, list):
                    for item in pii_items:
                        if isinstance(item, str) and len(item) >= 2:
                            regex_result = regex_result.replace(
                                item, '[PII_REDACTED]')
                    return regex_result
        # Non-200 or unparseable - regex result is still good
        _model_last_failure = time.time()
        return regex_result

    except Exception:
        _model_last_failure = time.time()
        return regex_result


def contains_secrets(text: str) -> bool:
    """Quick check: does this text contain any detectable secrets?"""
    _, count = redact_secrets(text)
    return count > 0
