"""
STATE MACHINE IMPLEMENTATION
=====================================
"""

from enum import Enum
import logging
import os

logger = logging.getLogger(__name__)


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


class StateTransitionError(Exception):
    """Raised when an invalid state transition is attempted"""
    pass


# 2. UPDATE your set_action_state function to enforce transitions:
def set_action_state(user_prompt: str, action_id: int, state: ActionState, reason: str = ""):
    """Set state of an action with validation."""
    current_state = get_action_state(user_prompt, action_id)

    # Allow same-state transitions (idempotent)
    if current_state == state:
        return

    # Validate transition
    if not validate_state_transition(user_prompt, action_id, state):
        raise StateTransitionError(
            f"Invalid transition: Action {action_id} cannot go from {current_state.value} to {state.value}")

    # Perform transition
    if user_prompt not in action_states:
        action_states[user_prompt] = {}
    action_states[user_prompt][action_id] = state
    logger.info(f"🎯 Action {action_id}: {current_state.value} → {state.value} ({reason})")


# 3. ADD these wrapper functions for safe state updates:
def safe_set_state(user_prompt: str, action_id: int, new_state: ActionState, reason: str = ""):
    """Safely set state with error handling"""
    try:
        set_action_state(user_prompt, action_id, new_state, reason)
        return True
    except StateTransitionError as e:
        logger.error(f"❌ {e}")
        return False


def force_state_through_valid_path(user_prompt: str, action_id: int, target_state: ActionState, reason: str = ""):
    """Force state to target through valid transitions"""
    current_state = get_action_state(user_prompt, action_id)

    # Map of how to reach each target state from any current state
    state_paths = {
        # From ASSIGNED
        (ActionState.ASSIGNED, ActionState.IN_PROGRESS): [ActionState.IN_PROGRESS],
        (ActionState.ASSIGNED, ActionState.STATUS_VERIFICATION_REQUESTED): [ActionState.IN_PROGRESS,
                                                                            ActionState.STATUS_VERIFICATION_REQUESTED],
        (ActionState.ASSIGNED, ActionState.COMPLETED): [ActionState.IN_PROGRESS,
                                                        ActionState.STATUS_VERIFICATION_REQUESTED,
                                                        ActionState.COMPLETED],

        # From IN_PROGRESS
        (ActionState.IN_PROGRESS, ActionState.STATUS_VERIFICATION_REQUESTED): [
            ActionState.STATUS_VERIFICATION_REQUESTED],
        (ActionState.IN_PROGRESS, ActionState.COMPLETED): [ActionState.STATUS_VERIFICATION_REQUESTED,
                                                           ActionState.COMPLETED],

        # From STATUS_VERIFICATION_REQUESTED
        (ActionState.STATUS_VERIFICATION_REQUESTED, ActionState.COMPLETED): [ActionState.COMPLETED],
        (ActionState.STATUS_VERIFICATION_REQUESTED, ActionState.PENDING): [ActionState.PENDING],
        (ActionState.STATUS_VERIFICATION_REQUESTED, ActionState.ERROR): [ActionState.ERROR],

        # From COMPLETED
        (ActionState.COMPLETED, ActionState.FALLBACK_REQUESTED): [ActionState.FALLBACK_REQUESTED],
        (ActionState.COMPLETED, ActionState.FALLBACK_RECEIVED): [ActionState.FALLBACK_REQUESTED,
                                                                 ActionState.FALLBACK_RECEIVED],
        (ActionState.COMPLETED, ActionState.RECIPE_REQUESTED): [ActionState.FALLBACK_REQUESTED,
                                                                ActionState.FALLBACK_RECEIVED,
                                                                ActionState.RECIPE_REQUESTED],

        # From PENDING (two paths based on your flows)
        (ActionState.PENDING, ActionState.COMPLETED): [ActionState.COMPLETED],  # Flow #4
        (ActionState.PENDING, ActionState.ERROR): [ActionState.ERROR],  # Flow #3

        # From ERROR (retry path)
        (ActionState.ERROR, ActionState.IN_PROGRESS): [ActionState.IN_PROGRESS],
        (ActionState.ERROR, ActionState.COMPLETED): [ActionState.IN_PROGRESS, ActionState.STATUS_VERIFICATION_REQUESTED,
                                                     ActionState.COMPLETED],

        # From FALLBACK states
        (ActionState.FALLBACK_REQUESTED, ActionState.FALLBACK_RECEIVED): [ActionState.FALLBACK_RECEIVED],
        (ActionState.FALLBACK_RECEIVED, ActionState.RECIPE_REQUESTED): [ActionState.RECIPE_REQUESTED],

        # From RECIPE states
        (ActionState.RECIPE_REQUESTED, ActionState.RECIPE_RECEIVED): [ActionState.RECIPE_RECEIVED],
        (ActionState.RECIPE_RECEIVED, ActionState.TERMINATED): [ActionState.TERMINATED],
    }

    if current_state == target_state:
        return True

    # Get the path to target state
    path_key = (current_state, target_state)
    if path_key in state_paths:
        path = state_paths[path_key]
        logger.info(f"🔧 Auto-path for Action {action_id}: {current_state.value} → {target_state.value}")

        # Execute each step in the path
        for step_state in path:
            try:
                set_action_state(user_prompt, action_id, step_state, f"auto-path: {reason}")
            except StateTransitionError as e:
                logger.error(f"❌ Auto-path failed at {step_state.value}: {e}")
                return False
        return True
    else:
        logger.error(f"❌ No valid path from {current_state.value} to {target_state.value}")
        return False


