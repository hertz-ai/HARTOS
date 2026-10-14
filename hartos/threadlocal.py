import logging
import threading

_log = logging.getLogger(__name__)


class ThreadLocalData:
    def __init__(self) -> None:
        self._local = threading.local()

    def set_request_id(self,request_id):
        self._local.request_id = request_id
        
    def get_request_id(self):
        return getattr(self._local, 'request_id', None)

    def set_user_id(self, user_id):
        self._local.user_id = user_id
        
    def get_user_id(self):
        return getattr(self._local, 'user_id', None)
    
    def set_reqid_list(self, new_data):
        self._local.data = new_data
        
    def get_reqid_list(self):
        return getattr(self._local, 'data', [])
    
    def set_req_token_count(self, value):
        self._local.req_token_count = value
        
    def update_req_token_count(self, new_value):
        self._local.req_token_count += new_value
        
    def get_req_token_count(self):
        return getattr(self._local, 'req_token_count', None)
    
    def set_res_token_count(self, value):
        self._local.res_token_count = value
        
    def update_res_token_count(self, new_value):
        self._local.res_token_count += new_value
        
    def get_res_token_count(self):
        return getattr(self._local, 'res_token_count', None)
    
    def set_recognize_intents(self):
        self._local.recognize_intent = []
    
    def update_recognize_intents(self, new_intent):
        if not hasattr(self._local, 'recognize_intent'):
            self._local.recognize_intent = []
        self._local.recognize_intent.append(new_intent)
        
    def get_recognize_intents(self):
        return getattr(self._local, 'recognize_intent', None)
    
    def set_global_intent(self, global_intent):
        self._local.global_intent = global_intent
        
    def get_global_intent(self):
        return getattr(self._local, 'global_intent', None)
    
    def set_prompt_id(self, prompt_id):
        self._local.prompt_id = prompt_id

    def get_prompt_id(self):
        return getattr(self._local, 'prompt_id', None)

    # --- Handing this thread's request state to a worker acting for it ---
    # A worker thread starts with an EMPTY threading.local, so work moved
    # onto one stops seeing the request's prompt_id, user_id, request_id and
    # activity run -- and the shell tool's consent check reads prompt_id.
    # local_loop runs one computer-use action on a worker (so its time budget
    # can bound it) and uses this pair to keep that action inside its run.

    def snapshot(self):
        """This thread's per-request state, as a dict to hand to a worker.

        A DEEP copy, per value: a worker that appends to an adopted list or
        dict changes its own copy, never the caller's (review F6,
        2026-09-27 -- the shallow copy let a worker's append land in the
        caller's recognize_intent list).  A value that cannot be copied is
        passed by reference and said so in the log, rather than dropped.
        """
        import copy
        out = {}
        for key, value in vars(self._local).items():
            try:
                out[key] = copy.deepcopy(value)
            except Exception as e:
                _log.warning("threadlocal snapshot: %r shared by reference, "
                             "not copied (%s: %s)", key, type(e).__name__, e)
                out[key] = value
        return out

    def adopt(self, snapshot):
        """Take on a snapshot() from the thread this one is acting for.

        MERGE, not replace: each key in ``snapshot`` is set on this thread;
        keys this thread already has and the snapshot lacks are kept.
        Values are NOT copied again, so whoever holds ``snapshot`` shares
        those objects with this thread.  That is deliberate: the VLM loop
        keeps the snapshot it handed an action's worker and marks the
        worker's ``activity_run`` closed when it abandons the action.
        Nothing flows back: the caller never sees what the worker sets.
        """
        for key, value in (snapshot or {}).items():
            setattr(self._local, key, value)

    def carry(self, fn):
        """``fn``, wrapped to run on another thread as THIS thread's request.

        The one way to hand work to a worker: snapshot() now, adopt() on the
        worker before ``fn`` runs.  The wrapper's ``.snapshot`` is the dict
        the worker adopts (shared, per adopt()), so a caller can still mark
        state it hands over, as the VLM loop does to close an abandoned
        action's run.  Callers: integrations.vlm.local_loop (one computer-use
        action), integrations.agentic_router (the plan's LLM calls, which ran
        with no user, prompt or request id until 2026-09-27).
        """
        snap = self.snapshot()

        def _carried(*args, **kwargs):
            self.adopt(snap)
            return fn(*args, **kwargs)

        _carried.snapshot = snap
        return _carried

    def turn_of(self, prompt_id):
        """Context manager: run a block as agent ``prompt_id``'s turn, then
        give the thread back its own agent.

        For a request /chat hands to ANOTHER agent (autonomous routing to an
        existing agent that matches, hart_intelligence_entry): the thread
        still carried the request's own prompt_id, so a tool acting for "the
        calling agent" (cast_experiment_vote) acted as the wrong one.  Only
        prompt_id is swapped and restored (via adopt()): whatever the turn
        sets for the handler to read afterwards (ui_actions, creation flags)
        is kept, and user_id is the same person either way.
        """
        import contextlib

        @contextlib.contextmanager
        def _cm():
            saved = {'prompt_id': self.get_prompt_id()}
            self.set_prompt_id(prompt_id)
            try:
                yield
            finally:
                self.adopt(saved)
        return _cm()

    def detached(self):
        """Context manager: run a block with NO request state on this thread,
        then put this thread's state back exactly as it was.

        For work that is no request's turn but runs on a thread a request
        used.  The /chat handler sets this state and never clears it, so a
        reused worker thread still carries the last chat's prompt_id,
        user_id, request_id, user_role, activity run and model override;
        measured 2026-09-27, an MCP tool saw them (mcp_http_bridge._invoke_
        tool is the caller).  Inside the block every getter answers its
        default.  The saved values are put back by reference, not copied,
        so an object the thread shares (a run the VLM loop may close) stays
        the same object.
        """
        import contextlib

        @contextlib.contextmanager
        def _cm():
            saved = dict(vars(self._local))
            for key in saved:
                delattr(self._local, key)
            try:
                yield
            finally:
                for key in list(vars(self._local)):
                    delattr(self._local, key)
                for key, value in saved.items():
                    setattr(self._local, key, value)
        return _cm()

    # --- Where the reply being published came from (local | hive | cloud) ---
    # hart_intelligence_entry.publish_async stamps served_by on every chat
    # envelope but the voice (a 'TTS' payload carries none).  The one
    # site that KNOWS the backend (the dispatcher, once an expert answers) names
    # it here around its delivery.  A context and not a keyword on purpose:
    # Nunba rebinds hart_intelligence.publish_async with a wrapper that takes
    # (topic, message, timeout) only, and safe_hartos_attr hands the dispatcher
    # that wrapper, so a new keyword is a TypeError on the desktop and the
    # expert's reply is never published.

    def get_served_by(self):
        """The origin the enclosing reply_from() block names, or None."""
        return getattr(self._local, 'served_by', None)

    def reply_from(self, served_by):
        """Context manager: replies published inside the block are served by
        ``served_by`` (any tag core.constants.canonical_served_by accepts),
        then this thread gets its previous origin back."""
        import contextlib

        @contextlib.contextmanager
        def _cm():
            saved = self.get_served_by()
            self._local.served_by = served_by
            try:
                yield
            finally:
                self._local.served_by = saved
        return _cm()

    # --- Computer-use run context (set by integrations.vlm.local_loop) ---
    # The run a desktop action belongs to, so a tool that executes DURING a
    # run can announce itself as a step of that run instead of inventing its
    # own.  The shell tool is the case that needs it: it reaches
    # hart_intelligence_entry._handle_shell_command_tool on the loop's OWN
    # thread (local_loop already relies on that for prompt_id), but the run id
    # was a bare local in run_local_agentic_loop, so the shell step could only
    # write the ribbon and never reached the computer_use.update topic.

    def set_activity_run(self, run_id, user_id=None, prompt_id=None):
        self._local.activity_run = {
            'run_id': run_id, 'user_id': user_id, 'prompt_id': prompt_id,
        } if run_id else None

    def get_activity_run(self):
        """The enclosing run's {run_id, user_id, prompt_id}, or None.

        May also carry ``closed: True``: the run ended while work adopted
        from it was still running (integrations.vlm.activity_stream.
        close_run_stamp), so that work must not announce steps into it.
        """
        return getattr(self._local, 'activity_run', None)

    def clear_activity_run(self):
        self._local.activity_run = None

    # --- The run's workspace (set by integrations.vlm.local_loop) ---
    # The folder a computer-use run works in: its declared task workspace
    # (vlm_adapter.resolve_task_workspace).  A file action's relative path
    # resolves here and every shell step of the run runs here, so a script
    # one step writes is the script the next step runs.  Until 2026-10-05
    # the workspace was only a sentence in the model's prompt: relative
    # paths resolved in the process cwd and shell steps ran there (the
    # install folder on the desktop), so the prompt's own route for a
    # refused `python -c` -- write_file the script, then shell it -- could
    # not find the file it had just written.  A context and not a keyword,
    # as prompt_id is: the shell tool is reached through safe_hartos_attr
    # with the command alone, on the run's thread or on an action worker
    # that carry() hands this state to.

    def get_workspace(self):
        """The enclosing run's workspace folder, or None outside a run."""
        return getattr(self._local, 'workspace', None)

    def workspace(self, path):
        """Context manager: the block is a run working in ``path``, then the
        thread gets its previous workspace back.  An empty ``path`` names no
        workspace, so the enclosing one, if any, stands."""
        import contextlib

        @contextlib.contextmanager
        def _cm():
            saved = self.get_workspace()
            if path and str(path).strip():
                self._local.workspace = str(path).strip()
            try:
                yield
            finally:
                self._local.workspace = saved
        return _cm()

    # --- Agent creation signals (set by LangChain Create_Agent tool) ---

    def set_creation_requested(self, description=None, autonomous=False):
        """Signal that the LLM decided the user wants to create an agent."""
        self._local.creation_requested = True
        self._local.creation_description = description
        self._local.creation_autonomous = autonomous

    def get_creation_requested(self):
        return getattr(self._local, 'creation_requested', False)

    def get_creation_description(self):
        return getattr(self._local, 'creation_description', None)

    def get_creation_autonomous(self):
        return getattr(self._local, 'creation_autonomous', False)

    def clear_creation_flags(self):
        self._local.creation_requested = False
        self._local.creation_description = None
        self._local.creation_autonomous = False

    # --- Per-request model config override (speculative execution) ---

    def set_model_config_override(self, config_list):
        """Override the global config_list for this thread/request only."""
        self._local.model_config_override = config_list

    def get_model_config_override(self):
        """Get per-request model config override, or None to use global."""
        return getattr(self._local, 'model_config_override', None)

    def clear_model_config_override(self):
        self._local.model_config_override = None

    # --- Agentic routing signals (set by LangChain Agentic_Router tool) ---

    def set_agentic_routing(self, task_description=None, plan_steps=None, matched_agent_id=None):
        """Signal that LangChain detected an agentic task needing autogen."""
        self._local.agentic_requested = True
        self._local.agentic_task_description = task_description
        self._local.agentic_plan_steps = plan_steps or []
        self._local.agentic_matched_agent_id = matched_agent_id

    def get_agentic_requested(self):
        return getattr(self._local, 'agentic_requested', False)

    def get_agentic_task_description(self):
        return getattr(self._local, 'agentic_task_description', None)

    def get_agentic_plan_steps(self):
        return getattr(self._local, 'agentic_plan_steps', [])

    def get_agentic_matched_agent_id(self):
        return getattr(self._local, 'agentic_matched_agent_id', None)

    def clear_agentic_flags(self):
        self._local.agentic_requested = False
        self._local.agentic_task_description = None
        self._local.agentic_plan_steps = []
        self._local.agentic_matched_agent_id = None

    # --- Per-request task source (own | hive | idle) ---

    def set_task_source(self, source: str):
        """Set the task source for this thread/request (own, hive, idle)."""
        self._local.task_source = source

    def get_task_source(self) -> str:
        """Get per-request task source, defaults to 'own'."""
        return getattr(self._local, 'task_source', 'own')

    def clear_task_source(self):
        self._local.task_source = 'own'

    # --- Per-request originating channel context (Discord/Telegram/Slack/…) ---
    # The inbound message's origin {channel, sender_id, chat_id, …}, set by the
    # /chat handler from the request body and read by the agent's channel tools so
    # a reply can be routed/tailored to the channel the message came from.  MUST
    # be thread-local (per-request) — two concurrent inbound messages have
    # different origins.  (It was previously assigned as a bare attribute on this
    # module-level singleton, which is SHARED across threads, so concurrent
    # requests clobbered each other's channel; see set_/get_request_id for the
    # canonical _local-backed pattern every other per-request field follows.)

    def set_client_user_agent(self, user_agent):
        """The User-Agent of the request this thread serves ('' when none):
        what a tool reads to tell a phone from a desktop."""
        self._local.client_user_agent = user_agent or ''

    def get_client_user_agent(self):
        return getattr(self._local, 'client_user_agent', '')

    def set_channel_context(self, channel_context):
        self._local.channel_context = channel_context

    def get_channel_context(self):
        return getattr(self._local, 'channel_context', None)

    def clear_channel_context(self):
        self._local.channel_context = None

    # --- User role (central | regional | flat | guest) — used by the
    #     ui_actions page registry to filter admin-only destinations ---

    def set_user_role(self, role: str):
        self._local.user_role = role

    def get_user_role(self) -> str:
        return getattr(self._local, 'user_role', 'flat')

    # --- LiquidUI action bar signals (set by the Navigate_App tool) ---
    # The Flask /chat handler reads these after the LangChain call
    # returns and attaches them to the response JSON so the frontend
    # LiquidActionBar renders route chips without any extra round trip.

    def set_ui_actions(self, actions: list):
        """Attach a list of ui_action dicts to the current request."""
        self._local.ui_actions = list(actions or [])

    def get_ui_actions(self) -> list:
        return getattr(self._local, 'ui_actions', [])

    def clear_ui_actions(self):
        self._local.ui_actions = []


thread_local_data = ThreadLocalData()

