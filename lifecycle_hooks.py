"""
STATE MACHINE IMPLEMENTATION
=====================================
"""

from enum import Enum
import re
import json
import logging
import os

logger = logging.getLogger(__name__)
# ────────────────────────────────────────────────
# ID-conversion helpers  ← NEW
# ────────────────────────────────────────────────
def ext_id(internal_id: int) -> int:
    """0-based index ➜ 1-based ID shown in chat."""
    return internal_id + 1

def int_id(external_id: int) -> int:
    """1-based ID from JSON/chat ➜ 0-based index."""
    return external_id - 1

class ActionState(Enum):
    """Updated state machine to match exact user requirements"""
    ASSIGNED = "assigned"                           # 1. Assign Each Action from array
    IN_PROGRESS = "in_progress"                    # 2. Action execute in progress
    STATUS_VERIFICATION_REQUESTED = "status_verification_requested"  # 3. Status requested to verifier
    COMPLETED = "completed"                        # 4. Action performed successfully and verified
    PENDING = "pending"                           # 5. Action pending completion by verifier
    ERROR = "error"                               # 6. Action error or json error
    FALLBACK_REQUESTED = "fallback_requested"     # 7. Action fallback requested to user
    FALLBACK_RECEIVED = "fallback_received"       # 8. Action fallback received from user
    RECIPE_REQUESTED = "recipe_requested"         # 9. Action recipe json creation requested to AI
    RECIPE_RECEIVED = "recipe_received"           # 10. Action recipe json received with status done
    TERMINATED = "terminated"                     # 11. Action passed to chat instructor and Terminate issued

# State tracking
action_states = {}  # {user_prompt: {action_id: current_state}}

def get_action_state(user_prompt: str, action_id: int) -> ActionState:
    """Get current state of an action."""
    return action_states.get(user_prompt, {}).get(action_id, ActionState.ASSIGNED)

def set_action_state(user_prompt: str, action_id: int, state: ActionState):
    """Set state of an action."""
    if user_prompt not in action_states:
        action_states[user_prompt] = {}
    action_states[user_prompt][action_id] = state
    logger.info(f"🎯 Action {action_id} state: {state.value}")

valid_transitions = {
    ActionState.ASSIGNED:  [ActionState.IN_PROGRESS],

    ActionState.IN_PROGRESS:  [ActionState.STATUS_VERIFICATION_REQUESTED],

    # Verifier can accept, ask again, or flag error
    ActionState.STATUS_VERIFICATION_REQUESTED: [
        ActionState.COMPLETED,
        ActionState.PENDING,
        ActionState.ERROR
    ],

    # ▸ NEW: if no fallback needed, jump straight to recipe stage
    ActionState.COMPLETED: [
        ActionState.FALLBACK_REQUESTED,
        ActionState.RECIPE_REQUESTED        # ← optional fast-track
    ],

    # User is still thinking or Verifier says “wait”
    ActionState.PENDING: [
        ActionState.STATUS_VERIFICATION_REQUESTED,
        ActionState.COMPLETED,
        ActionState.ERROR
    ],

    # Retry or hand back to verifier
    ActionState.ERROR: [
        ActionState.IN_PROGRESS,
        ActionState.STATUS_VERIFICATION_REQUESTED,
        ActionState.PENDING,
        ActionState.TERMINATED             # ← optional hard-stop
    ],

    ActionState.FALLBACK_REQUESTED:  [ActionState.FALLBACK_RECEIVED],

    # ▸ NEW: allow re-verification if fallback data wasn’t good enough
    ActionState.FALLBACK_RECEIVED: [
        ActionState.RECIPE_REQUESTED,
        ActionState.STATUS_VERIFICATION_REQUESTED   # ← optional loop-back
    ],

    ActionState.RECIPE_REQUESTED: [ActionState.RECIPE_RECEIVED],

    ActionState.RECIPE_RECEIVED:  [ActionState.TERMINATED],

    ActionState.TERMINATED: []     # final
}


