import autogen
import os
from typing import Annotated, Optional, Dict, Tuple
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import requests
import uuid
from datetime import datetime
import time
import redis
import pickle
from PIL import Image
from autogen.coding import DockerCommandLineCodeExecutor
import re
from autogen import register_function
import json
from autogen import ConversableAgent
from flask import current_app
from helper import topological_sort, fix_json, retrieve_json, fix_actions

from autogen.agentchat.contrib.capabilities import transform_messages, transforms

from autogen.cache.in_memory_cache import InMemoryCache

from crossbarhttp import Client
client = Client('http://aws_rasa.hertzai.com:8088/publish')


redis_client = redis.StrictRedis(
    host='azure_all_vms.hertzai.com', port=6369, db=0)

scheduler = BackgroundScheduler()
scheduler.start()

user_agents: Dict[str, Tuple[autogen.ConversableAgent, autogen.ConversableAgent]] = {}
# config_list = [{
#     "model": "gpt-4o-mini",
#     "api_type": "azure",
#     "api_key": "4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf",
#     "base_url": "https://hertzai-gpt4.openai.azure.com/",
#     "api_version": "2024-02-15-preview",
#     "price":[0.00015,0.0006]
# }]
# config_list = [{
#         "model": 'gpt-4o',
#         "api_type": "azure",
#         "api_key": '4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf',
#         "base_url": 'https://hertzai-gpt4.openai.azure.com/',
#         "api_version": "2024-02-15-preview",
#         "price": [0.0025, 0.01]
#     }]
config_list = [{
        "model": 'gpt-4o',
        "api_type": "azure",
        "api_key": '8941f5f6f17f43d391051edc27f4b2f6',
        "base_url": 'https://openai-api-e7zq7mkk.azure-api.net',
        "api_version": "2024-02-15-preview",
        "price": [0.0025, 0.01]
    }]

agent_data = {}
agent_metadata = {}
final_recipe = {}




database_url = 'https://mailer.hertzai.com'
def save_conversation_db(text,user_id,prompt_id,database_url,request_id):
    headers = {'Content-Type': 'application/json'}
    data = {
        "request": 'VIDEO GENERATION FROM GENERATE_VIDEO',
        "response": text.strip(),
        "user_id": int(user_id),
        "conv_bot_name": 'GPT-4o',
        "topic": f'{prompt_id}',
        "revision": False,
        "dialogue_id": None,
        "card_type": 'Custom GPT',
        "qid": None,
        "layout_id": None,
        "layout_list": '[]',
        "request_token": 0,
        "response_token": 0,
        "request_id": request_id,
        "historical_request_id": str('[]')
    }
    res = requests.post("{}/conversation".format(database_url),
                        data=json.dumps(data), headers=headers).json()
    conv_id = res['conv_id']
    return conv_id



