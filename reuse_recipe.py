from typing import Dict, Tuple
import autogen
import os
import requests
import uuid
import time
from datetime import datetime
from typing_extensions import Annotated
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import json
import mimetypes
import redis
import pickle
from PIL import Image
from langchain.memory import ZepMemory
from crossbarhttp import Client
from autobahn.twisted.wamp import Application
from autobahn.twisted.component import Component, run
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
import threading
from autogen import ConversableAgent, register_function
import requests
from flask import current_app

client = Client('http://aws_rasa.hertzai.com:8088/publish')
scheduler = BackgroundScheduler()
scheduler.start()

# Store user-specific agents & their chat history
user_agents: Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]] = {}
role_agents: Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]] = {}
agents_session = {}
recipes = {}
user_journey = {}
temp_users = {}
chat_joinees = {}
agents_roles = {}

redis_client = redis.StrictRedis(
    host='azure_all_vms.hertzai.com', port=6369, db=0)

config_list = [{
    "model": 'hertzai-4o',
    "api_type": "azure",
    "api_key": '8f3cd49e1c3346128ba77d09ee9c824c',
    "base_url": 'https://hertzai-gpt4.openai.azure.com/',
    "api_version": "2024-02-15-preview"
}]
executor_config = {
    "llm_config": {
        "config_list": config_list,
        "temperature": 0.4,
    }
}

def send_message_to_user(user_id,response,inp):
    url = 'http://aws_rasa.hertzai.com:9890/autogen_response'
    body = json.dumps({'user_id':user_id,'message':response,'inp':inp})
    headers = {'Content-Type': 'application/json'}
    res = requests.post(url,data=body,headers=headers)

def execute_python_file(task_description:str,user_id: int):
    print('inside calling user agent at time')
    if user_id not in user_agents:
        print('user_id is not present')
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

def get_frame(user_id):
    serialized_frame = redis_client.get(user_id)

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
    

def get_time_based_history(prompt: str, session_id: str, start_date: str, end_date: str):
    '''
        This function help to extract messages till specified time
        inputs:
            prompt: text from user from which we need to extract similar messages
            session_id: user_{user_id}
            start_date: time of search start
            end_date: time till search
    '''

    start_time = time.time()
    memory = ZepMemory(
        session_id=session_id,
        url='http://azure_all_vms.hertzai.com:8000',
        api_key='***REMOVED***',
        memory_key="chat_history",
    )

    try:

        metadata = {
            "start_date": start_date,
            "end_date":  end_date
        }

        try:
            messages = memory.chat_memory.search(prompt, metadata=metadata)
            current_app.logger.info(f'GOT THE messages from search {messages}')
        except Exception as e:
            current_app.logger.info(f'Error: {e}')
        try:
            extracted_metadata = [message.message['metadata']
                                  for message in messages]
            list_req_ids = [data.get('request_Id', None)
                            for data in extracted_metadata]
            current_app.logger.info(f'GOT THE EXTRACTED METADATA AS {extracted_metadata}')
        except Exception as e:
            current_app.logger.info(f"Error while getting req ids {e}")

        # messages = [message.dict() for message in messages]
        serialized_results = []
        for result in messages:
            serialized_result = result.dict(exclude_unset=True)
            # Process the 'message' field to include only specific subfields
            if 'message' in serialized_result and isinstance(serialized_result['message'], dict):
                message = serialized_result['message']
                filtered_message = {
                    'content': message.get('content'),
                    'role': message.get('role'),
                    'created_at': message.get('created_at'),
                    'request_id': message.get('metadata', {}).get('request_id') if 'metadata' in message else None
                }
                # Replace the original message with the filtered message
                serialized_result['message'] = filtered_message
            serialized_results.append(serialized_result)
        messages = serialized_results
        final_res = {'res_in_filter': messages}
        current_app.logger.info(f"final-->{final_res}")
        end_time = time.time()
        elapsed_time = end_time - start_time
        return json.dumps(final_res)
    except Exception as e:
        current_app.logger.info(f"Exception {e}")
        try:
            messages = memory.chat_memory.search(prompt)
        except:
           current_app.logger.info(f'Error: {e}')

        # current_app.logger.info(f"final messages in except-->{messages}")
        try:
            extracted_metadata = [message.message['metadata']
                                  for message in messages]
            list_req_ids = [data.get('request_Id', None)
                            for data in extracted_metadata]
        except Exception as e:
            current_app.logger.info(f"Error while getting req ids {e}")
        end_time = time.time()
        elapsed_time = end_time - start_time
        current_app.logger.info("time taken for zep is {elapsed_time}")
        return json.dumps({'res': [message.message['content'] for message in messages]})



