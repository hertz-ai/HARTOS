from typing import Dict, Tuple
import autogen
import os
import requests
import uuid
import time
from typing_extensions import Annotated
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


# Store user-specific agents and their chat history
user_agents: Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]] = {}


def execute_python_file(task_description:str,user_id: int):
    print('inside calling user agent at time')
    if user_id not in user_agents:
        print('user_id is not present')
    else:
        author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_id]
        current_time = datetime.now()
        text = f'This is the time now {current_time}\n you must perform this task {task_description}'
        agents_object['helper'].initiate_chat(recipient=manager, message=text, clear_history=False,silent=False)
        
        
    return 'done'


def create_agents_for_user(user_id: str) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
    """Create new assistant and user proxy agents for a user with basic configuration."""
    config_list = [{
        "model": 'hertzai-4o',
        "api_type": "azure",
        "api_key": '8f3cd49e1c3346128ba77d09ee9c824c',
        "base_url": 'https://hertzai-gpt4.openai.azure.com/',
        "api_version": "2024-02-15-preview"
    }]

    # Create a basic function calling config
    llm_config = {
        "config_list": config_list,
        "seed": 42
    }
    
    # Create the assistant agent with context awareness
    assistant = autogen.AssistantAgent(
        name=f"assistant_{user_id}",
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message='''You are a Helpful Assistant follow below action's to help user
        
        Actions: ["initiate the conversation by create an image on some topic on your own", "send the image to user", "wait for user understanding about the image", "evaluate the response", "return the actual response of what the flash card is about"]
        time based action: ["everyday at 10:10 a give a image of a dragon to user"]
        '''
    )

    # Create the user proxy agent
    user_proxy = autogen.UserProxyAgent(
        name=f"user_proxy_{user_id}",
        human_input_mode="NEVER",
        llm_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    helper = autogen.AssistantAgent(
        name="helper",
        llm_config=llm_config,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message="""Help the assistant agent to complete the task""",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )

    @helper.register_for_execution()
    @assistant.register_for_llm(api_style="function",description="Text to image Creator")
    def txt2img(text: Annotated[str, "Text to create image"]) -> str:
        print('INSIDE TXT2IMG')
        url = f"http://aws_rasa.hertzai.com:5459/txt2img?prompt={text}"

        payload = ""
        headers = {}

        response = requests.post(url, headers=headers, data=payload)
        return response.json()['img_url']
    
    @helper.register_for_execution()
    @assistant.register_for_llm(api_style="function",description="Creates time-based tasks using APScheduler to schedule tasks")
    def create_scheduled_tasks(
        cron_expression: Annotated[str, "Cron expression for scheduling"],
        task_description: Annotated[str, "Description of the task to be performed"],
        user_id: Annotated[int, "User ID"] = 5) -> str:
        """
        Creates time-based tasks using APScheduler to schedule tasks
        
        Args:
            cron_expression: When to run the task (e.g., "0 9 * * *" for daily at 9 AM)
            task_description: What the AutoGen agents should discuss/accomplish
            user_id: Identifier for the user creating the task
        
        Returns:
            str: Success message or error details
        """

        print(f'Creating scheduled task for user: {user_id}')
        if not scheduler.running:
            scheduler.start()
        job_id = str(uuid.uuid4())
        
        try:
            trigger = CronTrigger.from_crontab(cron_expression)
            job_id = f"job_{int(time.time())}"
            scheduler.add_job(execute_python_file, trigger=trigger, id=job_id,args=[task_description,user_id])
            print('Successfully created scheduler job')
            return 'Successfully created scheduler job'
        except Exception as e:
            return f"Error creating scheduled task: {str(e)}"
        
      
    
    @helper.register_for_execution()
    @assistant.register_for_llm(api_style="function",description="Send some text to 3rd person")
    def contact_parent(text: Annotated[str, "Text to send to person"],person: Annotated[str, "whom to send text"]) -> str:
        print('INSIDE contact_parent')
        print(f'send this text to {person} {text}')
        return 'contacted parent successfully'
    

    
    assistant.description = 'Agent that is designed to do some specific tasks'
    user_proxy.description = 'agent will act as user and perform task assigned to user'
    helper.description = 'helps assistant agent to call functions'
    
    
    def state_transition(last_speaker, groupchat):
        if last_speaker == user_proxy:
            return assistant
        if 'TERMINATE' in messages[-1]["content"].upper():
            print('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        return "auto"
        
    group_chat = autogen.GroupChat(
        agents=[assistant, helper, user_proxy],
        messages=[],
        max_round=15,
        # speaker_selection_method="auto",  # using an LLM to decide
        speaker_selection_method=state_transition,  # using an LLM to decide
        allow_repeat_speaker=True,  # Prevent same agent speaking twice
        send_introductions=True
    )
    
    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"config_list": config_list}
    )
    
    
    

    return assistant, user_proxy, group_chat, manager, helper

def get_agent_response(assistant: autogen.AssistantAgent, user_proxy: autogen.UserProxyAgent,manager: autogen.GroupChatManager,group_chat:autogen.GroupChat, message: str) -> str:
    """Get a single response from the agent for the given message."""
    try:

        response = user_proxy.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)
        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = group_chat.messages[-2]
        return last_message

    except Exception as e:
        print(f'Got some error {e}')
        return f"Error getting response: {str(e)}"


def chat_agent(user_id,text,prompt_id):
    try:
        use_recipe = True

        # Get or create agents for this user
        if user_id not in user_agents:
            user_agents[user_id] = create_agents_for_user(user_id,prompt_id)

        assistant, user_proxy, group_chat, manager, helper = user_agents[user_id]
        prompt_id = int(prompt_id)
    
        response = get_agent_response(assistant, user_proxy,manager,group_chat, text)

        # Get chat history length for debugging
        history_length = len(user_proxy.chat_messages.get(assistant.name, []))

        return response
    except Exception as e:
        print(f'Some ERROR IN REUSE RECIPE {e}')
        raise
