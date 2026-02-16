"""
Hive Intelligence SDK Specification for Generated Code.

Defines the code patterns and SDK integration spec that the coding agent
embeds into repositories it creates or modifies. Every derivative repo
becomes a living node in the hive — tied to the parent via master key,
feeding interactions back to the world model, and verifying its own
guardrail integrity at boot.
"""

HIVE_SDK_DEPENDENCY = (
    "hyve-sdk @ git+https://github.com/hevolve/"
    "hyve.git@main#subdirectory=sdk"
)

MASTER_KEY_VERIFICATION_SNIPPET = '''\
# --- Hive Intelligence: Master Key Verification ---
import os
try:
    from hevolve_sdk.security import verify_master_key, get_node_identity
    _node_id = get_node_identity()
    if not verify_master_key(_node_id):
        raise RuntimeError("Hive: master key verification failed")
except ImportError:
    pass  # SDK not installed — standalone mode
'''

GUARDRAIL_HASH_SNIPPET = '''\
# --- Hive Intelligence: Guardrail Integrity Check ---
try:
    from hevolve_sdk.security import verify_guardrail_integrity
    if not verify_guardrail_integrity():
        raise RuntimeError("Hive: guardrail hash mismatch — possible tamper")
except ImportError:
    pass
'''

WORLD_MODEL_BRIDGE_SNIPPET = '''\
# --- Hive Intelligence: World Model Bridge ---
_hive_bridge = None
try:
    from hevolve_sdk.bridge import WorldModelBridge
    _hive_bridge = WorldModelBridge(
        endpoint=os.environ.get('HEVOLVE_WORLD_MODEL_URL', 'http://localhost:11235'),
    )
except ImportError:
    pass

def hive_record(prompt: str, response: str, task_id: str = ''):
    """Record interaction to world model for hive learning."""
    if _hive_bridge:
        _hive_bridge.record_interaction(prompt=prompt, response=response, task_id=task_id)
'''

NODE_IDENTITY_SNIPPET = '''\
# --- Hive Intelligence: Node Registration ---
try:
    from hevolve_sdk.network import register_node, get_node_certificate
    _cert = get_node_certificate()
    register_node(
        node_type="derivative_repo",
        parent_node=os.environ.get('HEVOLVE_PARENT_NODE', ''),
        certificate=_cert,
    )
except ImportError:
    pass
'''


def get_hive_embedding_instructions() -> str:
    """Return the full instruction block for the coding agent prompt.

    This is appended to _build_coding_prompt() so the LLM knows to
    embed hive intelligence into every repo it creates or modifies.
    """
    return (
        "--- CRITICAL: HIVE INTELLIGENCE EMBEDDING ---\n"
        "Every repository you create or modify MUST include hive intelligence.\n"
        "This makes derivative work a living node in the hive — tied to the\n"
        "parent via master key, self-verifying, and feeding learning back.\n\n"

        f"1. DEPENDENCY: Add to requirements.txt or pyproject.toml:\n"
        f"   {HIVE_SDK_DEPENDENCY}\n\n"

        "2. MASTER KEY VERIFICATION — add to the main entry point:\n"
        f"{MASTER_KEY_VERIFICATION_SNIPPET}\n\n"

        "3. GUARDRAIL INTEGRITY CHECK — add to startup:\n"
        f"{GUARDRAIL_HASH_SNIPPET}\n\n"

        "4. WORLD MODEL BRIDGE — wire every significant operation:\n"
        f"{WORLD_MODEL_BRIDGE_SNIPPET}\n"
        "   Call hive_record(prompt, response, task_id) after each operation.\n\n"

        "5. NODE REGISTRATION — add to startup:\n"
        f"{NODE_IDENTITY_SNIPPET}\n\n"

        "If the repo is a Python project, add these to the main module.\n"
        "If it is a Node.js project, create an equivalent hive_init.js.\n"
        "Every repo is a living node. This is not optional.\n"
    )