def create_agents_for_role(user_id: str,prompt_id):
    current_app.logger.info('INSIDE create_agents_for_role')
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
            current_app.logger.info(f'Available Personas {personas}')
    except Exception as e:
        current_app.logger.info(e)
    if len(personas)>1: # & also check if we have record in db/agents_session to reuser
        temp = personas.copy()
        # temp.append({"name":"user","description":"User who will use this app"})
        agent_prompt = f'''You are a Helpful Assistant follow below action's
        initiate the conversation by asking which persona they belong to among the available personas: {temp} // give the persona names & ask to select one
        after you get the persona response from user ask them Would you like to start a new chat, or join an existing one with another user?
        if new chat then call the update_persona tool to update the records in db & return TERMINATE
        if they want to join an existing chat then ask the user to give the main user's contact number & then call the update_persona tool to update the records in db & return TERMINATE
        Note: only consider answers from User agent, 
        '''
        assistant = autogen.AssistantAgent(
            name=f"assistant",
            llm_config=llm_config,
            max_consecutive_auto_reply=10,
            is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
            code_execution_config={"work_dir": "coding", "use_docker": False},
            system_message=agent_prompt
        )
        user_proxy = autogen.UserProxyAgent(
            name=f"user",
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
            system_message="""You Help the assistant agent to complete the task, you are helper agent not user/n
            if you get any request related you user redicrect that conversation to user don't asumer anything or answer anything on your own""",
            is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        )
        
        
        @helper.register_for_execution()
        @assistant.register_for_llm(api_style="function",description="update the role/persona in db")
        def update_persona(name: Annotated[str, "The persona name user selected"],
                           description: Annotated[str, "The persona description user selected"],
                           new: Annotated[bool, "Wethere it is a new chat or no"],
                           contact_number: Annotated[str, "user's contact of which we will join conversation"]) -> str:
            current_app.logger.info('INSIDE update_persona')
            current_app.logger.info(f'agents_session {agents_session}')
            current_app.logger.info(f'chat_joinees {chat_joinees}')
            if new:
                current_app.logger.info('Creating new chat')
                if f"{user_id}_{prompt_id}" not in agents_session.keys():
                    agents_session[f"{user_id}_{prompt_id}"] = [{'agentInstanceID':f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
                                                'user_id':user_id,'role':name,'deviceID':'something'}]
                    agents_roles[f"{user_id}_{prompt_id}"] = {user_id:name}
                else:
                    agents_session[f"{user_id}_{prompt_id}"].append({'agentInstanceID':f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
                                                'user_id':user_id,'role':name,'deviceID':'something'})
                    agents_roles[f"{user_id}_{prompt_id}"][user_id] = name
                current_app.logger.info(f'After persona update {agents_session[f"{user_id}_{prompt_id}"]}')
                return 'terminate'
            else:
                current_app.logger.info('adding in existing chat')
                if contact_number in temp_users.keys():
                    current_app.logger.info('user found with contact number')
                    if f"{temp_users[contact_number]}_{prompt_id}" in agents_session.keys():
                        current_app.logger.info('user found with contact number in agents_sessiion')
                        agents_session[f"{temp_users[contact_number]}_{prompt_id}"].append({'agentInstanceID':f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
                                                    'user_id':user_id,'role':name,'deviceID':'something'})
                        agents_roles[f"{user_id}_{prompt_id}"][user_id] = name
                        current_app.logger.info('after append in agent_sessions')
                        chat_joinees[user_id] = {prompt_id : temp_users[contact_number]}
                        
                        current_app.logger.info(f'agents_session {agents_session}')
                        current_app.logger.info(f'chat_joinees {chat_joinees}')
                        return 'terminate'
                    else:
                        return f'Ask the user with contact number:{contact_number} to create a new chat'
                else:
                    current_app.logger.info('user found not with contact number')
                    return f'Ask the user with contact number:{contact_number} to create a new chat'
        
        
        assistant.description = 'Agent that is designed ask the roles to the user agent'
        user_proxy.description = 'agent will act as user & perform task assigned to user'
        helper.description = 'Agent will only work with assistant agent if needs help with something which is not related to user'
        
        
        def state_transition(last_speaker, groupchat):
            messages = groupchat.messages
            if last_speaker == user_proxy:
                return assistant
            if 'TERMINATE' in messages[-1]["content"].upper():
                current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
                # retrieve: action 1 -> action 2
                return None
            return "auto"
            
        group_chat = autogen.GroupChat(
            agents=[assistant, helper, user_proxy],
            messages=[],
            # messages_per_round=15,
            # speaker_selection_method="auto",  # using an LLM to decide
            speaker_selection_method=state_transition,  # using an LLM to decide
            allow_repeat_speaker=False,  # Prevent same agent speaking twice
            send_introductions=True
        )
        
        manager = autogen.GroupChatManager(
            groupchat=group_chat,
            llm_config={"config_list": config_list}
        )
        
        
        

        return assistant, user_proxy, group_chat, manager, helper,False
    else:
        agents_session[f"{user_id}_{prompt_id}"] = [{'agentInstanceID':f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
                                                'user_id':user_id,'role':personas[0]['name'],'deviceID':'something'}]
        
        agents_roles[f"{user_id}_{prompt_id}"] = {user_id:personas[0]['name']}
        return 'TERMINATE','TERMINATE','TERMINATE','TERMINATE','TERMINATE', True



