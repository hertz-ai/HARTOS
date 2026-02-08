"""
STATE MACHINE IMPLEMENTATION
=====================================

This module manages the ActionState state machine for tracking action lifecycle.
It also provides functions to sync ActionState with SmartLedger TaskStatus.
"""

from enum import Enum
import logging
import os
import threading
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

# Lock protecting _ledger_registry and action_states (accessed by multiple Waitress/Gunicorn threads)
_state_lock = threading.RLock()

# Global ledger registry for auto-sync
_ledger_registry: Dict[str, Any] = {}

def register_ledger_for_session(user_prompt: str, ledger: Any):
    """Register a ledger instance for a session to enable auto-sync."""
    with _state_lock:
        _ledger_registry[user_prompt] = ledger
    logger.debug(f"Registered ledger for {user_prompt}")

def get_registered_ledger(user_prompt: str) -> Optional[Any]:
    """Get the registered ledger for a session."""
    with _state_lock:
        return _ledger_registry.get(user_prompt)

def _auto_sync_to_ledger(user_prompt: str, action_id: int, state: 'ActionState'):
    """Auto-sync state change to ledger if registered."""
    ledger = _ledger_registry.get(user_prompt)
    if ledger is None:
        return  # No ledger registered, skip sync

    try:
        LedgerTaskStatus = _get_ledger_task_status()
        task_id = f"action_{action_id}"

        if task_id not in ledger.tasks:
            return  # Task doesn't exist in ledger

        # Map ActionState to LedgerTaskStatus
        STATE_MAP = {
            ActionState.ASSIGNED: LedgerTaskStatus.PENDING,
            ActionState.IN_PROGRESS: LedgerTaskStatus.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED: LedgerTaskStatus.VALIDATING,
            ActionState.COMPLETED: LedgerTaskStatus.COMPLETED,
            ActionState.PENDING: LedgerTaskStatus.BLOCKED,
            ActionState.ERROR: LedgerTaskStatus.FAILED,
            ActionState.FALLBACK_REQUESTED: LedgerTaskStatus.BLOCKED,
            ActionState.FALLBACK_RECEIVED: LedgerTaskStatus.IN_PROGRESS,
            ActionState.RECIPE_REQUESTED: LedgerTaskStatus.IN_PROGRESS,
            ActionState.RECIPE_RECEIVED: LedgerTaskStatus.COMPLETED,
            ActionState.TERMINATED: LedgerTaskStatus.COMPLETED,
        }

        ledger_status = STATE_MAP.get(state)
        if ledger_status:
            ledger.update_task_status(task_id, ledger_status, reason=f"ActionState: {state.value}")
            logger.info(f"📋 Auto-synced {task_id} → {ledger_status.value} (ActionState: {state.value})")
    except Exception as e:
        logger.error(f"Failed to auto-sync to ledger: {e}", exc_info=True)

# Import ledger types for sync function (lazy import to avoid circular deps)
def _get_ledger_task_status():
    """Lazy import to avoid circular dependencies"""
    from agent_ledger import TaskStatus as LedgerTaskStatus
    return LedgerTaskStatus

# Add new states to ActionState enum:
class FlowState(Enum):
    DEPENDENCY_ANALYSIS = "dependency_analysis"
    TOPOLOGICAL_SORT = "topological_sort"
    SCHEDULED_JOBS_CREATION = "scheduled_jobs_creation"
    FLOW_RECIPE_CREATION = "flow_recipe_creation"
    FLOW_COMPLETED = "flow_completed"

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


# Add to lifecycle_hooks.py
class FlowLifecycleState:
    """Track overall flow lifecycle beyond individual actions"""

    def __init__(self):
        self.flows = {}  # {user_prompt: {flow_id: state}}

    def set_flow_state(self, user_prompt, flow_id, state):
        if user_prompt not in self.flows:
            self.flows[user_prompt] = {}
        self.flows[user_prompt][flow_id] = state


flow_lifecycle = FlowLifecycleState()