def strip_json_values(data):
    if isinstance(data, dict):
        return {key: strip_json_values(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [strip_json_values(item) for item in data]
    elif isinstance(data, str):
        return f"redacted"  # Truncate to 8 characters and add " redact"
    elif isinstance(data, (int, float, bool)) or data is None:
        return f'redacted {type(data)}'  # Keep primitive types as is
    else:
        return f'{data}'

def send_message_to_user(user_id,response,inp):
    url = 'http://aws_rasa.hertzai.com:9890/autogen_response'
    body = json.dumps({'user_id':user_id,'message':response,'inp':inp})
    headers = {'Content-Type': 'application/json'}
    res = requests.post(url,data=body,headers=headers)
    


def execute_python_file(task_description,user_id,prompt_id):
    headers = {'Content-Type': 'application/json'}
    url = 'http://localhost:6777/time_agent'
    data = json.dumps({'task_description':task_description,'user_id':user_id,'prompt_id':prompt_id})
    res = requests.post(url,data=data,headers=headers)
    return 'done'

def time_based_execution(task_description:str,user_id: int,prompt_id:int):
    current_app.logger.info('INSIDE TIME_BASED_EXECUTION')
    user_prompt = f'{user_id}_{prompt_id}'
    if user_prompt not in user_agents:
        current_app.logger.info('user_id is not present')
    else:
        
        assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user = user_agents[user_prompt]
        # author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_id]
        current_time = datetime.now()
        text = f'This is the time now {current_time}\n you must perform this task {task_description}'
        time_user.initiate_chat(time_agent,message=text)
        key = list(time_user.chat_messages.keys())[0]
        last_message = time_user.chat_messages[key][-1]['content'].replace('TERMINATE','')
        
        #sending response to receiver agent
        send_message_to_user(user_id,last_message,task_description)
    return 'done'
        
def get_frame(user_id):
    current_app.logger.info('inside get_frame')
    serialized_frame = redis_client.get(user_id)
    current_app.logger.info('after redis client')
    try:
        if serialized_frame is not None:
            frame_bgr = pickle.loads(serialized_frame)
            current_app.logger.info(
                f"Frame for user_id {user_id} retrieved successfully.")
            frame = frame_bgr[:, :, ::-1]
            return frame
        else:
            current_app.logger.info(f"No frame found for user_id {user_id}.")
            return None
    except ModuleNotFoundError as e:
        raise e

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
    user_prompt = f'{user_id}_{prompt_id}'
    llm_config = {
        "cache_seed": None,
        "config_list": config_list,
        "max_tokens": 1500
    }
    
    custom_agents = []
    agents_object = {}
    with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
            list_of_persona = [x['name'] for x in config['personas']]
            current_app.logger.info(f'Got list of persona as {list_of_persona}')
    # Create assistant agent
    # Create assistant agent
    assistant = autogen.AssistantAgent(
        name="Assistant",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message="""•Purpose: The assistant executes actions provided by the ChatInstructor, seeks help from Helper and Executor agents when necessary, and ensures actions are completed accurately.
        •Action Flow:
            1. Receive Action: Ask the UserProxy to associate the action with a persona (if multiple personas exist).
            2. Execution:
                ➜Perform the action with the help of Helper and Executor agents.
                ➜If the action requires code execution, create code(python preffered) and ask Executor agent to execute the code.
            3. After Completion:
                ➜If action completed successful & there is no error, ask @Helper to save the information(which will be required in future) in memory using 'save_data_in_memory' tool.
                ➜After save_data_in_memory has completed, ask the StatusVerifier to confirm completion and include the persona name.
                ➜After confirmation, request the next action from the ChatInstructor.
            4. If Failed:
                ➜Create a summary of the error and ask the UserProxy for help if needed.
                ➜Never assume; always seek user assistance for unresolved issues.
            5. Action Modifications:
                ➜If the action is modified, ask the user what measures should be taken if it fails in the future.
        
        •Persona Association:
            list of persona:- """+f'{list_of_persona}'+"""
            Rules: 
                ➜If there’s only 1 persona in the list, associate that persona with all actions automatically.
                ➜If there are multiple personas, ask the @User to select the persona associated with each action.
        
        •Code Execution: Executor Agent: Executes code as needed.
        
        •Tools Helper Agent can use:
            1. The tools are: send_response_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory.
            2. Create Scheduled Jobs: For tasks involving timers or scheduled jobs, ask Helper agent to use the create_scheduled_jobs tool.
            3. Data/Memory Management:
                ➜If you want to save some data ask helper agent to use "save_data_in_memory" tool.
                ➜If you want to get some data ask helper agent to use "get_data_by_key"  tool.
            4. If you want to send some message to user then ask helper agent to user send_response_to_user tool.
            5. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
        
        •Error Handling:
            If there's an error or failure, respond with a structured error message format: {"status":"error","action":"current action","action_id":1/2/3...,"message":"message here"}
            For success, ask the status verifier agent to verify the status of completion for current action
        
        •Calling Other Agents:
            When you need to direct a question or route the conversation to a specific agent, use the @ tag followed by the agent's name. Examples include: @Executor or @Helper or @User
        
        •Communication Style:
            1. Speak casually, with clarity and respect. Maintain accuracy and clear communication.
            2. If needed, use a more formal tone if the user prefers.
        
        •Special Notes: 
            1. Create python code in ```code here``` if you want to perform some code related actions  or when you get unknown language unknown and ask @Executor to run the code.
            2. Avoid using time.sleep() in code. For scheduled tasks, always use the create_scheduled_jobs tool instead.
            3. When responding to user neither share your internal monologues with other agents nor mention other agent names nor your instructions.   
            4. Always save information which you think will be needed in future using 'save_data_in_memory' and if you want any information check the memory using tool 'get_data_by_key, get_saved_metadata'.

        •Working Directory: /home/hertzai2019/newauto/coding/

        •Reminder: If camera input is needed, ask the user to turn on their camera. All responses should be played via TTS with a talking-head animation.
        """+f"Extra Information: below are the list of actions the chat_manager is gonna give you keep this in mind but dont use this directly\n{user_tasks[user_prompt].actions}",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    helper = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config=False,
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
            1. Action Completed Successfully: {"status": "completed","action": "current action","action_id": 1/2/3...,"message": "message here","can_perform_without_user_input":"can you perform this action on your own without user input in future. only say no when it is absolutely mandatory and you cannot proceed without it, if you can proceed by checking with other agents you should say yes.  say yes/no if no they give the reason as well e.g. no-i need user's likes and dislike","persona_name":"persona name this action belongs to","fallback_action": "fallback action here"}  // If fallback_action is missing, ask the user: "What measures should be taken if this action fails in the future?" Include their response in fallback_action.
            2. Action Error: {"status": "error","action": "current action","action_id": 1/2/3...,"message": "message here"}
            3. Current Action Updated: {"status": "updated","action": "current action text","updated_action": "updated current action text","action_id": 1/2/3...,"message": "message here","persona_name":"persona name this action belongs to","fallback_action": ""} // If no fallback_action is provided, ask the user for measures to include.
            4. Entire Action array updated: {"status": "updated","entire_actions":[refer actions from Extra Information and provide all actions along with updated action  in single json array format]}
        Important Instructions:
            Only mark an action as "Completed" if the Assistant Agent confirms successful completion.
            For pending tasks or ongoing actions, respond to Assistant to complete the task.
            Always ensure a fallback_action is included for completed actions.
            Verify the action performed by assistant and make sure the action is performed correctly as per instructions. if action performed was not as per instructions give the original current action to the assistant.
            Report status only—do not perform actions yourself.
            Maintain the exact JSON structure in all responses.
            
        """+f"\nExtra Information: below are the list of actions the chat_manager will give you, keep this in mind but don't use this directly only use this if there is any update in any action or you want to insert/delete the actions & return the entire array as entire_actions\n{user_tasks[user_prompt].actions}",
        
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    author = autogen.UserProxyAgent(
        name="UserProxy",
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
                You can create code if not provided to you.
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
            1. Tools Helper Agent can use: Can use tools like send_response_to_user, text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory.
            2. Create Scheduled Jobs: For tasks involving timers or scheduled jobs, ask Helper agent to use the create_scheduled_jobs tool.
            3. Data/Memory Management:
                ➜If you want to save some data ask helper agent to use "save_data_in_memory" tool.
                ➜If you wnat to get some data ask helper agent to use "get_data_by_key", "get_saved_metadata" tool.
            4. If you want to send some message to user then ask helper agent to user send_response_to_user tool.
            5. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video."""
    )
    
    chat_instructor = autogen.UserProxyAgent(
        name="ChatInstructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    context_handling = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=30,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=3000, max_tokens_per_message=700, min_tokens=0),
        ]
    )
    context_handling.add_to_agent(assistant)
    context_handling.add_to_agent(helper)
    context_handling.add_to_agent(executor)
    context_handling.add_to_agent(verify)
    
    
    agents_object['assistant'] = assistant
    agents_object['helper'] = helper
    agents_object['author'] = author
    agents_object['user'] = author
    agents_object['executor'] = executor
    agents_object['verify'] = verify
    agents_object['chat_instructor'] = chat_instructor
    
    for i in config['personas']:
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
    assistant.register_for_execution(name="text_2_image")(txt2img)
    
    def get_user_camera_inp(inp: Annotated[str, "The Question to check from visual context"]) -> str:
        request_id = 'Autogent_1234'
        current_app.logger.info('Using Vision to answer question')
        frame = get_frame(str(user_id))
        if frame is not None:
            image_path = f"output_images/{user_id}_{request_id}_call.jpg"
            # Ensure the directory exists
            directory = os.path.dirname(image_path)
            if not os.path.exists(directory):
                os.makedirs(directory)
            # Convert the frame (which is a NumPy array) to a PIL image
            image = Image.fromarray(frame)
            # Save the image
            image.save(image_path)
            url = "http://azurekong.hertzai.com:8000/minicpm/upload"
            payload = {
                'prompt': f'Instruction: Respond in second person point of view\ninput:-{inp}'}
            files = [
                ('file', ('call.jpg', open(image_path, 'rb'), 'image/jpeg'))
            ]
            headers = {}
            try:
                response = requests.post(
                    url, headers=headers, data=payload, files=files)
                current_app.logger.info(response.text)
                response = response.text

                return response
            except Exception as e:
                current_app.logger.info('ERROR: Got error in visal QA')
                return 'failed to get visual context ask user to check if the camera is turned on'
            
    helper.register_for_llm(name="get_user_camera_inp", description="Get user's visual information to process somethings")(get_user_camera_inp)
    assistant.register_for_execution(name="get_user_camera_inp")(get_user_camera_inp)  
    
    
    def save_data_in_memory(key: Annotated[str, "Key for storing data now & retrieving data later"],
                    value: Annotated[Optional[str], "Value you want to store"] = None) -> str:
        current_app.logger.info('INSIDE save_data_in_memory')
        agent_data[prompt_id][key] = value
        return f'{agent_data[prompt_id]}'
    
    helper.register_for_llm(name="save_data_in_memory", description="Use this to Store and retrieve data using key-value storage system")(save_data_in_memory)
    assistant.register_for_execution(name="save_data_in_memory")(save_data_in_memory)
    
    def get_saved_metadata() -> str:
        stripped_json = strip_json_values(agent_data[prompt_id])
        return f'{stripped_json}'

    helper.register_for_llm(name="get_saved_metadata", description="Returns all metadata from the internal Memory")(get_saved_metadata)
    assistant.register_for_execution(name="get_saved_metadata")(get_saved_metadata)
    
    def get_data_by_key(key: Annotated[str, "Key for retrieving data"]) -> str:
        return f'{agent_data[prompt_id][key]}'

    helper.register_for_llm(name="get_data_by_key", description="Returns all data from the internal Memory")(get_data_by_key)
    assistant.register_for_execution(name="get_data_by_key")(get_data_by_key)
    
    def get_user_id() -> str:
        current_app.logger.info('INSIDE get_user_id')
        return f'{user_id}'

    
    helper.register_for_llm(name="get_user_id", description="Returns the unique identifier (user_id) of the current user.")(get_user_id)
    assistant.register_for_execution(name="get_user_id")(get_user_id)
    
    def get_prompt_id() -> str:
        current_app.logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'

    
    helper.register_for_llm(name="get_prompt_id", description="Returns the unique identifier (prompt_id) associated with the current prompt or conversation.")(get_prompt_id)
    assistant.register_for_execution(name="get_prompt_id")(get_prompt_id)
    
    def Generate_video(text: Annotated[str, "Text to be used for video generation"],
                       avatar_id: Annotated[str, "Unique identifier for the avatar"],
                       realtime: Annotated[bool,"If True, response is fast but less realistic by default it should be true; if False, response is realistic but slower"]) -> str:
        print('INSIDE Generate_video')
        database_url = 'https://mailer.hertzai.com'
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        print(f"avtar_id: {avatar_id}:\n{text[:10]}....\n")
        
        headers = {'Content-Type': 'application/json'}
        data = {}
        data["text"] = text
        data['flag_hallo'] = 'false'
        data['chattts'] = True
        data['openvoice'] = "false"
        try:
            res = requests.get("https://mailer.hertzai.com/get_image_by_id/{}".format(avatar_id))
            res = res.json()
            new_image_url = res["image_url"]
        except:
            data['openvoice'] = "true"
            new_image_url = None
            res = {'voice_id':None}
        data["cartoon_image"] = "True"
        data["bg_url"] = 'http://stream.mcgroce.com/txt/examples_cartoon/roy_bg.jpg'
        data['vtoonify'] = "false"
        data["image_url"] = new_image_url
        data['im_crop'] = "false"
        data['remove_bg'] = "false"
        data['hd_video'] = "false"
        data['uid'] = request_id
        data['gradient'] = "true"
        data['cus_bg'] = "false"
        data['solid_color'] = "false"
        data['inpainting'] = "false"
        data['prompt'] = ""
        data['gender'] = 'male'
        
        timeout = 60
        if not realtime:
            timeout = 600
            data['chattts'] = True
            data['flag_hallo'] = "true"
            data["cartoon_image"] = False
            
        if res['voice_id'] != None:
            voice_sample = requests.get(
                "{}/get_voice_sample_id/{}".format(database_url, res['voice_id']))
            voice_sample = voice_sample.json()
            data["audio_sample_url"] = voice_sample["voice_sample_url"]
            data['voice_id'] = res['voice_id']
        else:
            voice_sample = None
            data["audio_sample_url"] = None    
            data['voice_id'] = None
        conv_id = save_conversation_db(text,user_id,prompt_id,database_url,request_id)
        data['conv_id'] = conv_id
        data['avatar_id'] = avatar_id
        data['timeout'] = timeout
        try:
            video_link = requests.post("{}/video_generate_save".format(database_url),
                                        data=json.dumps(data), headers=headers, timeout=1)
        except:
            pass
        if data['chattts'] or data['flag_hallo'] == "true":
            return f"Video Generation task added to queue with conv_id:{conv_id} ask helper to save this conv_id along with the text used to generate video for future use."
        else:
            return f"Video Generation completed with conv_id:{conv_id} ask helper to save this conv_id along with the text used to generate video for future use."
    
    helper.register_for_llm(name="Generate_video", description="Generate video with text and save it in database")(Generate_video)
    assistant.register_for_execution(name="Generate_video")(Generate_video)
    
    def recent_files() -> str:
        current_app.logger.info('INSIDE get_user_uploaded_file')
        if recent_file_id[user_id]:
            return f'Got user uploaded file the file_id is {recent_file_id[user_id]}'

        return 'No file uploaded from user'
    
    helper.register_for_llm(name="get_user_uploaded_file", description="get user's recent uploaded files")(recent_files)
    assistant.register_for_execution(name="get_user_uploaded_file")(recent_files)
    

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
    assistant.register_for_execution(name="get_text_from_image")(img2txt)
    
    def create_scheduled_jobs(cron_expression: Annotated[str, "Cron expression for scheduling. Example: '0 9 * * 1-5' (Runs at 9:00 AM, Monday to Friday)."], 
                            job_description: Annotated[str, "Description of the job to be performed"]) -> str:
        current_app.logger.info('INSIDE create_scheduled_jobs')
        if not scheduler.running:
            scheduler.start()
        
        try:
            trigger = CronTrigger.from_crontab(cron_expression)
            job_id = f"job_{int(time.time())}"
            scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, int(user_id),int(prompt_id)])
            current_app.logger.info('Successfully created scheduler job')
            return 'Successfully created scheduler job'
        except Exception as e:
            current_app.logger.error(f'Error in create_scheduled_jobs: {str(e)}')
            return f"Error creating scheduled job: {str(e)}"
        
    helper.register_for_llm(name="create_scheduled_jobs", description="Creates time-based jobs using APScheduler to schedule jobs")(create_scheduled_jobs)
    assistant.register_for_execution(name="create_scheduled_jobs")(create_scheduled_jobs)
    
    def send_response_to_user(text: Annotated[str, "Text to send to the user"],
                         conv_id: Annotated[Optional[str], "Conversation ID associated with the text"] = None,
                         avatar_id: Annotated[Optional[str], "Unique identifier for the avatar"] = None,
                         response_type: Annotated[Optional[str], "Response mode: 'Realistic' (slower, better quality) or 'Realtime' (faster, lower quality)"] = None) -> str:
        current_app.logger.info('INSIDE send_response_to_user')
        current_app.logger.info(f'SENDING DATA 2 user with values text:{text}, conv_id:{conv_id}, avatar_id:{avatar_id}, response_type:{response_type}')
        return 'Message sent successfully to user'
    
    helper.register_for_llm(name="send_response_to_user", description="Sends a message or information to user. You can use this if you want to ask a question")(send_response_to_user)
    assistant.register_for_execution(name="send_response_to_user")(send_response_to_user)
    
    assistant.description = 'this is an assistant agent that coordinates & executes requested tasks & actions'
    executor.description = 'this is an executor agent that Specialized agent for code execution & response handling'
    author.description = 'this is an author/user agent that focused on user support, error resolution, contextual information. Contact this agent when you need any user based information or if you want to say something to user'
    chat_instructor.description = 'this is a ChatInstructor agent that provides step-by-step action plans for task execution'
    helper.description = 'this is a helper agent that facilitates task completion & assists other agents'
    verify.description = 'this is a verify status agent. which will verify the status of current action that will be called after ChatInstructor gives instruction to complete an action & assistant completes it, this agent will provide updates in a structured JSON format & then call user agent'
    
    def state_transition(last_speaker, groupchat):
        current_app.logger.info(f'Inside state_transition with actions {user_tasks[user_prompt].current_action}')
        messages = groupchat.messages
        current_app.logger.info(f'Inside state_transition with message {messages[-1]["content"][:10]}.. & last_speaker:{last_speaker.name}')
        crossbar_message = {"text": [messages[-1]["content"]], "priority": 49, "action": 'Thinking', "historical_request_id": [], "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
        'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0}, 'bottom_left': {'x': 0, 'y': 0}}}
        result = client.publish(
            f"com.hertzai.hevolve.chat.{user_id}", f'{crossbar_message}')
        
        metadata = get_saved_metadata()
        
        
        if not messages[-1]["content"].startswith('Reflect on the sequence'):
            json_obj = retrieve_json(messages[-1]["content"])
            if json_obj:
                try:   
                    current_app.logger.info(f'got status as:{json_obj["status"]} ')
                    if json_obj['status'].lower() == 'error':
                        return author
                    elif json_obj['status'].lower() == 'completed' or json_obj['status'].lower() == 'success':
                        if 'recipe' in json_obj.keys():
                            current_app.logger.info('Recipe created successfully')
                            merged_dict = {**final_recipe[prompt_id], **json_obj}
                            name = f'prompts/{prompt_id}_recipe.json'
                            with open(name, "w") as json_file:
                                json.dump(merged_dict, json_file)
                            current_app.logger.info(f"Dictionary saved to {name}")
                        if 'action_id' in json_obj.keys():
                            user_tasks[user_prompt].actions[int(json_obj['action_id'])-1] = json_obj['action']
                            user_tasks[user_prompt].new_json.append(json_obj)
                            current_app.logger.info(f'CHECKING FOR FALLBACK user_tasks[user_prompt].current_action={user_tasks[user_prompt].current_action} json_obj["action_id"]={json_obj["action_id"]}')
                            if user_tasks[user_prompt].current_action != int(json_obj['action_id']):
                                user_tasks[user_prompt].fallback = True
                            
                            current_app.logger.info(f'UPDATIN CURRENT ACTION AS :{int(json_obj["action_id"])}')
                            user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                        return chat_instructor
                    elif json_obj['status'].lower() == 'updated':
                        if 'entire_actions' in json_obj.keys() and type(json_obj['entire_actions'])==list:
                            current_app.logger.info('GOT UPDATED WITH entire actions')
                            try:
                                current_app.logger.info(f"user_tasks[user_prompt].actions:{len(user_tasks[user_prompt].actions)}, len(json_obj['entire_actions']:{len(json_obj['entire_actions'])}")
                                current_app.logger.info(f"user_tasks[user_prompt].actions:{user_tasks[user_prompt].actions}, len(json_obj['entire_actions']:{json_obj['entire_actions']}")
                                
                                current_app.logger.info('')
                                entire_actions = json_obj['entire_actions']
                                user_tasks[user_prompt].actions = entire_actions
                                user_tasks[user_prompt].current_action = 0
                                user_tasks[user_prompt].fallback = False
                                user_tasks[user_prompt].recipe = False
                            except Exception as e:
                                current_app.logger.info(f'error is here:{e}')
                                user_tasks[user_prompt].actions[int(json_obj['action_id'])-1] = json_obj['updated_action']
                                user_tasks[user_prompt].new_json.append(json_obj)
                                user_tasks[user_prompt].fallback = True
                        elif 'action_id' in json_obj.keys():
                            user_tasks[user_prompt].actions[int(json_obj['action_id'])-1] = json_obj['updated_action']
                            user_tasks[user_prompt].new_json.append(json_obj)
                            user_tasks[user_prompt].fallback = True
                    elif json_obj['status'].lower() == 'done':
                        current_app.logger.info('Got Individual action recipe save it')
                        name = f'prompts/{prompt_id}_{json_obj["action_id"]}.json'
                        user_tasks[user_prompt].fallback = False
                        user_tasks[user_prompt].recipe = False
                        with open(name, "w") as json_file:
                            json.dump(json_obj, json_file)
                        current_app.logger.info(f'Saved Individual recipe at: {name}')
                        
                        return chat_instructor   
                except:
                    pass
            
        pattern = r"@Helper"
        pattern1 = r"@Executor"
        pattern2 = r"@User"
        pattern3 = r"@StatusVerifier"
        try:
            if re.search(pattern2, messages[-1]["content"]):
                current_app.logger.info("String contains @User returnng author")
                messages[-1]["content"] = messages[-1]["content"].replace('@User','')
                return author
            if re.search(pattern3, messages[-1]["content"]):
                current_app.logger.info("String contains @StatusVerifier returnng StatusVerifier")
                return verify
            if re.search(pattern, messages[-1]["content"]) and last_speaker.name != 'Helper':
                current_app.logger.info("String contains @Helper returnng helper")
                messages[-1]["content"] = messages[-1]["content"].replace('@user','')
                group_chat.messages[-1]['content'] = f"{group_chat.messages[-1]['content']}\n Metadata/skeleton of all keys for retrieving data from memory:{metadata}"
                return helper
            if re.search(pattern1, messages[-1]["content"]):
                current_app.logger.info("String contains @Executor returnng executor")
                return executor
        except Exception as e:
            current_app.logger.error(f'Got error when searching for @user in last message :{e}')
            
        
            
        if last_speaker.name == 'Executor' or last_speaker.name == 'UserProxy' or last_speaker.name == 'UserProxy' or last_speaker.name == 'ChatInstructor':
            
            group_chat.messages[-1]['content'] = f"{group_chat.messages[-1]['content']}\n Metadata/skeleton of all keys for retrieving data from memory:{metadata}"
            current_app.logger.info('Got last speaker as executor or helper or author or chat_instructor & reutrning next speaker as assistant')
            return assistant
        json_obj = None
        
        if last_speaker == verify:
            current_app.logger.info('Got last speaker as verify_status & returning next speaker as chat_instructor')
            return chat_instructor
        try:
            if messages[-1]["content"] == '':
                groupchat.messages[-1]["content"] = 'tool call'
            if 'exitcode:' in messages[-1]["content"]:
                current_app.logger.info('Got exitcode in text returning assistant')
                group_chat.messages[-1]['content'] = f"{group_chat.messages[-1]['content']}\n Metadata/skeleton of all keys for retrieving data from memory:{metadata}"

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
    select_speaker_transforms = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=30,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=3000, max_tokens_per_message=500, min_tokens=0),
        ]
    )
    
    group_chat = autogen.GroupChat(
        agents=all_agents,
        messages=[],
        max_round=20,
        # select_speaker_message_template='''You manage a team that Completes a list of Actions provided by ChatInstructor Agent.
        # The Agents available in the team are: Assistant, Helper, Executor, ChatInstructor, StatusVerifier & User''',
        # select_speaker_prompt_template=f"Read the above conversation, select the next person from [Assistant, Helper, Executor, ChatInstructor, StatusVerifier & User] & only return the role as agent.",
        select_speaker_transform_messages=select_speaker_transforms,
        speaker_selection_method=state_transition,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=False
    )
    
    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"config_list": config_list,"cache_seed": None,"max_tokens": 1500}
    )
    
    
    
    return author, assistant, executor, group_chat, manager, chat_instructor, agents_object