# State tracking
action_states = {}  # {user_prompt: {action_id: current_state}}


def get_action_state(user_prompt: str, action_id: int) -> ActionState:
    """Get current state of an action."""
    return action_states.get(user_prompt, {}).get(action_id, ActionState.ASSIGNED)


def validate_state_transition(user_prompt: str, action_id: int, new_state: ActionState) -> bool:
    """Validate state transitions follow the exact sequence"""
    current_state = get_action_state(user_prompt, action_id)

    valid_transitions = {
        ActionState.ASSIGNED: [ActionState.IN_PROGRESS],
        ActionState.IN_PROGRESS: [ActionState.STATUS_VERIFICATION_REQUESTED],
        ActionState.STATUS_VERIFICATION_REQUESTED: [ActionState.COMPLETED, ActionState.PENDING, ActionState.ERROR],
        ActionState.COMPLETED: [ActionState.FALLBACK_REQUESTED],
        ActionState.PENDING: [ActionState.COMPLETED, ActionState.ERROR],
        ActionState.ERROR: [ActionState.IN_PROGRESS, ActionState.PENDING],  # Can retry or ask fallback
        ActionState.FALLBACK_REQUESTED: [ActionState.FALLBACK_RECEIVED],
        ActionState.FALLBACK_RECEIVED: [ActionState.RECIPE_REQUESTED],
        ActionState.RECIPE_REQUESTED: [ActionState.RECIPE_RECEIVED],
        ActionState.RECIPE_RECEIVED: [ActionState.TERMINATED],
        ActionState.TERMINATED: []  # Final state
    }

    allowed = valid_transitions.get(current_state, [])
    if new_state not in allowed:
        logger.error(f"❌ Invalid transition: {current_state.value} → {new_state.value}")
        return False

    logger.info(f"✅ Valid transition: {current_state.value} → {new_state.value}")
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
        group_chat.messages[-1]['name'] == 'ChatInstructor' and f'Action {current_action_id}' in group_chat.messages[-1]['content']):

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
    if (group_chat.messages and
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
    """12. Check if we can increment to next action"""
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
        for action_id in range(1, total_actions + 1):
            state = get_action_state(user_prompt, action_id)
            if state != ActionState.TERMINATED:
                incomplete_actions.append(f"Action {action_id}: {state.value}")

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
        total_actions = len(current_tasks.actions)
    else:
        total_actions = len(user_tasks.actions)

    for action_id in range(1, total_actions + 1):
        state = get_action_state(user_prompt, action_id)
        if state != ActionState.TERMINATED:
            return {
                'action': 'block',
                'message': f"Action {action_id} not terminated. Current state: {state.value}"
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


def debug_action_flow(user_prompt: str, action_id: int):
    """Debug specific action's flow pattern"""
    states = action_states.get(user_prompt, {})
    if action_id not in states:
        logger.info(f"Action {action_id}: Not started")
        return

    current_state = states[action_id]

    # Determine which of the 4 flows this matches
    if current_state == ActionState.TERMINATED:
        logger.info(f"✅ Action {action_id}: COMPLETED one of the 4 flows")
    elif current_state == ActionState.ERROR:
        logger.info(f"🔄 Action {action_id}: In ERROR (Flow #2 or #3)")
    elif current_state == ActionState.PENDING:
        logger.info(f"🔄 Action {action_id}: In PENDING (Flow #3 or #4)")
    else:
        logger.info(f"🔄 Action {action_id}: In progress ({current_state.value})")


# 6. ADD validation function for the 4 specific flows:
def validate_flow_pattern(user_prompt: str, action_id: int) -> str:
    """Identify which of the 4 flows this action followed"""
    # This would need action history tracking to be fully implemented
    # For now, just return current state info
    current_state = get_action_state(user_prompt, action_id)

    if current_state == ActionState.TERMINATED:
        return "completed_flow"
    elif current_state == ActionState.ERROR:
        return "error_flow_in_progress"
    elif current_state == ActionState.PENDING:
        return "pending_flow_in_progress"
    else:
        return "flow_in_progress"


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
