import autogen
from typing import Dict, Tuple
import os
from typing import Annotated, Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import requests
import uuid
from datetime import datetime
import time
from typing_extensions import Literal
from autogen.coding import DockerCommandLineCodeExecutor
import re
from autogen import register_function
import json
from autogen import ConversableAgent
from flask import current_app

from crossbarhttp import Client
client = Client('http://aws_rasa.hertzai.com:8088/publish')

scheduler = BackgroundScheduler()
scheduler.start()

user_agents: Dict[str, Tuple[autogen.ConversableAgent, autogen.ConversableAgent]] = {}
config_list = [{
        "model": "gpt-4o-mini",
        "api_type": "azure",
        "api_key": "8f3cd49e1c3346128ba77d09ee9c824c",
        "base_url": "https://hertzai-gpt4.openai.azure.com/",
        "api_version": "2024-02-15-preview"
    }]

agent_data = {}



def send_message_to_user(user_id,response,inp):
    url = 'http://aws_rasa.hertzai.com:9890/autogen_response'
    body = json.dumps({'user_id':user_id,'message':response,'inp':inp})
    headers = {'Content-Type': 'application/json'}
    res = requests.post(url,data=body,headers=headers)
    


def execute_python_file(task_description:str,user_id: int):
    import requests
    import json
    headers = {'Content-Type': 'application/json'}
    url = 'http://localhost:6777/time_agent'
    data = json.dumps({'task_description':task_description,'user_id':user_id})
    res = requests.post(url,data=data,headers=headers)
    return 'done'

def time_based_execution(task_description:str,user_id: int):
    current_app.logger.info('INSIDE TIME_BASED_EXECUTION')
    if user_id not in user_agents:
        current_app.logger.info('user_id is not present')
    else:
        
        assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user = user_agents[user_id]
        # author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_id]
        current_time = datetime.now()
        text = f'This is the time now {current_time}\n you must perform this task {task_description}'
        time_user.initiate_chat(time_agent,message=text)
        key = list(time_user.chat_messages.keys())[0]
        last_message = time_user.chat_messages[key][-1]['content'].replace('TERMINATE','')
        
        #sending response to receiver agent
        send_message_to_user(user_id,last_message,task_description)
    return 'done'
        


class Action:
    def __init__(self,actions):
        self.actions = actions
        self.current_action = 0
        self.fallback = False
        self.new_json = []
        self.recipe = False
    
    def get_action(self,current_action):
        return self.actions[current_action]

