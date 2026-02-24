# PR: Fix Agent Creation Workflow - Error Handling & State Management

## Summary
Fix critical bugs preventing agent creation completion when actions fail. Implements proper error handling with lifecycle state management and retry loop prevention.

## Problem
Agent creation workflows got stuck in infinite retry loops when actions failed (e.g., API errors, timeouts), preventing completion. Failed actions had no path to TERMINATED state.

## Solution
**Three-layer fix:**
1. **Lifecycle State Management** - Added ERROR → FALLBACK → TERMINATED transitions
2. **StatusVerifier Error Detection** - Enhanced prompt to recognize permanent failures (HTTP 403/404/500, timeouts)
3. **Retry Tracker Safety Net** - Forces ERROR state after 3 pending attempts (prevents infinite loops)

## Changes

### 1. `lifecycle_hooks.py`
**Added ActionRetryTracker class (lines 51-83)**
- Tracks pending retry attempts per action
- Max 3 retries before forcing ERROR state
- Prevents infinite retry loops

**Updated lifecycle_hook_process_verifier_response (lines 310-355)**
- Integrated retry tracking
- Forces ERROR state when threshold exceeded
- Added `[RETRY TRACKING]` and `[SAFETY NET]` logging

**Updated state transitions (line 197)**
```python
ActionState.ERROR: [
    ActionState.IN_PROGRESS,
    ActionState.PENDING,
    ActionState.ERROR,
    ActionState.FALLBACK_REQUESTED,  # NEW
    ActionState.RECIPE_REQUESTED,    # NEW
    ActionState.TERMINATED           # NEW
]
```

**Changed error handling (lines 300-308)**
```python
# Before: 'force_retry' (infinite loops)
# After: 'force_fallback' (request fallback → proceed)
return {
    'action': 'force_fallback',
    'message': f"Action failed... Please provide fallback actions..."
}
```

### 2. `create_recipe.py`
**Enhanced StatusVerifier prompt (lines 1672-1691)**

Added CRITICAL error detection rules:
- HTTP 403/404/500/401 → report "error" (not "pending")
- Connection timeouts → report "error"
- Tool responses with `"status": "error"` → report "error"
- Repeated failures (2+ times) → report "error"
- Clear distinction between retryable vs permanent failures

## Impact

### Before
```
Action fails → StatusVerifier: "pending" → Retry → Fails → "pending" → Infinite loop ♾️
No path from ERROR to TERMINATED → Workflow stuck
```

### After
```
Action fails → StatusVerifier: "error" → ERROR state → Request fallback → Complete ✅
OR
Action fails → "pending" → Retry 3x → Safety net: Force ERROR → Complete ✅

ERROR → FALLBACK → RECIPE → TERMINATED → Workflow completes
```

## Benefits
- ✅ Workflows complete successfully even with failed actions
- ✅ No more infinite retry loops (max 3 attempts)
- ✅ Clear error handling and user feedback
- ✅ Better observability with detailed logging
- ✅ Backward compatible (no breaking changes)

## Testing
- Tested with weather_bot_test agent (API failures)
- Verified tool errors (HTTP 403, timeouts) are detected
- Server stable throughout testing
- All previous functionality preserved

## Files Modified
- `lifecycle_hooks.py` (~80 lines added/modified)
- `create_recipe.py` (~20 lines modified)

**Total**: ~100 lines across 2 files

## Logs to Monitor
```bash
# Verify retry tracking
grep "RETRY TRACKING" logs
grep "SAFETY NET" logs

# Verify state transitions
grep "Action.*ERROR" logs
grep "FALLBACK_REQUESTED" logs
grep "TERMINATED" logs
```

## Risk Assessment
**Low Risk**
- All changes are additive (no deletions)
- Backward compatible with existing workflows
- Two-layer defense (primary + safety net)
- Comprehensive logging for debugging

## Related Issues
- Fixes agent creation blocking on failed actions
- Resolves infinite retry loops
- Enables graceful error handling in Review Mode

---

**Review**: Focus on lifecycle_hooks.py state transitions and retry logic
**Deploy**: No special steps needed, restart server to apply
**Monitor**: Check `[RETRY TRACKING]` and `[SAFETY NET]` logs post-deploy
