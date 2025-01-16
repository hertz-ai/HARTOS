from typing import Dict, Tuple
import autogen
import os
import requests
import uuid
import time
import re
from datetime import datetime
from typing_extensions import Literal
from typing import Annotated, Optional
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
from autogen import ConversableAgent, register_function, runtime_logging
import requests
from flask import current_app

from autogen.agentchat.contrib.capabilities import transform_messages, transforms
from autogen.cache.in_memory_cache import InMemoryCache


client = Client('http://aws_rasa.hertzai.com:8088/publish')
scheduler = BackgroundScheduler()
scheduler.start()
# logging_session_id = runtime_logging.start(config={"dbname": "logs.db"})
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
agent_data = {77:{'horror_story_1': "Narrator 1 (Avatar ID 1983):\n1. It was a dark and stormy night, the wind howling ominously through the trees. Emily and Jack decided to take shelter in the old, abandoned mansion at the edge of town.\n\nNarrator 2 (Avatar ID 1980):\n2. As they stepped inside, the door slammed shut behind them with a loud bang. The temperature dropped instantly, and a chill ran down their spines. Emily's flashlight flickered, casting eerie shadows on the cobweb-covered walls.\n\nNarrator 1 (Avatar ID 1983):\n3. They heard whispers, faint at first but growing louder. Jack's curiosity got the better of him, and he ventured deeper into the mansion, leaving Emily behind.\n\nNarrator 2 (Avatar ID 1980):\n4. Suddenly, a blood-curdling scream pierced the air. Emily ran towards the sound, only to find Jack's flashlight on the floor, flickering. Jack was nowhere to be seen. The whispers grew louder, closing in on Emily, until everything went dark.", 'new_story_1': "Narrator 1 (Avatar ID 1983):\n1. Clara stood at the edge of the cliff, the ocean waves crashing below her. The breeze was cool and salty, whispering secrets of long-forgotten tales.\n\nNarrator 2 (Avatar ID 1980):\n2. She had come here searching for answers, for a sign that everything would be alright. In her hand, she clutched a letter, its contents promising hope and a new beginning.\n\nNarrator 1 (Avatar ID 1983):\n3. As Clara unfolded the letter, her eyes widened. It wasn't just a message of hope; it was a map, leading to a hidden treasure deep within the forest.\n\nNarrator 2 (Avatar ID 1980):\n4. With newfound determination, Clara turned away from the cliffs and began her journey, her heart beating with the promise of adventure and discovery."}}
# config_list = [{
#     "model": 'gpt-4o',
#     "api_type": "azure",
#     "api_key": '4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf',
#     "base_url": 'https://hertzai-gpt4.openai.azure.com/',
#     "api_version": "2024-02-15-preview",
#     "price": [0.0025, 0.01]
# }]

