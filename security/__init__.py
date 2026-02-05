"""
HevolveBot Security Module
Provides encryption, authentication, input validation, and defense against
known OpenClaw/SantaClaw attack vectors (CVE-2025-6514, GHSA-g8p2-7wf7-98mq).
"""

from .secrets_manager import SecretsManager
from .crypto import encrypt_data, decrypt_data, A2ACrypto
from .tls_config import get_secure_session, upgrade_url
from .middleware import apply_security_middleware
from .safe_deserialize import safe_load_frame, safe_dump_frame
from .sanitize import escape_like, sanitize_path, sanitize_html, validate_input
from .jwt_manager import JWTManager
from .rate_limiter_redis import RedisRateLimiter
from .mcp_sandbox import MCPSandbox
from .prompt_guard import check_prompt_injection, sanitize_user_input_for_llm
from .audit_log import SensitiveFilter, get_secure_logger

__all__ = [
    'SecretsManager',
    'encrypt_data', 'decrypt_data', 'A2ACrypto',
    'get_secure_session', 'upgrade_url',
    'apply_security_middleware',
    'safe_load_frame', 'safe_dump_frame',
    'escape_like', 'sanitize_path', 'sanitize_html', 'validate_input',
    'JWTManager',
    'RedisRateLimiter',
    'MCPSandbox',
    'check_prompt_injection', 'sanitize_user_input_for_llm',
    'SensitiveFilter', 'get_secure_logger',
]