# Action retry tracking to prevent infinite loops
class ActionRetryTracker:
    """Track retry counts to force ERROR state after threshold"""

    def __init__(self):
        self.pending_counts = {}  # {(user_prompt, action_id): count}
        self.MAX_PENDING_RETRIES = 3  # Force ERROR after 3 pending attempts

    def increment_pending(self, user_prompt, action_id):
        """Increment pending count and return True if threshold exceeded"""
        key = (user_prompt, action_id)
        count = self.pending_counts.get(key, 0) + 1
        self.pending_counts[key] = count

        if count > self.MAX_PENDING_RETRIES:
            logger.warning(f"[RETRY LIMIT] Action {action_id} has been PENDING {count} times - forcing to ERROR state")
            return True  # Exceeded threshold

        logger.info(f"[RETRY TRACKING] Action {action_id} pending count: {count}/{self.MAX_PENDING_RETRIES}")
        return False  # Still under threshold

    def reset_count(self, user_prompt, action_id):
        """Reset counter when action completes or errors"""
        key = (user_prompt, action_id)
        if key in self.pending_counts:
            del self.pending_counts[key]
            logger.info(f"[RETRY TRACKING] Reset pending count for action {action_id}")


retry_tracker = ActionRetryTracker()


# Enforcement functions
def enforce_action_termination(user_prompt, current_action_id):
    """Ensure current action is TERMINATED before proceeding"""
    state = get_action_state(user_prompt, current_action_id)
    if state != ActionState.TERMINATED:
        raise StateTransitionError(
            f"Action {current_action_id} must be TERMINATED before proceeding (current: {state})")


def enforce_all_actions_terminated(user_prompt, total_actions):
    """Ensure all actions reached TERMINATED before flow completion"""
    for action_id in range(1, total_actions + 1):
        state = get_action_state(user_prompt, action_id)
        if state != ActionState.TERMINATED:
            return False, f"Action {action_id} not terminated (state: {state})"
    return True, "All actions terminated"

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

    # Perform transition (lock protects check-then-act on shared dict)
    with _state_lock:
        if user_prompt not in action_states:
            action_states[user_prompt] = {}
        action_states[user_prompt][action_id] = state
    logger.info(f"[TARGET] Action {action_id}: {current_state.value} → {state.value} ({reason})")

    # Auto-sync to ledger if registered
    _auto_sync_to_ledger(user_prompt, action_id, state)


# 3. ADD these wrapper functions for safe state updates:
def safe_set_state(user_prompt: str, action_id: int, new_state: ActionState, reason: str = ""):
    """Safely set state with error handling"""
    try:
        set_action_state(user_prompt, action_id, new_state, reason)
        return True
    except StateTransitionError as e:
        logger.error(f"[ERROR] {e}")
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
                logger.error(f"[ERROR] Auto-path failed at {step_state.value}: {e}")
                return False
        return True
    else:
        logger.error(f"[ERROR] No valid path from {current_state.value} to {target_state.value}")
        return False


# State tracking
action_states = {}  # {user_prompt: {action_id: current_state}}


def get_action_state(user_prompt: str, action_id: int) -> ActionState:
    """Get current state of an action."""
    with _state_lock:
        return action_states.get(user_prompt, {}).get(action_id, ActionState.ASSIGNED)