user_tasks = {}

def get_response_group(user_id,text,prompt_id,Failure=False,error=None):
    user_prompt = f'{user_id}_{prompt_id}'
    # Get or create agents for this user
    if user_prompt not in user_agents:
        author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = create_agents(user_id,user_tasks[user_prompt],prompt_id)
        user_agents[user_prompt] = (author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object)
        messages[user_prompt] = []
    else:
        author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_prompt]
    clear_history = False
    if Failure:
        current_app.logger.warning(f'CHECK THIS OUT group_chat.messages:{group_chat.messages[-1]}')
        for i in range(len(group_chat.messages)):
            group_chat.messages[i]['role'] = 'user'
        clear_history = True
        message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)
        text = f'Action {user_tasks[user_prompt].current_action+1}: {message} '

    if len(messages[user_prompt])>0:
        # last_agent, last_message = manager.resume(messages=messages[user_prompt])
        try:
            result = agents_object['user'].initiate_chat(recipient=manager, message=text, clear_history=clear_history,silent=False)
        except Exception as e:
            current_app.logger.error(f'Got some error it can be multiple tools called at one error:{e}')
            current_app.logger.error(f'len of group chat :{len(group_chat.messages)}')
            # current_app.logger.error(f' group chat :{group_chat.messages}')
            
            
            for i in range(len(group_chat.messages)):
                group_chat.messages[i]['role'] = 'user'
            message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)
            text = f'Action {user_tasks[user_prompt].current_action+1}: {message}'
            result = agents_object['helper'].initiate_chat(recipient=manager, message=text, clear_history=True,silent=False)
            
    else:
        message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)
        message = f'Action {user_tasks[user_prompt].current_action+1}: {message} '
        crossbar_message = {"text": ["Working on "+message+".\n please evaluate the response i am giving to check if it meets the current action"], "priority": 49, "action": 'Thinking', "historical_request_id": [], "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
        'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0}, 'bottom_left': {'x': 0, 'y': 0}}}
        result = client.publish(
            f"com.hertzai.hevolve.chat.{user_id}", f'{crossbar_message}')
        result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
    
    current_app.logger.info("\n=== Chat Summary ===")
    current_app.logger.info("\n=== Full response ===")
    # current_app.logger.info(result)
    

    
    while True:
        file_path = f'prompts/{prompt_id}.json'
        with open(file_path, 'r') as f:
            data = json.load(f)
            role = [x['name'] for x in data['personas']]
        current_app.logger.info('inside while')
        if group_chat.messages[-1]['name'] == 'ChatInstructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
            current_app.logger.info(f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content'][:10]}..")
            json_obj = retrieve_json(group_chat.messages[-2]["content"])
            if json_obj and 'status' in json_obj.keys():
                if json_obj['status'].lower() == 'completed' and 'recipe' not in json_obj.keys():
                    if user_tasks[user_prompt].current_action != int(json_obj['action_id']):
                        user_tasks[user_prompt].fallback = True
                    current_app.logger.info(f'UPDATIN CURRENT ACTION AS :{int(json_obj["action_id"])}')
                    user_tasks[user_prompt].current_action = int(json_obj['action_id'])                
            else:
                current_app.logger.warning(f'it is not a json object the error is:')
                current_app.logger.info('it is not a json object You should ask status verifier to give response in proper format & not move ahead to next action')
                if user_tasks[user_prompt].fallback == True or user_tasks[user_prompt].recipe == True:
                    actions_prompt = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action-1)
                    message = 'Hey @StatusVerifier Agent, Please verify the status of the action '+f'{user_tasks[user_prompt].current_action}: {actions_prompt}'+'\n performed and Respond in the following format {"status": "status here","action": "current action","action_id":'+f'{user_tasks[user_prompt].current_action}'+',"message": "message here","fallback_action": "fallback action here"}'
                else:
                    actions_prompt = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)
                    message = 'Hey @StatusVerifier Agent, Please verify the status of the action '+f'{user_tasks[user_prompt].current_action+1}: {actions_prompt}'+'\n performed and Respond in the following format {"status": "status here","action": "current action","action_id": '+f'{user_tasks[user_prompt].current_action+1}'+',"message": "message here","fallback_action": "fallback action here"}'
                result = assistant_agent.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                continue
            current_app.logger.info('resuming chat')
            if user_tasks[user_prompt].current_action>=len(user_tasks[user_prompt].actions):
                if user_tasks[user_prompt].recipe == True:
                    user_tasks[user_prompt].recipe = False
                    user_tasks[user_prompt].fallback = False
                    metadata = strip_json_values(agent_data[prompt_id])
                    message = '''Focus on the current task at hand and create a detailed recipe that includes only the necessary steps for this action, along with a suitable name. Provide the output in the following JSON format:
                    { "status", "done", "action": "Describe the action performed here","fallback_action":"", "persona":"","action_id": '''+f'{user_tasks[user_prompt].current_action}'+''', "recipe": "Include the recipe with all steps, tools, and generalized functions to perform this action successfully","can_perform_without_user_input":"can you perform this action on your own without user input in future. only say no when it is absolutely mandatory and you cannot proceed without it, if you can proceed by checking with other agents you should say yes.  say yes/no if no they give the reason as well e.g. no-i need user's likes and dislike", "generalized_functions": "Only include this field if any Python code is created, otherwise omit it entirely.","metadata":"metadata saved in memory till this action", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job exists; otherwise, do not create it.","persona":"", "action_entry_point":"An integer action_id is required as an entrypoint from list of existing action_ids to perform this job","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }
                    Recipe Requirements:
                    1. Generalized Python Functions: Give the code which was created and excuted successfully without any error handling edge cases. leave it blank when there is no code nedded to perform the action
                    2. Avoid directly storing any specific information provided by the author in the recipe. Use placeholders for variables instead.
                    3. Ensure that coding and non-coding steps are not combined within the same function.
                    4. For all Python functions, include comprehensive docstrings to explain their purpose, parameters, and usage. This should especially clarify non-coding steps that require utilizing the assistant's language capabilities.
                    5. If any internal tool is used to complete a step, provide detailed instructions on how to call or utilize that tool instead of providing the code for that step.
                    '''+f'6. Metadata created till this action: {metadata}\n7. The persona must be one of the following: {role}. No other personas are allowed.'
                elif user_tasks[user_prompt].fallback == True:
                    user_tasks[user_prompt].recipe = True
                    user_tasks[user_prompt].fallback = False
                    message = f" Action {user_tasks[user_prompt].current_action} fallback:ask user what actions should be taken if current actions fail in the future after you get the response from user give the conversaation to StatusVerifier agent"      
                else:
                    user_tasks[user_prompt].new_json.append(json_obj)
                    user_tasks[user_prompt].current_action += 1
                    name = f'prompts/{prompt_id}_new.json'
                    with open(name, "w") as json_file:
                        json.dump(user_tasks[user_prompt].new_json, json_file)
                    current_app.logger.info('updating updated action in .json')
                    individual_recipe = []
                    for i in range(1,(user_tasks[user_prompt].current_action)):
                        current_app.logger.info(f'checking for prompts/{prompt_id}_{i}.json')
                        try:
                            with open(f"prompts/{prompt_id}_{i}.json", 'r') as f:
                                config = json.load(f)
                                individual_recipe.append(config)
                        except Exception as e:
                            current_app.logger.error(f'Got error as :{e} while checking for prompts/{prompt_id}_{i}.json')
                            
                    group_chat.messages[-1]['content'] = f'{individual_recipe}'
                    assistant_agent.update_system_message = 'Check if the current_action depends on any other action, regardless of order—it can be before or after this action. If yes, return the list of action IDs that this action depends on to ChatInstructor (e.g., [1,2]). Otherwise, return an empty array []. \nIMPORTANT: Respond strictly in an array [] format.'
                    for num,action in enumerate(user_tasks[user_prompt].actions,1):
                        message = f'''Check if the current_action depends on any other action, regardless of order—it can be before or after this action. If yes, return the list of action IDs that this action depends on to ChatInstructor (e.g., [1,2]). Otherwise, return an empty array []. \nIMPORTANT: Respond strictly in an array [] format.\n current_action: {action}'''
                        result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                        for i in range(1,4):
                            text = group_chat.messages[-i]['content']
                            match = re.search(r'\[.*?\]', text)
                            if match:
                                break
                        if match:
                            action_ids = eval(match.group())
                            file_path = f'prompts/{prompt_id}_{num}.json'
                            with open(file_path, 'r') as f:
                                data = json.load(f)
                            data['actions_this_action_depends_on'] = action_ids
                            with open(file_path, 'w') as f:
                                json.dump(data, f, indent=4)
                        else:
                            file_path = f'prompts/{prompt_id}_{num}.json'
                            with open(file_path, 'r') as f:
                                data = json.load(f)
                            data['actions_this_action_depends_on'] = []
                            with open(file_path, 'w') as f:
                                json.dump(data, f, indent=4)
                    
                    individual_recipe = []
                    for i in range(1,(user_tasks[user_prompt].current_action)):
                        current_app.logger.info(f'checking for prompts/{prompt_id}_{i}.json')
                        try:
                            with open(f"prompts/{prompt_id}_{i}.json", 'r') as f:
                                config = json.load(f)
                                individual_recipe.append(config)
                        except Exception as e:
                            current_app.logger.error(f'Got error as :{e} while checking for prompts/{prompt_id}_{i}.json')
                    #TOPOLOGICAL SORT & CHECK FOR CYCLIC DEPENDENCY
                    status,updated_actions, cyc = topological_sort(individual_recipe)
                    if not status:
                        res = fix_actions(individual_recipe,cyc)
                        for i in res:
                            for j in individual_recipe:
                                if i['action_id'] == j['action_id']:
                                    j['actions_this_action_depends_on'] = i['actions_this_action_depends_on']
                                    break
                        status,updated_actions, cyc = topological_sort(individual_recipe)
                    group_chat.messages[-1]['content'] = f'{updated_actions}'
                    file_path = f'prompts/{prompt_id}.json'
                    with open(file_path, 'r') as f:
                        data = json.load(f)
                        role = [x['name'] for x in data['personas']]
                    final_recipe[prompt_id] = {"status":"completed","actions":updated_actions}
                    assistant_agent.update_system_message = '''Reflect on the sequence of tasks and create scheduled_tasks with proper persona name and action_entry_point. Provide the output in the following JSON format:
                    { "status", "completed", "recipe": "you should keep it blank.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job exists; otherwise, do not create it.","persona":"", "action_entry_point":"An integer action_id is required as an entrypoint from list of existing action_ids to perform this job","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }'''
                    current_app.logger.info(f'user_tasks[user_prompt].current_action:{user_tasks[user_prompt].current_action} == len(user_tasks[user_prompt].actions)')
                    message = '''Reflect on the sequence of tasks and create scheduled_tasks with proper persona name and action_entry_point. Provide the output in the following JSON format:
                    { "status", "completed", "recipe": "you should keep it blank.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job exists; otherwise, do not create it.","persona":"", "action_entry_point":"An integer action_id is required as an entrypoint from list of existing action_ids to perform this job","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }
                    Recipe Requirements:
                        '''+f"1. The persona must be one of the following: {role}. No other personas are allowed."
                    chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=True,silent=False)
                    last_message = group_chat.messages[-1]
                    json_response = retrieve_json(last_message['content'])
                    if json_response and 'status' in json_response.keys(): 
                        merged_dict = {**final_recipe[prompt_id], **json_response}
                        current_app.logger.info('Recipe created successfully')
                        name = f'prompts/{prompt_id}_recipe.json'
                        with open(name, "w") as json_file:
                            json.dump(merged_dict, json_file)
                        url = f'https://mailer.hertzai.com/update_agent_prompt?prompt_id={prompt_id}'
                        headers = {'Content-Type': 'application/json'}
                        res = requests.patch(url,headers=headers)
                        return 'Agent Created Successfully'
                    return 'Agent created successfully'
                result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=True,silent=False)
            else:
                # user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                current_app.logger.info(f'current action {user_tasks[user_prompt].current_action} & fallback {user_tasks[user_prompt].fallback} & recipe {user_tasks[user_prompt].recipe}')
                user_tasks[user_prompt].new_json.append(json_obj)
                try:
                    message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)
                except:
                    individual_recipe = []
                    for i in range(1,(user_tasks[user_prompt].current_action)):
                        current_app.logger.info(f'checking for prompts/{prompt_id}_{i}.json')
                        try:
                            with open(f"prompts/{prompt_id}_{i}.json", 'r') as f:
                                config = json.load(f)
                                individual_recipe.append(config)
                        except Exception as e:
                            current_app.logger.error(f'Got error as :{e} while checking for prompts/{prompt_id}_{i}.json')
                    group_chat.messages[-1]['content'] = f'{individual_recipe}'
                    assistant_agent.update_system_message = 'Check if the current_action depends on any other action, regardless of order—it can be before or after this action. If yes, return the list of action IDs that this action depends on to ChatInstructor (e.g., [1,2]). Otherwise, return an empty array []. \nIMPORTANT: Respond strictly in an array [] format.'
                    for num,action in enumerate(user_tasks[user_prompt].actions,1):
                        message = f'''Check if the current_action depends on any other action, regardless of order—it can be before or after this action. If yes, return the list of action IDs that this action depends on to ChatInstructor (e.g., [1,2]). Otherwise, return an empty array []. \nIMPORTANT: Respond strictly in an array [] format.\n current_action: {action}'''
                        result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                        for i in range(1,4):
                            text = group_chat.messages[-i]['content']
                            match = re.search(r'\[.*?\]', text)
                            if match:
                                break
                        if match:
                            action_ids = eval(match.group())
                            file_path = f'prompts/{prompt_id}_{num}.json'
                            with open(file_path, 'r') as f:
                                data = json.load(f)
                            data['actions_this_action_depends_on'] = action_ids
                            with open(file_path, 'w') as f:
                                json.dump(data, f, indent=4)
                        else:
                            file_path = f'prompts/{prompt_id}_{num}.json'
                            with open(file_path, 'r') as f:
                                data = json.load(f)
                            data['actions_this_action_depends_on'] = []
                            with open(file_path, 'w') as f:
                                json.dump(data, f, indent=4)
                    
                    individual_recipe = []
                    for i in range(1,(user_tasks[user_prompt].current_action)):
                        current_app.logger.info(f'checking for prompts/{prompt_id}_{i}.json')
                        try:
                            with open(f"prompts/{prompt_id}_{i}.json", 'r') as f:
                                config = json.load(f)
                                individual_recipe.append(config)
                        except Exception as e:
                            current_app.logger.error(f'Got error as :{e} while checking for prompts/{prompt_id}_{i}.json')
                    status,updated_actions, cyc = topological_sort(individual_recipe)
                    if not status:
                        res = fix_actions(individual_recipe,cyc)
                        for i in res:
                            for j in individual_recipe:
                                if i['action_id'] == j['action_id']:
                                    j['actions_this_action_depends_on'] = i['actions_this_action_depends_on']
                                    break
                        status,updated_actions, cyc = topological_sort(individual_recipe)
                        
                    group_chat.messages[-1]['content'] = f'{updated_actions}'
                    file_path = f'prompts/{prompt_id}.json'
                    with open(file_path, 'r') as f:
                        data = json.load(f)
                        role = [x['name'] for x in data['personas']]
                    final_recipe[prompt_id] = {"status":"completed","actions":updated_actions}
                    assistant_agent.update_system_message = '''Reflect on the sequence of tasks and create scheduled_tasks with proper persona name and action_entry_point. Provide the output in the following JSON format:
                    { "status", "completed", "recipe": "you should keep it blank.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job exists; otherwise, do not create it.","persona":"", "action_entry_point":"An integer action_id is required as an entrypoint from list of existing action_ids to perform this job","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }'''
                    message = '''Reflect on the sequence of tasks and create scheduled_tasks with proper persona name and action_entry_point. Provide the output in the following JSON format:
                    { "status", "completed", "recipe": "you should keep it blank.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job exists; otherwise, do not create it.","persona":"", "action_entry_point":"An integer action_id is required as an entrypoint from list of existing action_ids to perform this job","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }
                    Recipe Requirements:
                        '''+f"1. The persona must be one of the following: {role}. No other personas are allowed."
                    chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                    last_message = group_chat.messages[-1]
                    json_response = retrieve_json(last_message['content'])
                    if json_response and 'status' in json_response.keys(): 
                        merged_dict = {**final_recipe[prompt_id], **json_response}
                        current_app.logger.info('Recipe created successfully')
                        name = f'prompts/{prompt_id}_recipe.json'
                        with open(name, "w") as json_file:
                            json.dump(merged_dict, json_file)
                        url = f'https://mailer.hertzai.com/update_agent_prompt?prompt_id={prompt_id}'
                        headers = {'Content-Type': 'application/json'}
                        res = requests.patch(url,headers=headers)
                        return 'Agent Created Successfully'
                    return 'Agent created successfully'
                current_app.logger.info('checking for fallback and recipe')
                if user_tasks[user_prompt].recipe == True:
                    user_tasks[user_prompt].recipe = False
                    user_tasks[user_prompt].fallback = False
                    metadata = strip_json_values(agent_data[prompt_id])
                    message = '''Focus on the current task at hand and create a detailed recipe that includes only the necessary steps for this action, along with a suitable name. Provide the output in the following JSON format:
                    { "status", "done", "action": "Describe the action performed here","fallback_action":"", "persona":"","action_id": '''+f'{user_tasks[user_prompt].current_action}'+''', "recipe": "Include the recipe with all steps, tools, and generalized functions to perform this action successfully","can_perform_without_user_input":"can you perform this action on your own without user input in future. only say no when it is absolutely mandatory and you cannot proceed without it, if you can proceed by checking with other agents you should say yes.  say yes/no if no they give the reason as well e.g. no-i need user's likes and dislike", "generalized_functions": "Only include this field if any Python code is created, otherwise omit it entirely.","metadata":"metadata saved in memory till this action", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job exists; otherwise, do not create it.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job exists; otherwise, do not create it.", "persona":"","action_entry_point":"An integer action_id is required as an entrypoint from list of existing action_ids to perform this job","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }
                    Recipe Requirements:
                    1. Generalized Python Functions: Give the code which was created and excuted successfully without any error handling edge cases. leave it blank when there is no code nedded to perform the action
                    2. Avoid directly storing any specific information provided by the author in the recipe. Use placeholders for variables instead.
                    3. Ensure that coding and non-coding steps are not combined within the same function.
                    4. For all Python functions, include comprehensive docstrings to explain their purpose, parameters, and usage. This should especially clarify non-coding steps that require utilizing the assistant's language capabilities.
                    5. If any internal tool is used to complete a step, provide detailed instructions on how to call or utilize that tool instead of providing the code for that step.
                    '''+f'6. Metadata created till this action: {metadata}\n7. The persona must be one of the following: {role}. No other personas are allowed.'
                elif user_tasks[user_prompt].fallback == True:
                    user_tasks[user_prompt].recipe = True
                    user_tasks[user_prompt].fallback = False
                    message = f" Action {user_tasks[user_prompt].current_action} fallback:ask user what actions should be taken if current actions fail in the future after you get the response from user give the conversaation to StatusVerifier agent"      
                else:
                    # user_tasks[user_prompt].current_action = user_tasks[user_prompt].current_action+1
                    message = f'Action {user_tasks[user_prompt].current_action+1}: {message} '
                    crossbar_message = {"text": ["Working on "+message], "priority": 49, "action": 'Thinking', "historical_request_id": [], "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
                    'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0}, 'bottom_left': {'x': 0, 'y': 0}}}
                    result = client.publish(
                        f"com.hertzai.hevolve.chat.{user_id}", f'{crossbar_message}')
                
                result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
            current_app.logger.info("\n=== Chat Summary ===")
            current_app.logger.info("\n=== Full response ===")
            # current_app.logger.info(result)
            
                
        else:
            break
            
        if user_tasks[user_prompt].current_action >len(user_tasks[user_prompt].actions):
            current_app.logger.info(f'current action {user_tasks[user_prompt].current_action} is greater than legth {len(user_tasks[user_prompt].actions)}')
            break            

    messages[user_prompt] = group_chat.messages
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]
    return last_message['content']

messages = {}
recent_file_id = {}

def recipe(user_id, text,prompt_id,file_id):
    user_prompt = f'{user_id}_{prompt_id}'
    current_app.logger.info('--'*100)
    if file_id:
            recent_file_id[user_id] = file_id

    if user_prompt not in user_tasks.keys():
        with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
        user_tasks[user_prompt] = Action(config['flows'][0]['actions'])
        agent_data[prompt_id] = {'user_id':user_id}
    try:
        
        last_response = get_response_group(user_id,text,prompt_id)

    except Exception as e:
        current_app.logger.error(f"Error occurred in create Recipe: {str(e)}")  # Add logging for debugging
        last_response = get_response_group(user_id,text,prompt_id,True,e)
        
    try:
        json_response = retrieve_json(last_response)
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
    