def validate_state_transition(user_prompt, action_id, new_state: ActionState) -> bool:
    current_state = get_action_state(user_prompt, action_id)
    allowed = valid_transitions.get(current_state, [])
    if new_state not in allowed:
        logger.error(f"❌ Illegal hop {current_state.value} → {new_state.value} detected and fixed")
        return False
    logger.info(f"✅ {current_state.value} → {new_state.value}")
    return True

def lifecycle_hook_track_action_assignment(user_prompt: str, user_tasks, group_chat) -> bool:
    """1. Track when action is assigned from array"""
    if isinstance(user_tasks, dict):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return False
        current_action_id = current_tasks.current_action
    else:
        current_action_id = user_tasks.current_action

    # When ChatInstructor assigns action, move from ASSIGNED to IN_PROGRESS
    if (group_chat.messages and
        group_chat.messages[-1]['name'] == 'ChatInstructor' and
        f'Action {ext_id(current_action_id)}' in group_chat.messages[-1]['content']):

        if validate_state_transition(user_prompt, current_action_id, ActionState.IN_PROGRESS):
            set_action_state(user_prompt, current_action_id, ActionState.IN_PROGRESS)
            return True

    return False

def lifecycle_hook_track_status_verification_request(user_prompt: str, user_tasks, group_chat) -> bool:
    """3. Track when status verification is requested"""
    if isinstance(user_tasks, dict):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return False
        current_action_id = current_tasks.current_action
    else:
        current_action_id = user_tasks.current_action

    # When @StatusVerifier is mentioned, move to STATUS_VERIFICATION_REQUESTED
    if (group_chat.messages and
        '@StatusVerifier' in group_chat.messages[-1]['content']):

        if validate_state_transition(user_prompt, current_action_id, ActionState.STATUS_VERIFICATION_REQUESTED):
            set_action_state(user_prompt, current_action_id, ActionState.STATUS_VERIFICATION_REQUESTED)
            return True

    return False



def lifecycle_hook_process_verifier_response(user_prompt: str, json_obj: dict, user_tasks) -> dict:
    """4-6. Process verifier response: completed/pending/error"""
    if not json_obj or 'status' not in json_obj:
        return {'action': 'allow', 'message': None}

    if isinstance(user_tasks, dict):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return {'action': 'allow', 'message': None}
        current_action_id = current_tasks.current_action
    else:
        current_action_id = user_tasks.current_action

    status = json_obj['status'].lower()
    current_state = get_action_state(user_prompt, current_action_id)

    # Must be in STATUS_VERIFICATION_REQUESTED to process verifier response
    if current_state != ActionState.STATUS_VERIFICATION_REQUESTED:
        return {'action': 'allow', 'message': None}

    if status == 'completed':
        if validate_state_transition(user_prompt, current_action_id, ActionState.COMPLETED):
            set_action_state(user_prompt, current_action_id, ActionState.COMPLETED)
            # Automatically request fallback after completion
            return {
                'action': 'force_fallback',
                'message': f"Action {current_action_id} fallback: ask user what actions should be taken if current actions fail in the future after you get the response from user give the conversation to StatusVerifier agent"
            }

    elif status == 'pending':
        if validate_state_transition(user_prompt, current_action_id, ActionState.PENDING):
            set_action_state(user_prompt, current_action_id, ActionState.PENDING)
            return {
                'action': 'force_completion',
                'message': f"Complete pending steps for action {current_action_id} and ask @StatusVerifier to verify completion"
            }

    elif status == 'error':
        if validate_state_transition(user_prompt, current_action_id, ActionState.ERROR):
            set_action_state(user_prompt, current_action_id, ActionState.ERROR)
            return {
                'action': 'force_retry',
                'message': f"Error in action {current_action_id}: {json_obj.get('message', 'Unknown error')}. Please resolve and retry."
            }

    return {'action': 'allow', 'message': None}