def create_agents_for_user(user_id: str,prompt_id) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
    """Create new assistant & user proxy agents for a user with basic configuration."""
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
    creator = True if f'{user_id}_{prompt_id}' in agents_session.keys() else False
    role = None
    if creator:
        for i in agents_session[f'{user_id}_{prompt_id}']:
            if i['user_id'] == user_id:
                role = i['role']
                break
    if not role:
        if user_id in chat_joinees.keys():
            chat_creator_user_id = f"{chat_joinees[user_id][prompt_id]}_{prompt_id}"
            for i in agents_session[f"{chat_creator_user_id}"]:
                if i['user_id'] == user_id:
                    role = i['role']
                    break
    if not role:
        role = ''
    goal = ''
    with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
            goal = config['goal']
    current_app.logger.info(f'Got goal as {goal}')
    
    agent_prompt = f'''You are a Helpful {role} Assistant. Follow the actions below to assist the user:
        1. Try to complete a task on your own If you are unable to perform a specific task, ask the helper agent for assistance.
        2. Only follow actions where the persona is: {role}.
        3. Follow the steps below to achieve the goal: {goal}.
        4. Use the provided Recipe for more details related to the actions.
        5. Keep track of action and only go to text action when the current action is completed successfully
        6. Always use code from recipe given below
        7. If there is any action which is like to perform a task continously you should not do it.
        8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.

        Actions: <actionsStart>{recipes[prompt_id]['steps']}<actionEnd>
        Recipe: <recipeStart>{recipes[prompt_id]['recipe']}<recipeEnd>
        generalized_functions: <generalized_functionsStart>{recipes[prompt_id]['generalized_functions']}<generalized_functionsEnd>
        When writing code, always print the final response just before returning it.
        Note: Other agents do not have access to these actions or recipe information. Ensure you provide them with the necessary context and related information to perform the required actions.
    '''
    if role == '':
        role = 'Assistant'
    else:
        role = f'{role}_assistant'
    assistant = autogen.AssistantAgent(
        name=role,
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message=agent_prompt
    )
    
    current_app.logger.info(f'creating agent with propt {agent_prompt}')

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
        system_message=f"""You are Helper Agent. Help the {role} agent to complete the task:
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than {role}_assistant,Executor,multi_role_agent.
            4. Tools you have [txt2img,img2txt,user_camera_inp,get_chat_history,create_scheduled_jobs] if you have any task which is not doable by these tool check recipe first else create python code to do so
            5. Keep track of action and only go to text action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            
            Actions: <actionsStart>{recipes[prompt_id]['steps']}<actionEnd>
            Recipe: <recipeStart>{recipes[prompt_id]['recipe']}<recipeEnd>
            generalized_functions: <generalized_functionsStart>{recipes[prompt_id]['generalized_functions']}<generalized_functionsEnd>
            
            When writing code, always print the final response just before returning it.
        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    executor = autogen.AssistantAgent(
        name="Executor",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message=f'''You are a executor agent. focused solely on creating, running & debugging code.
            Your responsibilities:
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than {role}_assistant,Executor,multi_role_agent.
            4. Tools you have [txt2img,img2txt,user_camera_inp,get_chat_history,create_scheduled_jobs] if you have any task which is not doable by these tool check recipe first else create python code to do so
            5. Keep track of action and only go to text action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            
            Actions: <actionsStart>{recipes[prompt_id]['steps']}<actionEnd>
            Recipe: <recipeStart>{recipes[prompt_id]['recipe']}<recipeEnd>
            generalized_functions: <generalized_functionsStart>{recipes[prompt_id]['generalized_functions']}<generalized_functionsEnd>
            
            Note: Your Working Directory is "/home/hertzai2019/newauto/coding" use this if you need,
            Add proper error handling, logging.
            Always provide clear execution results or error messages to the assistant.
            if you get any conversation which is not related to coding ask the manager to route this conversation to user
            When writing code, always print the final response just before returning it.
        ''',
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    multi_role_agent = autogen.AssistantAgent(
        name="multi_role_agent",
        llm_config=llm_config,
        code_execution_config=False,
        system_message="""You will send message from multiple different personas your, job is to ask those question to assistant agent
        if you think some text was intent to give to some other agent but i came to you send the same message to user""",
    )

    # @assistant.register_for_execution()
    # @helper.register_for_llm(api_style="function", description="sends message/ask questions to different roles/personas")
    # def send_message_to_roles(role: Annotated[str, "the role to which the message to send"], 
    #                         message: Annotated[str, "The question to ask or message to send"]) -> str:
    #     current_app.logger.info('INSIDE send_message_to_roles')
    #     if f"{user_id}_{prompt_id}" in agents_session.keys():
    #         for i in agents_session[f"{user_id}_{prompt_id}"]:
    #             if i['role'] == role:
    #                 current_app.logger.info(f'got role: {i}')
    #                 crossbar_message = i
    #                 crossbar_message['message'] = message
    #                 crossbar_message['caller_role'] = agents_roles[f"{user_id}_{prompt_id}"][user_id]
    #                 crossbar_message['caller_user_id'] = user_id
    #                 result = client.publish(
    #                     f"com.hertzai.hevolve.agent.multichat", crossbar_message)
    #                 current_app.logger.info('Published to chat')  
    #                 return 'Message sent Successfully'
    #         return 'Not able to send Message try again later' 
    #     elif user_id in chat_joinees.keys() and prompt_id in chat_joinees[user_id].keys():
    #         current_app.logger.info('contacting user with chat_joinees')
    #         current_app.logger.info(f'chat_joinees[user_id][prompt_id] {chat_joinees[user_id][prompt_id]}  prompt_id{prompt_id}')
    #         chat_creator_user_id = f"{chat_joinees[user_id][prompt_id]}_{prompt_id}"
    #         current_app.logger.info(f'chat_creator_user_id {chat_creator_user_id}')
    #         for i in agents_session[f"{chat_creator_user_id}"]:
    #             if i['role'] == role:
    #                 current_app.logger.info(f'got role: {i}')
    #                 crossbar_message = i
    #                 crossbar_message['message'] = message
    #                 crossbar_message['caller_role'] = agents_roles[chat_creator_user_id][user_id]
    #                 crossbar_message['caller_user_id'] = user_id
    #                 result = client.publish(
    #                     f"com.hertzai.hevolve.agent.multichat", crossbar_message)
    #                 current_app.logger.info(result)
    #                 current_app.logger.info('Published to chat') 
    #                 return 'Message sent Successfully'
    #         return 'Not able to send Message try again later' 
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Text to image Creator")
    def txt2img(text: Annotated[str, "Text to create image"]) -> str:
        current_app.logger.info('INSIDE TXT2IMG')
        url = f"http://aws_rasa.hertzai.com:5459/txt2img?prompt={text}"

        payload = ""
        headers = {}

        response = requests.post(url, headers=headers, data=payload)
        return response.json()['img_url']
        
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Image to Text/Question Answering from image")
    def img2txt(image_url: Annotated[str, "image url of which you want text"],text: Annotated[str, "the details you want from image"]='Describe the Images and Text data in this image in detail') -> str:
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
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Get user's visual information to process somethings")
    def user_camera_inp(inp: Annotated[str, "The Question to check from visual context"]) -> str:
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

    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Get Chat history based on text & start & end date")
    def get_chat_history(text: Annotated[str, "Text related to which you want history"],start: Annotated[str, "start date in format %Y-%m-%dT%H:%M:%S.%fZ"],end: Annotated[str, "end date in format %Y-%m-%dT%H:%M:%S.%fZ"]) -> str:
        current_app.logger.info('INSIDE get_chat_history')
        return get_time_based_history(text, f'user_{user_id}', start, end)
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function", description="Creates time-based jobs using APScheduler to schedule jobs")
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
            current_app.logger.info(f'Error in create_scheduled_jobs: {str(e)}')
            return f"Error creating scheduled job: {str(e)}"

    

    # Let's first define the assistant agent that suggests tool calls.
    time_agent = ConversableAgent(
        name="time",
        system_message="You are a helpful AI assistant. "
        "You can help with creating scheduled jobs "
        "If you want any information/chat history or you are not able to do any task pass the task to main agent using 'connect_time_main' tool"
        "Return 'TERMINATE' when the task is done.",
        llm_config=llm_config,
    )

    # The user proxy agent is used for interacting with the assistant agent
    # & executes tool calls.
    time_user = ConversableAgent(
        name="time_user",
        llm_config=False,
        is_termination_msg=lambda msg: msg.get("content") is not None and "TERMINATE" in msg["content"],
        human_input_mode="NEVER",
    )
    
    # Register the tool signature with the assistant agent.
    time_agent.register_for_llm(name="Scheduler", description="Creates time-based jobs using APScheduler to schedule jobs")(create_scheduled_jobs)

    # Register the tool function with the user proxy agent.
    time_user.register_for_execution(name="Scheduler")(create_scheduled_jobs)
    
    def connect_time_main(message: Annotated[str, "The message time agent want to send to main agent"]) -> str:
        message = f"Role: Time Agent\n Message: {message}"
        print(f'user_id {user_id}')
        assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user = user_agents[user_id]
        response = multi_role_agent.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)
        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = group_chat.messages[-2]
        #sending response to receiver agent
        send_message_to_user(user_id,last_message,'')
        
        text = f'The Response from main Agent: {last_message}'
        time_user.initiate_chat(time_agent,message=text)
        key = list(time_user.chat_messages.keys())[0]
        last_message = user_proxy.chat_messages[key][-1]['content'].replace('TERMINATE','')
        send_message_to_user(user_id,last_message,'')
        return 'Done'
        
    # Register the tool signature with the assistant agent.
    time_agent.register_for_llm(name="Connect_to_main_agent", description="Connects time agent to main assistant agemt to perform actions which time agent cannot perform")(connect_time_main)

    # Register the tool function with the user proxy agent.
    time_user.register_for_execution(name="Connect_to_main_agent")(connect_time_main)  
    
    assistant.description = 'Agent that is designed to do some specific tasks'
    user_proxy.description = 'Agent will act as user & perform task assigned to user'
    helper.description = 'helps assistant agent to call functions'
    multi_role_agent.description = 'Never call this agent it will act as a external agent'
    executor.description = 'this is an executor agent that Specialized agent for code execution & response handling'
    
    
    def state_transition(last_speaker, groupchat):
        messages = groupchat.messages
        current_app.logger.info(f'Inside state_transition with message :10 {messages[-1]["content"][:10]} & last_speaker {last_speaker.name}')
        if last_speaker == user_proxy or last_speaker == multi_role_agent or last_speaker == helper:
            return assistant
        if 'exitcode:' in messages[-1]["content"]:
            current_app.logger.info('Got exitcode in text returning assistant')
            return assistant
        if 'TERMINATE' in messages[-1]["content"].upper():
            current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        return "auto"
        
    group_chat = autogen.GroupChat(
        agents=[assistant, helper, user_proxy,multi_role_agent,executor],
        messages=[],
        # messages_per_round=15,
        # speaker_selection_method="auto",  # using an LLM to decide
        speaker_selection_method=state_transition,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=True
    )
    
    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"config_list": config_list}
    )
    

    return assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user

def get_agent_response(assistant: autogen.AssistantAgent, user_proxy: autogen.UserProxyAgent,manager: autogen.GroupChatManager,group_chat:autogen.GroupChat, message: str) -> str:
    """Get a single response from the agent for the given message."""
    try:

        response = user_proxy.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)
        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = group_chat.messages[-2]
        return last_message

    except Exception as e:
        current_app.logger.info(f'Got some error {e}')
        return f"Error getting response: {str(e)}"


recent_file_id = {}
recipes = {}
def chat_agent(user_id,text,prompt_id,file_id):
    current_app.logger.info('--'*100)
    user_message = text
    try:
        if file_id:
            recent_file_id[user_id] = file_id

        # Get or create agents for this user
        if user_id not in user_agents:
            if user_id not in user_journey:
                role_agents[user_id] = create_agents_for_role(user_id,prompt_id)
                assistant, user_proxy, group_chat, manager, helper, stop = role_agents[user_id]
                if stop:
                    user_journey[user_id] = 'UseBot'
                else:
                    user_journey[user_id] = 'Roles'
            if user_journey[user_id] == 'UseBot':
                with open(f"prompts/{prompt_id}_recipe.json", 'r') as f:
                    config = json.load(f)
                    try:
                        if 'scheduled_tasks' in config and len(config['scheduled_tasks'])>0:
                            current_app.logger.info('Creating scheduled tasks')
                            trigger = CronTrigger.from_crontab(config['scheduled_tasks'][0]['cron_expression'])
                            job_id = f"job_{int(time.time())}"
                            scheduler.add_job(execute_python_file, trigger=trigger, id=job_id,args=[config['scheduled_tasks'][0]['job_description'],user_id])
                            current_app.logger.info('Successfully created scheduler job')
                    except Exception as e:
                        current_app.logger.error(f'Some Error in creating scheduled tasks error:{e}')
                    recipes[prompt_id] = config
                user_agents[user_id] = create_agents_for_user(user_id,prompt_id)
                user_journey[user_id] = 'UseBot'
        if user_journey[user_id] == 'Roles':
            assistant, user_proxy, group_chat, manager, helper, stop = role_agents[user_id]
            response = user_proxy.initiate_chat(manager, message=user_message,speaker_selection={"speaker": "assistant"}, clear_history=False)
            last_message = group_chat.messages[-1]
            if 'terminate' in last_message['content'].lower():
                last_message = group_chat.messages[-2]
                user_journey[user_id] = 'UseBot'
                return 'Role updated Successfully use the bot now'
            
            return last_message['content']
        else:
            assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user = user_agents[user_id]

            prompt_id = int(prompt_id)
            response = get_agent_response(assistant, user_proxy,manager,group_chat, user_message)
            return response
    except Exception as e:
        current_app.logger.info(f'Some ERROR IN REUSE RECIPE {e}')
        raise
    
def crossbar_multiagent(msg):
    current_app.logger.info("insde crossbar_multiagent")
    current_app.logger.info('--'*100)
    assistant, user_proxy, group_chat, manager, helper, multi_role_agent = user_agents[msg['user_id']]
    message = f"Role: {msg['caller_role']}\n Message: {msg['message']}"
    response = multi_role_agent.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]
        
    #sending response to receiver agent
    send_message_to_user(msg['user_id'],last_message,msg['message'])
    
    assistant, user_proxy, group_chat, manager, helper, multi_role_agent = user_agents[msg['caller_user_id']]
    message = f"Role: {msg['role']}\n Message: {last_message}"
    response = multi_role_agent.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]
    
    #sending response to caller agent
    send_message_to_user(msg['caller_user_id'],last_message,msg['message'])