config_list = [{
    "model": "gpt-4o-mini",
    "api_type": "azure",
    "api_key": "4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf",
    "base_url": "https://hertzai-gpt4.openai.azure.com/",
    "api_version": "2024-02-15-preview",
    "price":[0.00015,0.0006]
}]


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
        api_key='eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.J8GYPZN-tVnkiTnS5tyjpQ9FdohZKZo_s5CgasXOqSU',
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
    config_list = [{
        "model": 'gpt-4o',
        "api_type": "azure",
        "api_key": '4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf',
        "base_url": 'https://hertzai-gpt4.openai.azure.com/',
        "api_version": "2024-02-15-preview",
        "price": [0.0025, 0.01]
    }]
    current_app.logger.info('INSIDE create_agents_for_role')

    # Create a basic function calling config
    llm_config = {
        "config_list": config_list,
        "cache_seed": None,
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
        if user askes to create new chat then call the "update_persona" tool to update the records in db & return TERMINATE
        if they want to join an existing chat then ask the user to give the main user's contact number & then call the "update_persona" tool to update the records in db & return TERMINATE
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
            
        select_speaker_compression_args = dict(
            model_name="microsoft/llmlingua-2-xlm-roberta-large-meetingbank", use_llmlingua2=True, device_map="cpu"
        )
        select_speaker_transforms = transform_messages.TransformMessages(
            transforms=[
                transforms.MessageHistoryLimiter(max_messages=5),
                transforms.TextMessageCompressor(
                    min_tokens=1000,
                    text_compressor=transforms.LLMLingua(select_speaker_compression_args, structured_compression=True),
                    cache=InMemoryCache(seed=43),
                    filter_dict={"role": ["system"], "name": ["ceo", "checking_agent"]},
                    exclude_filter=True,
                ),
                transforms.MessageTokenLimiter(max_tokens=3000, max_tokens_per_message=500, min_tokens=300),
            ]
        )
        group_chat = autogen.GroupChat(
            agents=[assistant, helper, user_proxy],
            messages=[],
            max_round=20,
            select_speaker_transform_messages=select_speaker_transforms,
            speaker_selection_method=state_transition,  # using an LLM to decide
            allow_repeat_speaker=False,  # Prevent same agent speaking twice
            send_introductions=True
        )
        
        manager = autogen.GroupChatManager(
            groupchat=group_chat,
            llm_config={"cache_seed": None,"config_list": config_list}
        )
        
        
        

        return assistant, user_proxy, group_chat, manager, helper,False
    else:
        agents_session[f"{user_id}_{prompt_id}"] = [{'agentInstanceID':f'com.hertzai.hevolve.chat.{prompt_id}.{user_id}',
                                                'user_id':user_id,'role':personas[0]['name'],'deviceID':'something'}]
        
        agents_roles[f"{user_id}_{prompt_id}"] = {user_id:personas[0]['name']}
        return 'TERMINATE','TERMINATE','TERMINATE','TERMINATE','TERMINATE', True



def create_agents_for_user(user_id: str,prompt_id) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
    """Create new assistant & user proxy agents for a user with basic configuration."""

    # Create a basic function calling config
    llm_config = {
        "config_list": config_list,
        "cache_seed": None
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
        current_app.logger.info(f'Got role as {role}')
    goal = ''
    with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
            goal = config['goal']
    current_app.logger.info(f'Got goal as {goal}')
    role_actions = []
    current_app.logger.info(f'Getting role actions')
    for i in recipes[prompt_id]['steps']:
        current_app.logger.info(f'this is action persona:{i["persona"]} ')
        if i['persona'].lower() == role.lower():
            
            role_actions.append(i)
    current_app.logger.info(f'role_actions: {role_actions}')
    
    if len(role_actions) == 0:
        role_actions = recipes[prompt_id]['steps']
    individual_recipe = []
    for i in range(1,(len(recipes[prompt_id]['steps']))):
        current_app.logger.info(f'checking for prompts/{prompt_id}_{i}.json')
        try:
            with open(f"prompts/{prompt_id}_1.json", 'r') as f:
                config = json.load(f)
                individual_recipe.append(config)
        except Exception as e:
            current_app.logger.error(f'Got error as :{e} while checking for prompts/{prompt_id}_{i}.json')
    response_format = {'message_2_user': 'Your message here'}
    agent_prompt = f'''You are a Helpful {role} Assistant. Follow the actions below to assist the user:
        1. Try to complete a task on your own If you are unable to perform a specific task, ask the helper agent for assistance.
        2. Only follow actions where the persona is: {role}.
        3. Follow the steps below to achieve the goal: {goal}.
        4. Use the provided Recipe for more details related to the actions.
        5. Keep track of action and only go to text action when the current action is completed successfully
        6. Always use steps/code from recipe given below
        7. If there is any action which is like to perform a task continously or time based or scheduled activity you should not perform this action is already taken care of.
        8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
        9. Tools Helper Agent can use [txt2img, img2txt, save_data_in_memory, get_data_from_memory, get_user_id, get_prompt_id, Generate_video, get_user_uploaded_file, get_user_camera_inp, get_chat_history, create_scheduled_jobs] if you have any task which is not doable by these tool check recipe first else create python code to do so
        10. Do not mention anything related to action or get confirmation from user if not needed.
        11. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: @{role} {response_format}
        
        Actions: <actionsStart>{role_actions}<actionEnd>
        Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>
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
            4. Tools you have [txt2img, img2txt, save_data_in_memory, get_data_from_memory, get_user_id, get_prompt_id, Generate_video, get_user_uploaded_file, get_user_camera_inp, get_chat_history, create_scheduled_jobs] if you have any task which is not doable by these tool check recipe first else create python code to do so
            5. Keep track of action and only go to text action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: @{role} {response_format}
            
            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>            
            
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
            4. Tools Helper Agent can use [txt2img, img2txt, save_data_in_memory, get_data_from_memory, get_user_id, get_prompt_id, Generate_video, get_user_uploaded_file, get_user_camera_inp, get_chat_history, create_scheduled_jobs] if you have any task which is not doable by these tool check recipe first else create python code to do so
            5. Keep track of action and only go to text action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: @{role} {response_format}
            
            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>
        
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
    # @executor.register_for_execution()
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
    # 
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Text to image Creator")
    def txt2img(text: Annotated[str, "Text to create image"]) -> str:
        current_app.logger.info('INSIDE txt2img')
        url = f"http://aws_rasa.hertzai.com:5459/txt2img?prompt={text}"

        payload = ""
        headers = {}

        response = requests.post(url, headers=headers, data=payload)
        return response.json()['img_url']
       
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Image to Text/Question Answering from image")
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
    
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Use this to Store and retrieve data using key-value storage system")
    def save_data_in_memory(key: Annotated[str, "Key for storing/retrieving data"],
                    value: Annotated[Optional[str], "Value you want to store"] = None) -> str:
        current_app.logger.info('INSIDE save_data_in_memory')
        agent_data[prompt_id][key] = value
        return f'{agent_data[prompt_id]}'
    
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Returns all data from the internal Memory")
    def get_data_from_memory() -> str:
        current_app.logger.info(f'INSIDE get_all_data with prompt_id {prompt_id}')
        current_app.logger.info(f'data from get_all_data {agent_data[prompt_id]}')
        return f'{agent_data[prompt_id]}'
 
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Returns the unique identifier (user_id) of the current user.")
    def get_user_id() -> str:
        current_app.logger.info('INSIDE get_user_id')
        return f'{user_id}'
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Returns the unique identifier (prompt_id) associated with the current prompt or conversation.")
    def get_prompt_id() -> str:
        current_app.logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Generate video with text and save it in database")
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

    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="get user's recent uploaded files")
    def get_user_uploaded_file() -> str:
        current_app.logger.info('INSIDE get_user_uploaded_file')
        if recent_file_id[user_id]:
            return f'Got user uploaded file the file_id is {recent_file_id[user_id]}'

        return 'No file uploaded from user'
    
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Get user's visual information to process somethings")
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
    
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Get Chat history based on text & start & end date")
    def get_chat_history(text: Annotated[str, "Text related to which you want history"],start: Annotated[str, "start date in format %Y-%m-%dT%H:%M:%S.%fZ"],end: Annotated[str, "end date in format %Y-%m-%dT%H:%M:%S.%fZ"]) -> str:
        current_app.logger.info('INSIDE get_chat_history')
        return get_time_based_history(text, f'user_{user_id}', start, end)
    
    
    @assistant.register_for_execution()
    @helper.register_for_llm(api_style="function",description="Creates time-based jobs using APScheduler to schedule jobs")
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
        
    # Let's first define the assistant agent that suggests tool calls. TODO add recipe here
    time_agent = ConversableAgent(
        name="time",
        system_message="You are an helpful AI assistant used to perform time based tasks given to you. "
        f"""You can refer below details to perform task:
            Actions: <actionsStart>{role_actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{individual_recipe}<generalized_functionsEnd><recipeEnd>
        
        """
        f"When you want to communicate with {role} connect main agent using 'connect_time_main' tool."
        "Return 'TERMINATE' when the task is done.",
        llm_config=llm_config,
    )

    # The user proxy agent is used for interacting with the assistant agent
    # & executes tool calls.
    time_user = ConversableAgent(
        name="executor",
        llm_config=False,
        is_termination_msg=lambda msg: msg.get("content") is not None and "TERMINATE" in msg["content"],
        code_execution_config={"work_dir": "coding", "use_docker": False},
        human_input_mode="NEVER",
    )
    
    ##Tools call
    time_agent.register_for_llm(name="txt2img", description="Text to image Creator")(txt2img)
    time_user.register_for_execution(name="txt2img")(txt2img)
    time_agent.register_for_llm(name="img2txt", description="Image to Text/Question Answering from image")(img2txt)
    time_user.register_for_execution(name="img2txt")(img2txt)  
    time_agent.register_for_llm(name="save_data_in_memory", description="Use this to Store and retrieve data using key-value storage system")(save_data_in_memory)
    time_user.register_for_execution(name="save_data_in_memory")(save_data_in_memory)  
    time_agent.register_for_llm(name="get_data_from_memory", description="Returns all data from the internal Memory")(get_data_from_memory)
    time_user.register_for_execution(name="get_data_from_memory")(get_data_from_memory)  
    time_agent.register_for_llm(name="get_user_id", description="Returns the unique identifier (user_id) of the current user.")(get_user_id)
    time_user.register_for_execution(name="get_user_id")(get_user_id)  
    time_agent.register_for_llm(name="get_prompt_id", description="Returns the unique identifier (prompt_id) associated with the current prompt or conversation.")(get_prompt_id)
    time_user.register_for_execution(name="get_prompt_id")(get_prompt_id)  
    time_agent.register_for_llm(name="Generate_video", description="Generate video with text and save it in database")(Generate_video)
    time_user.register_for_execution(name="Generate_video")(Generate_video)  
    time_agent.register_for_llm(name="get_user_uploaded_file", description="get user's recent uploaded files")(get_user_uploaded_file)
    time_user.register_for_execution(name="get_user_uploaded_file")(get_user_uploaded_file)  
    time_agent.register_for_llm(name="get_user_camera_inp", description="Get user's visual information to process somethings")(get_user_camera_inp)
    time_user.register_for_execution(name="get_user_camera_inp")(get_user_camera_inp)  
    time_agent.register_for_llm(name="create_scheduled_jobs", description="Creates time-based jobs using APScheduler to schedule jobs")(create_scheduled_jobs)
    time_user.register_for_execution(name="create_scheduled_jobs")(create_scheduled_jobs)  
    time_agent.register_for_llm(name="get_chat_history", description="Get Chat history based on text & start & end date")(get_chat_history)
    time_user.register_for_execution(name="get_chat_history")(get_chat_history)  
    
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
    
    assistant.description = 'Designed to handle specific tasks by interacting directly with other agents or the user. It acts as the primary orchestrator for task management and ensures tasks are completed efficiently'
    user_proxy.description = 'Acts as a user, performing tasks assigned by the Assistant Agent. It simulates user actions and provides results or feedback as required.'
    helper.description = 'Assists the Assistant Agent by handling function (txt2img, img2txt, save_data_in_memory, get_data_from_memory, get_user_id, get_prompt_id, Generate_video, get_user_uploaded_file, get_user_camera_inp, get_chat_history, create_scheduled_jobs) calls and supporting backend processes. '
    multi_role_agent.description = 'Acts as an external agent with multi-functional capabilities. Note: This agent should never be directly invoked.'
    executor.description = 'A specialized agent responsible for executing code and handling response management. It ensures computational tasks are performed accurately and returns results effectively.'
    
    
    def state_transition(last_speaker, groupchat):
        messages = groupchat.messages
        current_app.logger.info(f'Inside state_transition with message :10 {messages[-1]["content"][:10]} & last_speaker {last_speaker.name}')
        if last_speaker.name == f"user_proxy_{user_id}" or last_speaker.name == 'multi_role_agent' or last_speaker.name == 'helper' or last_speaker.name == 'Executor':
            return assistant
        if 'exitcode:' in messages[-1]["content"]:
            current_app.logger.info('Got exitcode in text returning assistant')
            return assistant
        if 'TERMINATE' in messages[-1]["content"].upper():
            current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        return "auto"
        
    select_speaker_compression_args = dict(
        model_name="microsoft/llmlingua-2-xlm-roberta-large-meetingbank", use_llmlingua2=True, device_map="cpu"
    )
    select_speaker_transforms = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=10),
            transforms.TextMessageCompressor(
                min_tokens=1000,
                text_compressor=transforms.LLMLingua(select_speaker_compression_args, structured_compression=True),
                cache=InMemoryCache(seed=43),
                filter_dict={"role": ["system"], "name": ["ceo", "checking_agent"]},
                exclude_filter=True,
            ),
            transforms.MessageTokenLimiter(max_tokens=3000, max_tokens_per_message=500, min_tokens=300),
        ]
    )
    group_chat = autogen.GroupChat(
        agents=[assistant, helper, user_proxy,multi_role_agent,executor],
        messages=[],
        max_round=20,
        select_speaker_transform_messages=select_speaker_transforms,
        speaker_selection_method=state_transition,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=True
    )
    
    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"cache_seed": None,"config_list": config_list}
    )
    

    return assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user

def get_agent_response(assistant: autogen.AssistantAgent, user_proxy: autogen.UserProxyAgent,manager: autogen.GroupChatManager,group_chat:autogen.GroupChat, message: str) -> str:
    """Get a single response from the agent for the given message."""
    try:

        result = user_proxy.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)
        # Print the chat summary
        current_app.logger.info("\n=== Chat Summary ===")
        current_app.logger.info(result.summary)

        current_app.logger.info("\n=== Full response ===")
        current_app.logger.info(result)
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
                if prompt_id not in agent_data.keys():
                    agent_data[prompt_id] = {}
                role_agents[user_id] = create_agents_for_role(user_id,prompt_id)
                assistant, user_proxy, group_chat, manager, helper, stop = role_agents[user_id]
                if stop:
                    user_journey[user_id] = 'UseBot'
                else:
                    user_journey[user_id] = 'Roles'
            if user_journey[user_id] == 'UseBot':
                with open(f"prompts/{prompt_id}_recipe.json", 'r') as f:
                    config = json.load(f)
                    recipes[prompt_id] = config
                    try:
                        if 'scheduled_tasks' in config and len(config['scheduled_tasks'])>0:
                            current_app.logger.info('Creating scheduled tasks')
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
                            # if role and config['scheduled_tasks'][0]['persona'].lower() == role.lower():
                            #     trigger = CronTrigger.from_crontab(config['scheduled_tasks'][0]['cron_expression'])
                            #     job_id = f"job_{int(time.time())}"
                            #     scheduler.add_job(execute_python_file, trigger=trigger, id=job_id,args=[config['scheduled_tasks'][0]['job_description'],user_id])
                            #     current_app.logger.info('Successfully created scheduler job')
                    except Exception as e:
                        current_app.logger.error(f'Some Error in creating scheduled tasks error:{e}')
                    recipes[prompt_id] = config
                user_agents[user_id] = create_agents_for_user(user_id,prompt_id)
                user_journey[user_id] = 'UseBot'
        if user_journey[user_id] == 'Roles':
            assistant, user_proxy, group_chat, manager, helper, stop = role_agents[user_id]
            result = user_proxy.initiate_chat(manager, message=user_message,speaker_selection={"speaker": "assistant"}, clear_history=False)
            # Print the chat summary
            current_app.logger.info("\n=== Chat Summary ===")
            current_app.logger.info(result.summary)

            
            # Print the full chat history
            current_app.logger.info("\n=== Full response ===")
            current_app.logger.info(result)
            
            last_message = group_chat.messages[-1]
            if 'terminate' in last_message['content'].lower():
                with open(f"prompts/{prompt_id}_recipe.json", 'r') as f:
                    config = json.load(f)
                    recipes[prompt_id] = config
                user_agents[user_id] = create_agents_for_user(user_id,prompt_id)
                assistant, user_proxy, group_chat, manager, helper, multi_role_agent, time_agent, time_user = user_agents[user_id]
                user_journey[user_id] = 'UseBot'
                message = "let's perform the actions availabe in sequence\nIMP instruction: If you want to ask something or send something to the user, always use this format: @user {'message_2_user': 'Your message here'}"
                result = helper.initiate_chat(manager, message=message,speaker_selection={"speaker": "assistant"}, clear_history=False)
                # Print the chat summary
                current_app.logger.info("\n=== Chat Summary ===")
                current_app.logger.info(result.summary)

                current_app.logger.info("\n=== Full response ===")
                current_app.logger.info(result)
                last_message = group_chat.messages[-1]
                if last_message['content'] == 'TERMINATE':
                    last_message = group_chat.messages[-2]
                return last_message
        
            
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