def lifecycle_hook_track_fallback_request(user_prompt: str, user_tasks, group_chat) -> bool:
    """7. Track when fallback is requested to user"""
    if isinstance(user_tasks, dict):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return False
        current_action_id = current_tasks.current_action
    else:
        current_action_id = user_tasks.current_action

    # When fallback is requested, move to FALLBACK_REQUESTED
    current_state = get_action_state(user_prompt, current_action_id)  # ← NEW

    # Only allow fallback after the action is COMPLETED
    if (current_state == ActionState.COMPLETED and                     # ← NEW
        group_chat.messages and
        'fallback' in group_chat.messages[-1]['content'].lower() and
        'ask user' in group_chat.messages[-1]['content'].lower()):

        if validate_state_transition(user_prompt, current_action_id, ActionState.FALLBACK_REQUESTED):
            set_action_state(user_prompt, current_action_id, ActionState.FALLBACK_REQUESTED)
            return True

    return False

def lifecycle_hook_track_user_fallback(user_prompt: str, user_tasks, group_chat) -> bool:
    """8. Track when fallback is received from user"""
    if isinstance(user_tasks, dict):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return False
        current_action_id = current_tasks.current_action
    else:
        current_action_id = user_tasks.current_action

    current_state = get_action_state(user_prompt, current_action_id)

    # When user responds to fallback request
    if (current_state == ActionState.FALLBACK_REQUESTED and
        group_chat.messages and
        group_chat.messages[-1]['name'] == 'UserProxy'):

        if validate_state_transition(user_prompt, current_action_id, ActionState.FALLBACK_RECEIVED):
            set_action_state(user_prompt, current_action_id, ActionState.FALLBACK_RECEIVED)
            return True

    return False

def lifecycle_hook_track_recipe_request(user_prompt: str, user_tasks, group_chat) -> bool:
    """9. Track when recipe creation is requested"""
    if isinstance(user_tasks, dict):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return False
        current_action_id = current_tasks.current_action
    else:
        current_action_id = user_tasks.current_action

    # When recipe creation is requested
    if (group_chat.messages and
        'Focus on the current task at hand and create a detailed recipe' in group_chat.messages[-1]['content']):

        if validate_state_transition(user_prompt, current_action_id, ActionState.RECIPE_REQUESTED):
            set_action_state(user_prompt, current_action_id, ActionState.RECIPE_REQUESTED)
            return True

    return False

def lifecycle_hook_track_recipe_completion(user_prompt: str, json_obj: dict, user_tasks) -> dict:
    """10. Track when recipe is received and saved"""
    if not json_obj or json_obj.get('status', '').lower() != 'done':
        return {'action': 'allow', 'message': None}

    if isinstance(user_tasks, dict):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return {'action': 'allow', 'message': None}
        current_action_id = current_tasks.current_action
    else:
        current_action_id = user_tasks.current_action

    current_state = get_action_state(user_prompt, current_action_id)

    if current_state == ActionState.RECIPE_REQUESTED:
        if validate_state_transition(user_prompt, current_action_id, ActionState.RECIPE_RECEIVED):
            set_action_state(user_prompt, current_action_id, ActionState.RECIPE_RECEIVED)
            return {
                'action': 'save_recipe_and_terminate',
                'message': f"Recipe received for action {current_action_id}. Save and proceed to termination."
            }

    return {'action': 'allow', 'message': None}

def lifecycle_hook_track_termination(user_prompt: str, user_tasks, group_chat) -> bool:
    """11. Track when action is terminated and passed to chat instructor"""
    if isinstance(user_tasks, dict):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return False
        current_action_id = current_tasks.current_action
    else:
        current_action_id = user_tasks.current_action

    # When TERMINATE is issued
    if (group_chat.messages and
        group_chat.messages[-1]['content'] == 'TERMINATE'):

        if validate_state_transition(user_prompt, current_action_id, ActionState.TERMINATED):
            set_action_state(user_prompt, current_action_id, ActionState.TERMINATED)
            return True

    return False