def validate_state_transition(user_prompt: str, action_id: int, new_state: ActionState) -> bool:
    """Validate state transitions follow the exact sequence"""
    current_state = get_action_state(user_prompt, action_id)

    valid_transitions = {
        ActionState.ASSIGNED: [ActionState.IN_PROGRESS, ActionState.ASSIGNED],
        ActionState.IN_PROGRESS: [ActionState.STATUS_VERIFICATION_REQUESTED, ActionState.IN_PROGRESS],
        ActionState.STATUS_VERIFICATION_REQUESTED: [ActionState.COMPLETED, ActionState.PENDING, ActionState.ERROR, ActionState.STATUS_VERIFICATION_REQUESTED],
        ActionState.COMPLETED: [ActionState.FALLBACK_REQUESTED, ActionState.RECIPE_REQUESTED, ActionState.TERMINATED, ActionState.COMPLETED],  # Allow direct recipe request (autonomous) or termination
        ActionState.PENDING: [ActionState.COMPLETED, ActionState.ERROR, ActionState.PENDING],
        # FIX: Allow ERROR to reach TERMINATED via FALLBACK_REQUESTED/RECIPE_REQUESTED or directly
        ActionState.ERROR: [ActionState.IN_PROGRESS, ActionState.PENDING, ActionState.ERROR, ActionState.FALLBACK_REQUESTED, ActionState.RECIPE_REQUESTED, ActionState.TERMINATED],
        ActionState.FALLBACK_REQUESTED: [ActionState.FALLBACK_RECEIVED, ActionState.FALLBACK_REQUESTED],
        ActionState.FALLBACK_RECEIVED: [ActionState.RECIPE_REQUESTED, ActionState.FALLBACK_RECEIVED],
        ActionState.RECIPE_REQUESTED: [ActionState.RECIPE_RECEIVED, ActionState.RECIPE_REQUESTED],
        ActionState.RECIPE_RECEIVED: [ActionState.TERMINATED, ActionState.RECIPE_RECEIVED],
        ActionState.TERMINATED: [ActionState.ASSIGNED]  # Final state but an entire actions can be updated and hence can go to assigned state again
    }

    allowed = valid_transitions.get(current_state, [])
    if new_state not in allowed:
        logger.error(f"[ERROR] Invalid transition: {current_state.value} → {new_state.value}")
        return False

    logger.info(f"[OK] Valid transition: {current_state.value} → {new_state.value}")
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

    current_state = get_action_state(user_prompt, current_action_id)

    if current_state not in [ActionState.ASSIGNED, ActionState.ERROR]:
        logger.info(f"[LOCKED] Action {current_action_id} in {current_state.value} - skipping assignment hook")
        return False

    # When ChatInstructor assigns action, move from ASSIGNED to IN_PROGRESS
    if (group_chat.messages and
        group_chat.messages[-1]['name'] == 'ChatInstructor' and f'Action {current_action_id}' in group_chat.messages[-1]['content']):

        if validate_state_transition(user_prompt, current_action_id, ActionState.IN_PROGRESS):
            safe_set_state(user_prompt, current_action_id, ActionState.IN_PROGRESS,"hook tracking lifecycle_hook_track_action_assignment")
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
            safe_set_state(user_prompt, current_action_id, ActionState.STATUS_VERIFICATION_REQUESTED,"hook tracking lifecycle_hook_track_status_verification_request")
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
        # Reset retry counter on completion
        retry_tracker.reset_count(user_prompt, current_action_id)
        if validate_state_transition(user_prompt, current_action_id, ActionState.COMPLETED):
            safe_set_state(user_prompt, current_action_id, ActionState.COMPLETED,"hook tracking lifecycle_hook_process_verifier_response")
            # Automatically request fallback after completion
            return {
                'action': 'force_fallback',
                'message': f"Action {current_action_id} fallback: ask user what actions should be taken if current actions fail in the future after you get the response from user give the conversation to StatusVerifier agent"
            }

    elif status == 'pending':
        # SAFETY NET: Check if pending count exceeded (prevents infinite retry loops)
        if retry_tracker.increment_pending(user_prompt, current_action_id):
            # Force transition to ERROR if pending too many times
            logger.error(f"[SAFETY NET] Action {current_action_id} exceeded max pending retries - forcing ERROR state")
            status = 'error'  # Override to error
            json_obj['message'] = f"Action failed after {retry_tracker.MAX_PENDING_RETRIES} retry attempts. Original message: {json_obj.get('message', 'No details')}"
            # Fall through to error handling below

        if status == 'pending':  # Still pending (not overridden)
            if validate_state_transition(user_prompt, current_action_id, ActionState.PENDING):
                safe_set_state(user_prompt, current_action_id, ActionState.PENDING,"hook tracking lifecycle_hook_process_verifier_response")
                return {
                    'action': 'force_completion',
                    'message': f"Complete pending steps for action {current_action_id} and ask @StatusVerifier to verify completion"
                }

    if status == 'error':  # Separated to allow fall-through from pending override
        # Reset retry counter on error (will start fresh if retried)
        retry_tracker.reset_count(user_prompt, current_action_id)
        if validate_state_transition(user_prompt, current_action_id, ActionState.ERROR):
            safe_set_state(user_prompt, current_action_id, ActionState.ERROR,"hook tracking lifecycle_hook_process_verifier_response")
            # FIX: Automatically request fallback for failed actions (like completed actions)
            # This allows ERROR to progress toward TERMINATED instead of getting stuck
            return {
                'action': 'force_fallback',
                'message': f"Action {current_action_id} failed: {json_obj.get('message', 'Unknown error')}. Please provide fallback actions for future failures of this type, then we'll create the recipe and move forward."
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
            safe_set_state(user_prompt, current_action_id, ActionState.FALLBACK_REQUESTED,"hook tracking lifecycle_hook_track_fallback_request")
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
            safe_set_state(user_prompt, current_action_id, ActionState.FALLBACK_RECEIVED,"hook tracking lifecycle_hook_track_user_fallback")
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
            safe_set_state(user_prompt, current_action_id, ActionState.RECIPE_REQUESTED,"hook tracking lifecycle_hook_track_recipe_request")
            return True

    return False


def lifecycle_hook_track_recipe_completion(user_prompt: str, json_obj: dict, user_tasks) -> dict:
    """10. Track when recipe is received and saved"""

    if not json_obj or 'status' not in json_obj or json_obj.get('status', '').lower() != 'done':
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
            safe_set_state(user_prompt, current_action_id, ActionState.RECIPE_RECEIVED,"hook tracking lifecycle_hook_track_recipe_completion")
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
            safe_set_state(user_prompt, current_action_id, ActionState.TERMINATED,"hook tracking lifecycle_hook_track_termination")
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



def lifecycle_hook_check_all_actions_terminated(user_prompt: str, user_tasks) -> dict:
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
    if current_action > total_actions:
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
        'message': "[OK] All validations passed - Agent creation ready"
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
        logger.info(f"[OK] Action {action_id}: COMPLETED one of the 4 flows")
    elif current_state == ActionState.ERROR:
        logger.info(f"[PROCESSING] Action {action_id}: In ERROR (Flow #2 or #3)")
    elif current_state == ActionState.PENDING:
        logger.info(f"[PROCESSING] Action {action_id}: In PENDING (Flow #3 or #4)")
    else:
        logger.info(f"[PROCESSING] Action {action_id}: In progress ({current_state.value})")


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
    logger.info(f"\n[STATUS] Lifecycle Status for {user_prompt}:")
    logger.info("-" * 50)

    for action_id, state in states.items():
        terminated = "[OK] TERMINATED" if state == ActionState.TERMINATED else "[PROCESSING] IN PROGRESS"
        logger.info(f"Action {action_id}: {state.value} {terminated}")


def initialize_deterministic_actions():
    """Initialize the state machine"""
    logger.info("[TARGET] Deterministic action lifecycle initialized")
    return True


def initialize_minimal_lifecycle_hooks():
    """Alias for initialization"""
    return initialize_deterministic_actions()


# =============================================================================
# LEDGER SYNC FUNCTIONS
# =============================================================================

def sync_action_state_to_ledger(
    user_prompt: str,
    action_id: int,
    state: ActionState,
    user_ledgers: Dict[str, Any]
) -> bool:
    """
    Sync ActionState changes to SmartLedger TaskStatus.

    This function should be called after every ActionState change to keep
    the ledger in sync. This ensures the ledger accurately reflects the
    current state of all actions.

    Args:
        user_prompt: The user_prompt key (e.g., "123_456")
        action_id: The action ID (1-based)
        state: The new ActionState
        user_ledgers: The global user_ledgers dictionary

    Returns:
        bool: True if sync was successful, False otherwise

    State Mapping:
        ActionState.ASSIGNED → LedgerTaskStatus.PENDING
        ActionState.IN_PROGRESS → LedgerTaskStatus.IN_PROGRESS
        ActionState.STATUS_VERIFICATION_REQUESTED → LedgerTaskStatus.IN_PROGRESS
        ActionState.COMPLETED → LedgerTaskStatus.COMPLETED
        ActionState.PENDING → LedgerTaskStatus.BLOCKED
        ActionState.ERROR → LedgerTaskStatus.FAILED
        ActionState.FALLBACK_REQUESTED → LedgerTaskStatus.PAUSED
        ActionState.FALLBACK_RECEIVED → LedgerTaskStatus.IN_PROGRESS
        ActionState.RECIPE_REQUESTED → LedgerTaskStatus.IN_PROGRESS
        ActionState.RECIPE_RECEIVED → LedgerTaskStatus.IN_PROGRESS
        ActionState.TERMINATED → LedgerTaskStatus.COMPLETED
    """
    if user_prompt not in user_ledgers:
        logger.debug(f"No ledger found for {user_prompt}, skipping sync")
        return False

    ledger = user_ledgers[user_prompt]
    task_id = f"action_{action_id}"

    if task_id not in ledger.tasks:
        logger.debug(f"Task {task_id} not found in ledger, skipping sync")
        return False

    try:
        LedgerTaskStatus = _get_ledger_task_status()

        # Map ActionState to LedgerTaskStatus
        STATE_MAP = {
            ActionState.ASSIGNED: LedgerTaskStatus.PENDING,
            ActionState.IN_PROGRESS: LedgerTaskStatus.IN_PROGRESS,
            ActionState.STATUS_VERIFICATION_REQUESTED: LedgerTaskStatus.IN_PROGRESS,
            ActionState.COMPLETED: LedgerTaskStatus.COMPLETED,
            ActionState.PENDING: LedgerTaskStatus.BLOCKED,
            ActionState.ERROR: LedgerTaskStatus.FAILED,
            ActionState.FALLBACK_REQUESTED: LedgerTaskStatus.PAUSED,
            ActionState.FALLBACK_RECEIVED: LedgerTaskStatus.IN_PROGRESS,
            ActionState.RECIPE_REQUESTED: LedgerTaskStatus.IN_PROGRESS,
            ActionState.RECIPE_RECEIVED: LedgerTaskStatus.IN_PROGRESS,
            ActionState.TERMINATED: LedgerTaskStatus.COMPLETED,
        }

        ledger_status = STATE_MAP.get(state)
        if ledger_status is None:
            logger.warning(f"No mapping for ActionState {state}")
            return False

        # Get current ledger status to avoid unnecessary updates
        current_ledger_status = ledger.tasks[task_id].status
        if current_ledger_status == ledger_status:
            return True  # Already in correct state

        # Update ledger
        ledger.update_task_status(
            task_id,
            ledger_status,
            reason=f"Synced from ActionState.{state.value}"
        )
        logger.debug(f"Synced {task_id}: ActionState.{state.value} → LedgerTaskStatus.{ledger_status.value}")
        return True

    except Exception as e:
        logger.error(f"Error syncing action state to ledger: {e}")
        return False


def sync_all_actions_to_ledger(user_prompt: str, user_ledgers: Dict[str, Any]) -> int:
    """
    Sync all current ActionStates to the ledger.

    Useful for bulk sync after recovery or initialization.

    Args:
        user_prompt: The user_prompt key
        user_ledgers: The global user_ledgers dictionary

    Returns:
        int: Number of actions successfully synced
    """
    if user_prompt not in action_states:
        return 0

    synced = 0
    for action_id, state in action_states[user_prompt].items():
        if sync_action_state_to_ledger(user_prompt, action_id, state, user_ledgers):
            synced += 1

    logger.info(f"Synced {synced} actions to ledger for {user_prompt}")
    return synced


def get_ledger_status_for_action(user_prompt: str, action_id: int, user_ledgers: Dict[str, Any]) -> Optional[str]:
    """
    Get the current ledger status for an action.

    Args:
        user_prompt: The user_prompt key
        action_id: The action ID
        user_ledgers: The global user_ledgers dictionary

    Returns:
        str: The current ledger status value, or None if not found
    """
    if user_prompt not in user_ledgers:
        return None

    ledger = user_ledgers[user_prompt]
    task_id = f"action_{action_id}"

    if task_id not in ledger.tasks:
        return None

    return ledger.tasks[task_id].status.value
