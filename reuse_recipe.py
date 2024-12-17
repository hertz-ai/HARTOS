from typing import Dict, Tuple
import autogen
import os
import requests
import uuid
import time
from typing_extensions import Annotated
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import json
import mimetypes

# Store user-specific agents and their chat history
user_agents: Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]] = {}

scheduler = BackgroundScheduler()
scheduler.start()
agents_session = {}

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


def create_agents_for_user(user_id: str,prompt_id) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
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
    
    personas = []
    try:
        with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
            personas = config['number_of_persona']
    except Exception as e:
        print(e)
    if len(personas)>0: # and also check if we have record in db/agents_session to reuser
        temp = personas.copy()
        temp.append([])
        
        agent_prompt = f'''You are a Helpful Assistant follow below action's to help user
        initiate the conversation by asking user which persona they belong to among the available personas: {personas} // give user the persona names and ask to select one
        Actions: {recipes[prompt_id]['steps']}
        '''
    else:     
        # Create the assistant agent with context awareness
        agent_prompt = f'''You are a Helpful Assistant follow below action's to help user
            
            Actions: {recipes[prompt_id]['steps']}
            '''
    assistant = autogen.AssistantAgent(
        name=f"assistant_{user_id}",
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message=agent_prompt
    )
    
    print(f'creating agent with propt {agent_prompt}')

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
    @assistant.register_for_llm(api_style="function",description="Image to Text")
    def img2txt(image_url: Annotated[str, "image url of which you want text"],text: Annotated[str, "the details you want from image"]='Describe the Images and Text data in this image in detail') -> str:
        print('INSIDE img2txt')
        url = "http://azure_all_vms.hertzai.com:6066/image_inference"

        payload = {
            'url': image_url,
            'prompt': text
        }
        files = []
        headers = {}

        response = requests.request(
            "POST", url, headers=headers, data=payload, files=files, timeout=300)
        if response.status_code == 200:
            return response.text
        else:
            return 'Not able to get this page details try later'
    
    @helper.register_for_execution()
    @assistant.register_for_llm(api_style="function",description="Send some text to 3rd person")
    def contact_parent(text: Annotated[str, "Text to send to person"],person: Annotated[str, "whom to send text"]) -> str:
        print('INSIDE contact_parent')
        print(f'send this text to {person} {text}')
        return 'contacted parent successfully'
    
    @helper.register_for_execution()
    @assistant.register_for_llm(api_style="function", description="Creates time-based jobs using APScheduler to schedule jobs")
    def create_scheduled_jobs(cron_expression: Annotated[str, "Cron expression for scheduling"], 
                            job_description: Annotated[str, "Description of the job to be performed"],
                            user_id: Annotated[int, "User ID"] = 5) -> str:
        print('INSIDE create_scheduled_jobs')
        if not scheduler.running:
            scheduler.start()
        
        try:
            trigger = CronTrigger.from_crontab(cron_expression)
            job_id = f"job_{int(time.time())}"
            scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, user_id])
            print('Successfully created scheduler job')
            return 'Successfully created scheduler job'
        except Exception as e:
            print(f'Error in create_scheduled_jobs: {str(e)}')
            return f"Error creating scheduled job: {str(e)}"

    @helper.register_for_execution()
    @assistant.register_for_llm(api_style="function",description="Upload a file and generate a downloadable URL. Accepts any file type (images, documents, etc.) and returns the download URL.")
    def upload_file(file_path: Annotated[str, "Full path to the file you want to upload"]) -> str:
        try:
            # Validate file exists
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"File not found: {file_path}")
            # Generate unique request ID
            request_id = str(uuid.uuid4())
            file_name = os.path.basename(file_path)
            mime_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
            # Prepare upload request
            url = "https://azurekong.hertzai.com:8443/makeit/upload_file"
            payload = {
                'request_id': request_id,
                'agent': True
            }
            with open(file_path, 'rb') as file:
                files = [
                    ('file', (file_name, file, mime_type))
                ]
                response = requests.post(url, data=payload, files=files) 
                response.raise_for_status()
                return response.json().get('file_url', 'URL not provided in response')
                
        except FileNotFoundError as e:
            raise e
        except requests.exceptions.RequestException as e:
            raise Exception(f"Upload failed: {str(e)}")
        except Exception as e:
            raise Exception(f"Unexpected error during upload: {str(e)}")
    

    
    assistant.description = 'Agent that is designed to do some specific tasks'
    user_proxy.description = 'agent will act as user and perform task assigned to user'
    helper.description = 'helps assistant agent to call functions'
    
    
    def state_transition(last_speaker, groupchat):
        messages = groupchat.messages
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


recent_file_id = {}
recipes = {}
def chat_agent(user_id,text,prompt_id,file_id):
    try:
        use_recipe = True
        if file_id:
            recent_file_id[user_id] = file_id

        # Get or create agents for this user
        if user_id not in user_agents:
            
            with open(f"prompts/{prompt_id}_recipe.json", 'r') as f:
                config = json.load(f)
                try:
                    if 'scheduled_tasks' in config and len(config['scheduled_tasks'])>0:
                        print('Creating scheduled tasks')
                        trigger = CronTrigger.from_crontab(config['scheduled_tasks'][0]['cron_expression'])
                        job_id = f"job_{int(time.time())}"
                        scheduler.add_job(execute_python_file, trigger=trigger, id=job_id,args=[config['scheduled_tasks'][0]['job_description'],user_id])
                        print('Successfully created scheduler job')
                except:
                    print('Some Error in creating scheduled tasks')
                recipes[prompt_id] = config
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