def create_agents(user_id: str,task,prompt_id) -> Tuple[autogen.ConversableAgent, autogen.ConversableAgent]:
    """Create new assistant & user agents for a given user_id"""
    
    llm_config = {
        "cache_seed": None,
        "config_list": [{
        "model": 'hertzai-4o',
        "api_type": "azure",
        "api_key": '8f3cd49e1c3346128ba77d09ee9c824c',
        "base_url": 'https://hertzai-gpt4.openai.azure.com/',
        "api_version": "2024-02-15-preview"
    }],
    }
    
    custom_agents = []
    agents_object = {}
    with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
            list_of_persona = [x['name'] for x in config['number_of_persona']]
            current_app.logger.info(f'Got list of persona as {list_of_persona}')
    # Create assistant agent
    # Create assistant agent
    assistant = autogen.AssistantAgent(
        name="Assistant",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message="""•Purpose: The assistant executes actions provided by the ChatInstructor, seeks help from Helper and Executor agents when necessary, and ensures actions are completed accurately.
        •Action Flow:
            1. Receive Action: Ask the user to associate the action with a persona (if multiple personas exist).
            2. Execution:
                ➜Perform the action with the help of Helper and Executor agents.
                ➜If the action requires code execution, delegate it to the Executor agent.
            3. After Completion:
                ➜If successful, ask the user what to do if the action fails in the future. Pass the response to the StatusVerifier agent.
                ➜If no error, ask the StatusVerifier to confirm completion and include the persona name.
                ➜After confirmation, request the next action from the ChatInstructor.
            4. If Failed:
                ➜Create a summary of the error and ask the user for help if needed.
                ➜Never assume; always seek user assistance for unresolved issues.
            5. Action Modifications:
                ➜If the action is modified, ask the user what measures should be taken if it fails in the future.
        
        •Persona Association:
            list of persona:- """+f'{list_of_persona}'+"""
            Rules: 
                ➜If there’s only 1 persona in the list, associate that persona with all actions automatically.
                ➜If there are multiple personas, ask the user to select the persona associated with each action.
        
        •Code Execution: Executor Agent: Executes code as needed.
        
        •Tools Helper Agent can use:
            1. The tools are: text_2_image, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_from_memory and save_data_in_memory.
            2. Create Scheduled Jobs: For tasks involving timers or scheduled jobs, ask Helper agent to use the create_scheduled_jobs tool.
            3. Data/Memory Management:
                ➜If you want to save some data ask helper agent to use save_data_in_memory tool.
                ➜If you wnat to get some data ask helper agent to use get_data_from_memory tool.
        
        •Error Handling:
            If there's an error or failure, respond with a structured error message format: {"status":"error","action":"current action","action_id":1,"message":"message here"}
            For success, ask the status verifier agent to verify the status of completion for current action
        
        •Calling Other Agents:
            When you need to direct a question or route the conversation to a specific agent, use the @ tag followed by the agent's name. Examples include: @Executor or @Helper or @User
        
        •Communication Style:
            1. Speak casually, with clarity and respect. Maintain accuracy and clear communication.
            2. If needed, use a more formal tone if the user prefers.
        
        •Special Note: Avoid using time.sleep() in code. For scheduled tasks, always use the create_scheduled_jobs tool instead.

        •Working Directory: /home/hertzai2019/newauto/coding/

        •Reminder: If camera input is needed, ask the user to turn on their camera. All responses should be played via TTS with a talking-head animation.
        """+f"Extra Information: below are the list of actions the chat_manager is gonna give you keep this in mind but dont use this directly\n{user_tasks[user_id].actions}",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    helper = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message="""You are an Helper Agent,
        Focus: Assisting the Assistant Agent to complete actions.
        Note: Do not coordinate with other agents. After your response, always pass the conversation back to the Assistant Agent.
        Coding Instructions:
            Avoid using time.sleep in code.
            Instead, use the create_scheduled_jobs tool for tasks requiring timed intervals.
            If the Assistant Agent requests code with time.sleep, respond that it cannot be executed and utilize the create_scheduled_jobs tool instead.
            Always include proper error handling and logging.
            Ensure the final response is printed before returning it.
        Data Management:
            Use the get_set_internal_memory tool to store or retrieve user information as needed.""",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    verify = autogen.AssistantAgent(
        name="StatusVerifier",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=""""You are an Status verification agent.
        Role: Track and verify the status of actions. Provide updates strictly in JSON format.
        Response formats:
            1. Action Completed Successfully: {"status": "completed","action": "current action","action_id": 1,"message": "message here","persona_name":"persona name this action belongs to","fallback_action": "fallback action here"  // If fallback_action is missing, ask the user: "What measures should be taken if this action fails in the future?" Include their response in fallback_action.
            2. Action Error: {"status": "error","action": "current action","action_id": 1,"message": "message here"}
            3. Action Updated: {"status": "updated","action": "current action","updated_action": "updated action","action_id": 1,"message": "message here","persona_name":"persona name this action belongs to","fallback_action": "","entire_actions":[all actions in array format]} // If no fallback_action is provided, ask the user for measures to include.
        Important Instructions:
            Only mark an action as "Completed" if the Assistant Agent confirms successful completion.
            For pending tasks or ongoing actions, respond with: "Please try again."
            Always ensure a fallback_action is included for completed actions.
            Verify and report status only—do not perform actions.
            Maintain the exact JSON structure in all responses.
        """+f"\nExtra Information: below are the list of actions the chat_manager is gonna give you keep this in mind but dont use this directly only use this is there is any update in any action & you want to update the actions & return the entire array as entire_actions\n{user_tasks[user_id].actions}",
        
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    author = autogen.UserProxyAgent(
        name="User",
        human_input_mode="NEVER",
        is_termination_msg=lambda x: True if x.get("content").strip()=='' else False,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    
    executor = autogen.AssistantAgent(
        name="Executor",
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        llm_config=llm_config,
        system_message="""You are an Executor agent. 
        Focus: Creating, running, and debugging code.
        
        Responsibilities:
            1. Code Execution:
                Execute code provided by the Assistant Agent.
                Report execution results, errors, or output.
            2. Error Management:
                Identify issues if errors occur.
                Propose and implement fixes.
                Report back to the Assistant with clear details.
            3. Key Notes:
                Working Directory: /home/hertzai2019/newauto/coding. Use this path as needed.
                For storing or retrieving information about the user, request the Helper Agent to use the get_set_internal_memory tool.
                No General Conversations: Redirect unrelated conversations to the manager to route to the user.
        
        Coding Instructions:
            Avoid using time.sleep. Instead, request the Helper Agent to use the create_scheduled_jobs tool for tasks requiring delays or intervals.
            If the Assistant Agent provides code requiring time.sleep, inform them that it cannot be executed and suggest using the create_scheduled_jobs tool.
            Add proper error handling and logging in all code.
            Ensure the final response is printed before returning it.
        Calling Other Agents:
            When you need to direct a question or route the conversation to a specific agent, use the @ tag followed by the agent's name. Examples include: @Executor or @Helper or @User
        Things You cannot do but Helper Agent can:
            1. Tools Helper Agent can use: Can use tools like text_2_image, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_from_memory and save_data_in_memory.
            2. Create Scheduled Jobs: For tasks involving timers or scheduled jobs, ask Helper agent to use the create_scheduled_jobs tool.
            3. Data/Memory Management:
                ➜If you want to save some data ask helper agent to use save_data_in_memory tool.
                ➜If you wnat to get some data ask helper agent to use get_data_from_memory tool."""
    )
    
    chat_instructor = autogen.UserProxyAgent(
        name="ChatInstructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    agents_object['assistant'] = assistant
    agents_object['helper'] = helper
    agents_object['author'] = author
    agents_object['user'] = author
    agents_object['executor'] = executor
    agents_object['verify'] = verify
    agents_object['chat_instructor'] = chat_instructor
    
    for i in config['number_of_persona']:
        name = i['name']
        name = autogen.UserProxyAgent(
            name=i['name'],
            human_input_mode="NEVER",
            default_auto_reply="TERMINATE",
            is_termination_msg=lambda x: True if x.get("content").strip()=='' else False,
            max_consecutive_auto_reply=0,
            code_execution_config=False,
        )
        name.description = i['description']
        custom_agents.append(name)
        agents_object[i['name']] = name
    
    def txt2img(text: Annotated[str, "Text to create image"]) -> str:
        current_app.logger.info('INSIDE txt2img')
        url = f"http://aws_rasa.hertzai.com:5459/txt2img?prompt={text}"

        payload = ""
        headers = {}

        response = requests.post(url, headers=headers, data=payload)
        return response.json()['img_url']
    
    helper.register_for_llm(name="text_2_image", description="Text to image Creator")(txt2img)
    executor.register_for_execution(name="text_2_image")(txt2img)
    
    def save_data_in_memory(key: Annotated[str, "Key for storing/retrieving data"],
                    value: Annotated[Optional[str], "Value you want to store"] = None) -> str:
        current_app.logger.info('INSIDE save_data_in_memory')
        agent_data[prompt_id][key] = value
        return f'{agent_data[prompt_id]}'

    
    helper.register_for_llm(name="save_data_in_memory", description="Use this to Store and retrieve data using key-value storage system")(save_data_in_memory)
    executor.register_for_execution(name="save_data_in_memory")(save_data_in_memory)
    
    def get_all_data() -> str:
        return f'{agent_data[prompt_id]}'

    
    helper.register_for_llm(name="get_data_from_memory", description="Returns all data from the internal Memory")(get_all_data)
    executor.register_for_execution(name="get_data_from_memory")(get_all_data)
    
    def get_user_id() -> str:
        current_app.logger.info('INSIDE get_user_id')
        return f'{user_id}'

    
    helper.register_for_llm(name="get_user_id", description="Returns the unique identifier (user_id) of the current user.")(get_user_id)
    executor.register_for_execution(name="get_user_id")(get_user_id)
    
    def get_prompt_id() -> str:
        current_app.logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'

    
    helper.register_for_llm(name="get_prompt_id", description="Returns the unique identifier (prompt_id) associated with the current prompt or conversation.")(get_prompt_id)
    executor.register_for_execution(name="get_prompt_id")(get_prompt_id)
    
    
    def Generate_video(text: Annotated[str, "Text you want to create video"],
                       avatar_id: Annotated[str, "one avatar_id"]) -> str:
        current_app.logger.info('INSIDE video_gen')
        database_url = 'https://mailer.hertzai.com'
        makeittalk_url = 'http://azurekong.hertzai.com:8000/makeitLoad'
        final_res = []
    
        print(f"avtar_id: {avatar_id}:\n{text[:10]}....\n")
        
        headers = {'Content-Type': 'application/json'}
        data = {}
        data["text"] = text
        data['flag_hallo'] = 'false'
        data['chattts'] = True
        res = requests.get("https://mailer.hertzai.com/get_image_by_id/{}".format(avatar_id))
        res = res.json()
        new_image_url = res["image_url"]
        data["cartoon_image"] = "True"
        data["bg_url"] = 'http://stream.mcgroce.com/txt/examples_cartoon/roy_bg.jpg'
        data['vtoonify'] = "false"
        data["image_url"] = new_image_url
        data['im_crop'] = "false"
        data['remove_bg'] = "false"
        data['hd_video'] = "false"
        data['uid'] = 'somethingrandom here'
        data['gradient'] = "true"
        data['cus_bg'] = "false"
        data['solid_color'] = "false"
        data['inpainting'] = "false"
        data['prompt'] = ""
        data['gender'] = 'male'
        if res['voice_id'] != None:
            voice_sample = requests.get(
                "{}/get_voice_sample_id/{}".format(database_url, res['voice_id']))
            voice_sample = voice_sample.json()
        else:
            voice_sample = None
        if voice_sample is None:
            current_app.logger.info('voice sample is none using default')
            voice_sample = requests.get(
                "{}/get_voice_sample/{}".format(database_url, user_id))
            voice_sample = voice_sample.json()
        data["audio_sample_url"] = voice_sample["voice_sample_url"]
        video_link = requests.post("{}/video-gen/".format(makeittalk_url),
                                    data=json.dumps(data), headers=headers, timeout=60)
        current_app.logger.info(" *********   video link  ********* %s", video_link)
        video_link = json.loads(video_link.content)
        video_link['image_url'] = new_image_url
        video_link['text'] = text
        video_link['avatar_id'] = avatar_id
        video_link['bg_url'] = 'http://stream.mcgroce.com/txt/examples_cartoon/roy_bg.jpg'
        final_res.append(video_link)
        headers = {'Content-Type': 'application/json'}
        data = {
            "conv_id": None,
            "teacher_avatar_id": avatar_id,
            "voice_id": None,
            "text": text,
            "audio_url": voice_sample["voice_sample_url"],
            "reference_txt": video_link['reference_txt'],
            "warper_txt": video_link['warper_txt'],
            "triangulation_txt": video_link['triangulation_txt'],
            "image_url": new_image_url
        }
        res = requests.post("{}/toonify-generated-video".format(database_url),
                            data=json.dumps(data), headers=headers).json()
        
        return f"Video Generation completed and saved Successfully"

    
    helper.register_for_llm(name="Generate_video", description="Generate video with text and save it in database")(Generate_video)
    executor.register_for_execution(name="Generate_video")(Generate_video)
    
    def recent_files() -> str:
        current_app.logger.info('INSIDE get_user_uploaded_file')
        if recent_file_id[user_id]:
            return f'Got user uploaded file the file_id is {recent_file_id[user_id]}'

        return 'No file uploaded from user'
    
    helper.register_for_llm(name="get_user_uploaded_file", description="get user's recent uploaded files")(recent_files)
    executor.register_for_execution(name="get_user_uploaded_file")(recent_files)
    

    def img2txt(image_url: Annotated[str, "image url of which you want text"],text: Annotated[str, "the details you want from image"]='Describe the Images & Text data in this image in detail') -> str:
        current_app.logger.info('INSIDE img2txt')
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
    
    helper.register_for_llm(name="get_text_from_image", description="Image to Text")(img2txt)
    executor.register_for_execution(name="get_text_from_image")(img2txt)
    
    def create_scheduled_jobs(cron_expression: Annotated[str, "Cron expression for scheduling"], 
                            job_description: Annotated[str, "Description of the job to be performed"],
                            user_id: Annotated[int, "User ID"] = 5) -> str:
        current_app.logger.info('INSIDE create_scheduled_jobs')
        if not scheduler.running:
            scheduler.start()
        
        try:
            trigger = CronTrigger.from_crontab(cron_expression)
            job_id = f"job_{int(time.time())}"
            scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, user_id])
            current_app.logger.info('Successfully created scheduler job')
            return 'Successfully created scheduler job'
        except Exception as e:
            current_app.logger.error(f'Error in create_scheduled_jobs: {str(e)}')
            return f"Error creating scheduled job: {str(e)}"
        
    helper.register_for_llm(name="create_scheduled_jobs", description="Creates time-based jobs using APScheduler to schedule jobs")(create_scheduled_jobs)
    executor.register_for_execution(name="create_scheduled_jobs")(create_scheduled_jobs)
    
    assistant.description = 'this is an assistant agent that coordinates & executes requested tasks & actions'
    executor.description = 'this is an executor agent that Specialized agent for code execution & response handling'
    author.description = 'this is an author/user agent that focused on user support, error resolution, contextual information. Contact this agent when you need any user based information or if you want to say something to user'
    chat_instructor.description = 'this is a ChatInstructor agent that provides step-by-step action plans for task execution'
    helper.description = 'this is a helper agent that facilitates task completion & assists other agents'
    verify.description = 'this is a verify status agent. which will verify the status of current action that will be called after ChatInstructor gives instruction to complete an action & assistant completes it, this agent will provide updates in a structured JSON format & then call user agent'
    
    def state_transition(last_speaker, groupchat):
        current_app.logger.info('Inside state_transition')
        messages = groupchat.messages
        current_app.logger.info(f'Inside state_transition with message {messages[-1]["content"][:10]}.. & last_speaker:{last_speaker.name}')
        crossbar_message = {"text": [messages[-1]["content"]], "priority": 99, "action": 'Agent', "historical_request_id": [], "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
        'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0}, 'bottom_left': {'x': 0, 'y': 0}}}
        result = client.publish(
            f"com.hertzai.hevolve.chat.{user_id}", crossbar_message)
        if not messages[-1]["content"].startswith('Reflect on the sequence'):
            try:
                json_obj = eval(messages[-1]["content"])
                current_app.logger.info(f'got json object')
            except:
                json_obj = None
                # current_app.logger.info('it is not a json object')
                pass
            if not json_obj:
                try:
                    json_match = re.search(r'{[\s\S]*}', messages[-1]["content"])
                    if json_match:    
                        current_app.logger.info(f'got json object')
                        json_part = json_match.group(0)
                        json_obj = json.loads(json_part)
                except:
                    pass
            if json_obj:
                try:   
                    if json_obj['status'].lower() == 'error':
                        return author
                    elif json_obj['status'].lower() == 'completed' or json_obj['status'].lower() == 'success':
                        if 'recipe' in json_obj.keys():
                            current_app.logger.info('Recipe created successfully')
                            name = f'prompts/{prompt_id}_recipe.json'
                            with open(name, "w") as json_file:
                                json.dump(json_obj, json_file)
                            current_app.logger.info(f"Dictionary saved to {name}")
                        if 'action_id' in json_obj.keys():
                            user_tasks[user_id].actions[json_obj['action_id']-1] = json_obj['action']
                            user_tasks[user_id].new_json.append(json_obj)
                        return chat_instructor
                    elif json_obj['status'].lower() == 'updated':
                        if 'entire_actions' in json_obj.keys() and type(json_obj['entire_actions'])==list:
                            entire_actions = json_obj['entire_actions']
                            user_tasks[user_id].actions = entire_actions
                            user_tasks[user_id].current_action = 0
                            user_tasks[user_id].fallback = False
                            user_tasks[user_id].recipe = False
                        elif 'action_id' in json_obj.keys():
                            user_tasks[user_id].actions[json_obj['action_id']-1] = json_obj['updated_action']
                            user_tasks[user_id].new_json.append(json_obj)
                    elif json_obj['status'].lower() == 'done':
                        current_app.logger.info('Got Individual action recipe save it')
                        name = f'prompts/{prompt_id}_{json_obj["action_id"]}.json'
                        with open(name, "w") as json_file:
                            json.dump(json_obj, json_file)
                        current_app.logger.info(f'Saved Individual recipe at: {name}')
                        
                        return chat_instructor   
                except:
                    pass
            
        pattern = r"@Helper"
        pattern1 = r"@Executor"
        try:
            if re.search(pattern, messages[-1]["content"]):
                current_app.logger.info("String contains @Helper returnng helper")
                messages[-1]["content"] = messages[-1]["content"].replace('@user','')
                return helper
            if re.search(pattern1, messages[-1]["content"]):
                current_app.logger.info("String contains @Executor returnng executor")
                return executor
        except Exception as e:
            current_app.logger.error(f'Got error when searching for @user in last message :{e}')
            
        #checking if message is routed to user via tag
        pattern = r"@user"
        pattern1 = r"@StatusVerifier"
        try:
            if re.search(pattern, messages[-1]["content"]):
                current_app.logger.info("String contains @user returnng author")
                messages[-1]["content"] = messages[-1]["content"].replace('@user','')
                return author
            if re.search(pattern1, messages[-1]["content"]):
                current_app.logger.info("String contains @StatusVerifier returnng StatusVerifier")
                return verify
        except Exception as e:
            current_app.logger.error(f'Got error when searching for @user in last message :{e}')
            
        if last_speaker.name == 'Executor' or last_speaker.name == 'User' or last_speaker.name == 'user' or last_speaker.name == 'ChatInstructor':
            current_app.logger.info('Got last speaker as executor or helper or author or chat_instructor & reutrning next speaker as assistant')
            return assistant
        json_obj = None
        
        if last_speaker == verify:
            current_app.logger.info('Got last speaker as verify_status & returning next speaker as chat_instructor')
            return chat_instructor
        try:
            if messages[-1]["content"] == '':
                current_app.logger.info(f'Got content as blank {messages[-1]}')
                return assistant
            if 'exitcode:' in messages[-1]["content"]:
                current_app.logger.info('Got exitcode in text returning assistant')
                return assistant
        except Exception as e:
            current_app.logger.error(f'Got error when content as blank with error as :{e}')
        

        
        if 'TERMINATE' in messages[-1]["content"].upper():
            current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        else:
            return 'auto'
    
    
    all_agents = [assistant, executor, author, chat_instructor,helper,verify]
    all_agents.extend(custom_agents)
    group_chat = autogen.GroupChat(
        agents=all_agents,
        messages=[],
        max_round=20,
        # select_speaker_message_template='''You manage a team that Completes a list of Actions provided by ChatInstructor Agent.
        # The Agents available in the team are: Assistant, Helper, Executor, ChatInstructor, StatusVerifier & User''',
        # select_speaker_prompt_template=f"Read the above conversation, select the next person from [Assistant, Helper, Executor, ChatInstructor, StatusVerifier & User] & only return the role as agent.",
        # speaker_selection_method="auto",  # using an LLM to decide
        speaker_selection_method=state_transition,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=True
    )
    
    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"config_list": config_list,"cache_seed": None}
    )
    
    
    
    return author, assistant, executor, group_chat, manager, chat_instructor, agents_object

user_tasks = {}

def get_response_group(user_id,text,prompt_id,Failure=False,error=None):
    
    # Get or create agents for this user
    if user_id not in user_agents:
        author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = create_agents(user_id,user_tasks[user_id],prompt_id)
        user_agents[user_id] = (author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object)
        messages[user_id] = []
    else:
        author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_id]
    
    if Failure:
        text = f"The last action you tried failed with error message: {error}\n please try again"

    if len(messages[user_id])>0:
        # last_agent, last_message = manager.resume(messages=messages[user_id])
        try:
            agents_object['user'].initiate_chat(recipient=manager, message=text, clear_history=False,silent=False)
        except Exception as e:
            current_app.logger.error(f'Got some error it can be multiple tools called at one error:{e}')
            agents_object['helper'].initiate_chat(recipient=manager, message='hey', clear_history=False,silent=False)
            
    else:
        message = user_tasks[user_id].get_action(user_tasks[user_id].current_action)
        user_tasks[user_id].fallback = not user_tasks[user_id].fallback
        message = f'Action {user_tasks[user_id].current_action+1}: {message} '
        crossbar_message = {"text": ["Working on "+message+".\n please evaluate the response i am giving to check if it meets the current action"], "priority": 99, "action": 'Agent', "historical_request_id": [], "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
        'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0}, 'bottom_left': {'x': 0, 'y': 0}}}
        result = client.publish(
            f"com.hertzai.hevolve.chat.{user_id}", crossbar_message)
        chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
        
    while True:
        current_app.logger.info('inside while')
        if group_chat.messages[-1]['name'] == 'ChatInstructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
            current_app.logger.info(f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content'][:10]}..")
            try:
                json_obj = eval(group_chat.messages[-2]["content"])
                current_app.logger.info(f'got json object {json_obj}')
            except:
                try:
                    json_match = re.search(r'{[\s\S]*}', group_chat.messages[-2]["content"])
                    if json_match:
                        current_app.logger.info(f'got json object {json_obj}')
                        json_part = json_match.group(0)
                        json_obj = json.loads(json_part)
                    else:
                        raise 'No json found'
                except Exception as e:
                    current_app.logger.error(f'it is not a json object the error is: {e}')
                    current_app.logger.info('it is not a json object You should ask status verifier to give response in proper format & not move ahead to next action')
                    message = 'Hey @StatusVerifier Agent, Please verify the status of the action performed and Respond in the following format {"status": "status here","action": "current action","action_id": 1,"message": "message here","fallback_action": "fallback action here"}'
                    assistant_agent.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                    continue
            current_app.logger.info('resuming chat')
            if user_tasks[user_id].current_action==len(user_tasks[user_id].actions):
                user_tasks[user_id].new_json.append(json_obj)
                user_tasks[user_id].current_action += 1
                name = f'prompts/{prompt_id}_new.json'
                with open(name, "w") as json_file:
                    json.dump(user_tasks[user_id].new_json, json_file)
                current_app.logger.info('updating updated action in .json')
                # details['flows'][0]['actions'] = user_tasks[user_id].actions
                # name = f'prompts/{prompt_id}.json'
                # with open(name, "w") as json_file:
                #     json.dump(details, json_file)
                current_app.logger.info(f'user_tasks[user_id].current_action:{user_tasks[user_id].current_action} == len(user_tasks[user_id].actions)')
                message = '''Reflect on the sequence of tasks and create a comprehensive recipe that includes all necessary steps along with a suitable name. Provide the output in the following JSON format:
                { "status": "completed", "steps": [ { "action": "Describe the action performed here", "action_id": 1, "persona": "Specify the persona responsible for this action", "fallback_action": "Provide the fallback action to take if this step fails"} ], "recipe": "Include the final recipe with all steps and generalized functions here", "generalized_functions": [], "scheduled_tasks": [ { "cron_expression": "", "job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }
                Recipe Requirements:
                    1. Generalized Python Functions: Suggest well-documented and reusable Python functions to handle similar tasks for coding steps in the future.
                    2. Avoid directly storing any specific information provided by the author in the recipe. Use placeholders for variables instead.
                    3. Ensure that coding and non-coding steps are not combined within the same function.
                    4. For all Python functions, include comprehensive docstrings to explain their purpose, parameters, and usage. This should especially clarify non-coding steps that require utilizing the assistant's language capabilities.
                    5. If any internal tool is used to complete a step, provide detailed instructions on how to call or utilize that tool instead of providing the code for that step.'''
                chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
            else:
                user_tasks[user_id].current_action = json_obj['action_id']
                current_app.logger.info(f'current action {user_tasks[user_id].current_action} & fallback {user_tasks[user_id].fallback} & recipe {user_tasks[user_id].recipe}')
                user_tasks[user_id].new_json.append(json_obj)
                message = user_tasks[user_id].get_action(user_tasks[user_id].current_action)
                current_app.logger.info('checking for fallback and recipe')
                if user_tasks[user_id].fallback == True:
                    user_tasks[user_id].recipe = True
                    message = f" Action {user_tasks[user_id].current_action} fallback:ask user what actions should be taken if current actions fail in the future after you get the response from user give the conversaation to StatusVerifier agent"      
                elif user_tasks[user_id].recipe == True:
                    user_tasks[user_id].recipe = False
                    user_tasks[user_id].fallback = True
                    message = '''Reflect on the sequence of tasks and create a comprehensive recipe that includes all necessary steps along with a suitable name. Provide the output in the following JSON format:
                    { "status": "done", "action": "Describe the action performed here", "action_id": 1, "recipe": "Include the recipe with all steps, tools and generalized functions to perform this action successfully", "generalized_functions": [], "scheduled_tasks": [ { "cron_expression": "", "job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }
                    Recipe Requirements:
                        1. Generalized Python Functions: Suggest well-documented and reusable Python functions to handle similar tasks for coding steps in the future.
                        2. Avoid directly storing any specific information provided by the author in the recipe. Use placeholders for variables instead.
                        3. Ensure that coding and non-coding steps are not combined within the same function.
                        4. For all Python functions, include comprehensive docstrings to explain their purpose, parameters, and usage. This should especially clarify non-coding steps that require utilizing the assistant's language capabilities.
                        5. If any internal tool is used to complete a step, provide detailed instructions on how to call or utilize that tool instead of providing the code for that step.'''
                    # message = f" Action {user_tasks[user_id].current_action} fallback:ask user what actions should be taken if current actions fail in the future after you get the response from user give the conversaation to StatusVerifier agent"
                else:
                    user_tasks[user_id].current_action = user_tasks[user_id].current_action+1
                    message = f'Action {user_tasks[user_id].current_action}: {message} '
                    crossbar_message = {"text": ["Working on "+message], "priority": 99, "action": 'Agent', "historical_request_id": [], "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
                    'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0}, 'bottom_left': {'x': 0, 'y': 0}}}
                    result = client.publish(
                        f"com.hertzai.hevolve.chat.{user_id}", crossbar_message)
                user_tasks[user_id].fallback = not user_tasks[user_id].fallback
                
                chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                
        else:
            break
            
        if user_tasks[user_id].current_action >len(user_tasks[user_id].actions):
            current_app.logger.info(f'current action {user_tasks[user_id].current_action} is greater than legth {len(user_tasks[user_id].actions)}')
            break            

    messages[user_id] = group_chat.messages
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]
    return last_message['content']

messages = {}
recent_file_id = {}

def recipe(user_id, text,prompt_id,file_id):
    current_app.logger.info('--'*100)
    if file_id:
            recent_file_id[user_id] = file_id

    if user_id not in user_tasks.keys():
        with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
        user_tasks[user_id] = Action(config['flows'][0]['actions'])
        agent_data[prompt_id] = {'user_id':user_id}
    try:
        
        last_response = get_response_group(user_id,text,prompt_id)

    except Exception as e:
        current_app.logger.error(f"Error occurred in create Recipe: {str(e)}")  # Add logging for debugging
        last_response = get_response_group(user_id,text,prompt_id,True,e)
        
    try:
        json_response = eval(last_response)
        if 'status' in json_response.keys(): 
            if 'recipe' in json_response.keys():
                url = f'https://mailer.hertzai.com/update_agent_prompt?prompt_id={prompt_id}'
                headers = {'Content-Type': 'application/json'}
                res = requests.patch(url,headers=headers)
                return 'Agent Created Successfully'
            else:
                return json_response['message']
        
    except:
        pass
    return last_response
    

