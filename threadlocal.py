import threading


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


thread_local_data = ThreadLocalData()