def lifecycle_hook_can_increment_action(user_prompt: str, user_tasks) -> dict:
    """12. Check if can increment to next action"""
    if isinstance(user_tasks, dict):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return {'action': 'allow', 'message': None}
        current_action_id = current_tasks.current_action
    else:
        current_action_id = user_tasks.current_action

    current_state = get_action_state(user_prompt, current_action_id)

    if current_state != ActionState.TERMINATED:
        return {
            'action': 'block',
            'message': f"Cannot increment to next action. Action {current_action_id} must reach TERMINATED state first. Current state: {current_state.value}"
        }

    return {'action': 'allow', 'message': None}

def lifecycle_hook_check_all_actions_complete(user_prompt: str, user_tasks) -> dict:
    """13. Check if all actions in array are exhausted and can create flow recipe"""
    if isinstance(user_tasks, dict):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return {'action': 'allow', 'message': None}
        total_actions = len(current_tasks.actions)
        current_action = current_tasks.current_action
    else:
        total_actions = len(user_tasks.actions)
        current_action = user_tasks.current_action

    # Check if all actions completed
    if current_action >= total_actions:
        # Verify all actions reached TERMINATED state
        incomplete_actions = []
        for idx in range(total_actions):               # 0 … total-1
            state = get_action_state(user_prompt, idx)
            if state != ActionState.TERMINATED:
                incomplete_actions.append(f"Action {idx}: {state.value}")

        if incomplete_actions:
            return {
                'action': 'block_flow_completion',
                'message': f"Cannot create flow recipe. Incomplete actions: {incomplete_actions}"
            }

        return {
            'action': 'create_flow_recipe',
            'message': "All actions completed. Create flow recipe for personas."
        }

    return {'action': 'continue_actions', 'message': None}

def lifecycle_hook_validate_final_agent_creation(user_prompt: str, user_tasks, prompt_id: int) -> dict:
    """16. Final validation before 'Agent created successfully'"""

    # Check 1: All actions reached TERMINATED
    if isinstance(user_tasks, dict):
        current_tasks = user_tasks.get(user_prompt)
        if not current_tasks:
            return {'action': 'block', 'message': 'No tasks found'}
        current_action_id = current_tasks.current_action

    total_actions = len(current_tasks.actions)

    for idx in range(total_actions):               # 0 … total-1
        state = get_action_state(user_prompt, idx)
        if state != ActionState.TERMINATED:
            return {
                'action': 'block',
                'message': f"Action {current_action_id} not terminated. Current state: {state.value}"
            }

    # Check 2: All recipe files exist
    flow = 0  # Assuming recipe_for_persona logic
    missing_files = []

    for action_id in range(1, total_actions + 1):
        recipe_file = f'prompts/{prompt_id}_{flow}_{action_id}.json'
        if not os.path.exists(recipe_file):
            missing_files.append(recipe_file)

    if missing_files:
        return {
            'action': 'block',
            'message': f"Missing recipe files: {missing_files}"
        }

    # Check 3: Flow recipe exists
    flow_recipe_file = f'prompts/{prompt_id}_{flow}_recipe.json'
    if not os.path.exists(flow_recipe_file):
        return {
            'action': 'block',
            'message': f"Missing flow recipe: {flow_recipe_file}"
        }

    # Check 4: Timer tasks executed (if required)
    # This would need additional tracking based on your timer execution logic

    return {
        'action': 'allow',
        'message': "✅ All validations passed - Agent creation ready"
    }

def debug_lifecycle_status(user_prompt: str):
    """Debug function to show current lifecycle status"""
    states = action_states.get(user_prompt, {})
    logger.info(f"\n🔍 Lifecycle Status for {user_prompt}:")
    logger.info("-" * 50)

    for action_id, state in states.items():
        terminated = "✅ TERMINATED" if state == ActionState.TERMINATED else "🔄 IN PROGRESS"
        logger.info(f"Action {action_id}: {state.value} {terminated}")

def initialize_deterministic_actions():
    """Initialize the state machine"""
    logger.info("🎯 Deterministic action lifecycle initialized")
    return True

def initialize_minimal_lifecycle_hooks():
    """Alias for initialization"""
    return initialize_deterministic_actions()