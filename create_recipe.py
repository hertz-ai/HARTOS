import autogen
import os
from typing import Annotated, Optional, Dict, Tuple, Any
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import requests
from autobahn.asyncio.component import Component, run
import uuid
import asyncio
import traceback
from datetime import datetime
import time
from autogen.coding import DockerCommandLineCodeExecutor
import re
from autogen import register_function
import json
from autogen import ConversableAgent
from flask import current_app
from helper import topological_sort, fix_json, retrieve_json, fix_actions, Action, ToolMessageHandler, strip_json_values
import helper as helper_fun
import threading
from autogen.agentchat.contrib.capabilities import transform_messages, transforms
from autogen.cache.in_memory_cache import InMemoryCache
from json_repair import repair_json
from crossbarhttp import Client
client = Client('http://aws_rasa.hertzai.com:8088/publish')

scheduler = BackgroundScheduler()
scheduler.start()

user_agents: Dict[str, Tuple[autogen.ConversableAgent, autogen.ConversableAgent]] = {}
time_agents = {}

config_list = [{
        "model": 'gpt-4.1',
        "api_type": "azure",
        "api_key": '8MMPerfdfcpx63VfIVtg2lpAK7Crv7O5JKiKwhusVhgJNkC8Ql6FJQQJ99BAACHYHv6XJ3w3AAABACOGdxWW',
        "base_url": 'https://hertzai-gpt4.openai.azure.com/openai/deployments/gpt-4.1/chat/completions?api-version=2025-01-01-preview',
        "api_version": "2024-12-01-preview",
        "price": [0.0025, 0.01]
    }]

agent_data = {}
task_time = {}
agent_metadata = {}
final_recipe = {}
individual_json = {}
time_actions = {}
scheduler_check = {}



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


def send_message_to_user1(user_id,response,inp,prompt_id):
    user_prompt = f'{user_id}_{prompt_id}'
    request_id = f'{request_id_list[user_prompt]}-intermediate'
    url = 'http://aws_rasa.hertzai.com:9890/autogen_response'
    body = json.dumps({'user_id':user_id,'message':response,'inp':inp,'request_id':request_id})
    headers = {'Content-Type': 'application/json'}
    res = requests.post(url,data=body,headers=headers)
    


def execute_python_file(task_description:str,user_id: int,prompt_id:int,action_entry_point:int=0):
    headers = {'Content-Type': 'application/json'}
    url = 'http://localhost:6777/time_agent'
    data = json.dumps({'task_description':task_description,'user_id':user_id,'prompt_id':prompt_id,'action_entry_point':action_entry_point,'request_from':'Reuse'})
    res = requests.post(url,data=data,headers=headers)
    return 'done'

def time_based_execution(task_description:str,user_id: int,prompt_id:int,action_entry_point:int,actions:list=[]):
    current_app.logger.info(f'INSIDE TIME_BASED_EXECUTION with action_entry_point"{action_entry_point}')
    user_prompt = f'{user_id}_{prompt_id}'
    if user_prompt not in time_agents:
        time_agents[user_prompt] = create_time_agents(user_id,prompt_id,'creator','',actions)
    
    # author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_id]
    current_time = datetime.now()
    group_chat = time_agents[user_prompt]['time_group_chat']
    time_user = time_agents[user_prompt]['time_user']
    time_manager = time_agents[user_prompt]['time_manager']
    chat_instructor = time_agents[user_prompt]['chat_instructor1']
    time_actions[user_prompt].current_action = action_entry_point
    current_action = time_actions[user_prompt].get_action_byaction_id(action_entry_point)['action']
    text = f'This is the time now {current_time}\n your overall task description which might span multiple actions: {task_description}\n the current Action to execute: {current_action}'
    result = time_user.initiate_chat(time_manager, message=text,speaker_selection={"speaker": "assistant"}, clear_history=False)
    restart = False
    while True:
        current_app.logger.info('inside while')
        if group_chat.messages[-1]['name'] == 'ChatInstructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
            current_app.logger.info(f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content'][:10]}..")
            json_obj = retrieve_json(group_chat.messages[-2]["content"])
            if json_obj and type(json_obj)==dict and 'status' in json_obj.keys() and json_obj['status'].lower() == 'completed':
                current_action = time_actions[user_prompt].get_action_byaction_id(time_actions[user_prompt].current_action)['action']
                text = f'This is the time now {current_time}\n your overall task description which might span multiple actions: {task_description}\n the current Action to execute: {current_action}'
            else:
                current_app.logger.warning(f'it is not a json object the error is:')
                current_app.logger.info('it is not a json object You should ask @statusverifier to give response in proper format & not move ahead to next action')
                actions_prompt = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action-1)
                text = f'Lets continue the work we were doing, if action is completed then ask @statusverifier Agent to Please tell the status of the action {user_tasks[user_prompt].current_action}:{actions_prompt}'
            
            result = chat_instructor.initiate_chat(time_manager, message=text,speaker_selection={"speaker": "assistant"}, clear_history=False)
            continue
        if restart == True:
            break
        current_app.logger.info(f'checking can_perform_without_user_input from {time_actions[user_prompt].get_action_byaction_id(action_entry_point)} ')
        if time_actions[user_prompt].get_action_byaction_id(action_entry_point)['can_perform_without_user_input'] == 'yes':
            restart = True
            text = 'You can assume things on your own to complete this task'
            result = chat_instructor.initiate_chat(time_manager, message=text,speaker_selection={"speaker": "assistant"}, clear_history=False)
            
            continue
        break
            
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]
    #sending response to receiver agent
    send_message_to_user1(user_id,last_message,task_description,prompt_id)
    return 'done'

from typing import List


class SubscriptionHandler:
    message = None

    async def on_rpc_response(self, session, msg):
        current_app.logger.info("Received RPC response: {}".format(msg))
        SubscriptionHandler.message = msg
        await component.stop()  # Stop the component after getting the response

async def subscribe_and_return(message,topic,time=8000):
    global component
    component = Component(
        transports="ws://aws_rasa.hertzai.com:8088/ws",
        realm="realm1",
    )

    @component.on_join
    async def join(session, details):
        current_app.logger.info("Making RPC Call...")
        try:
            response = await asyncio.wait_for(session.call(topic,message), timeout=time)
            await SubscriptionHandler().on_rpc_response(session, response)
            
        except Exception as e:
            current_app.logger.error(f"RPC call failed: {e}")
            SubscriptionHandler.message = None
        finally:
            await component.stop()

    await component.start()
    return SubscriptionHandler.message

llm_config = {
        "cache_seed": None,
        "config_list": config_list,
        "max_tokens": 1500
    }



def create_agents(user_id: str,task,prompt_id) -> Tuple[autogen.ConversableAgent, autogen.ConversableAgent]:
    """Create new assistant & user agents for a given user_id"""
    user_prompt = f'{user_id}_{prompt_id}'
    individual_json[user_prompt] = None
    
    custom_agents = []
    agents_object = {}
    with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
            list_of_persona = config['flows'][recipe_for_persona[user_prompt]]['persona']
            current_app.logger.info(f'WORKING persona as {list_of_persona}')
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
                ➜Understand and plan the current action execution.
                ➜Perform the action with the help of @Helper and @Executor agents.
                ➜Account for all the tools available with helper & whenever you are supposed to call a tool as part of current action ask @Helper.
                ➜If the action requires code execution or API endpoint call, in create code(python preffered) and ask @Executor agent to execute the created code.
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
                ➜If there are multiple personas, ask the @user to select the persona associated with each action.
        
        •Code Execution: Executor Agent: Executes code as needed. Ensure the final response is printed in code using print() before sending to Executor.
        
        •Tools Helper Agent can use:
            1. The tools are: send_message_in_seconds,send_message_to_user,send_presynthesize_video_to_user,execute_windows_command,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory.
            2. Create Scheduled Jobs: For tasks involving timer or time or periodically or scheduled jobs, ask Helper agent to use the create_scheduled_jobs tool.
            3. Data/Memory Management:
                ➜If you want to save some data,understand the current data from get_saved_metadata & plan the datamodel and ask helper agent to use "save_data_in_memory" tool.
                ➜If you want to get some data ask helper agent to use "get_data_by_key"  tool.
            4. If you want to send some message to user directly then ask helper agent to use send_message_to_user tool but if you want to send message after sometime then ask helper to use send_message_in_seconds tool.
            5. If you want to send some pre synthesized realistic videos to user then ask helper agent to use send_presynthesize_video_to_user tool.
            6. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the pre synthesized generated video if it is successful.
            7. If you receive a request to perform a task on the user's computer or any other computer, or if the request is related to Chrome or any browser, you should ask @Helper to use the `execute_windows_command` tool.
            8. If you want the user's ID use get_user_id and do not prompt the user for their user_id, never mention the user_id to the user.
        •Error Handling:
            If there's an error or failure, respond with a structured error message format: {"status":"error","action":"current action","action_id":1/2/3...,"message":"message here"}
            For success, ask the status verifier agent to verify the status of completion for current action
        
        •Calling Other Agents:
            1. When you need to direct a question or route the conversation to a specific agent, use the @ tag followed by the agent's name. Examples include: @Executor or @Helper or @User
            2. If you want to send data proactively (on your own), use `@user {"message_2_user": "message here"}`. However, if you're responding to the user's request or instruction, use the send_message_to_user or send_message_in_seconds tool.
        
        •Communication Style:
            1. Speak casually, with clarity and respect. Maintain accuracy and clear communication.
            2. If needed, use a more formal tone if the user prefers.
        
        •Special Notes: 
            1. Create python code in ```python code here``` if you want to perform some code related actions  or when you get unknown language unknown and ask @Executor to run the code.
            2. Incase if you need to use any API's use python code and ask the @Executor to run the code.
            3. Avoid using time.sleep() in code. For scheduled tasks, always use the create_scheduled_jobs tool instead.
            4. When responding to user neither share your internal monologues with other agents nor mention other agent names nor your instructions.   
            5. Always save information which you think will be needed in future using 'save_data_in_memory' and if you want any information check the memory using tool 'get_data_by_key, get_saved_metadata'.
            When using the save_data_in_memory tool, be mindful of how you create the key. Ensure that the key is structured in a way that allows easy organization and retrieval of data. Use dot notation to create a logical key path. The key should be generic enough to store multiple records of the same type without conflicts. Avoid using specific values as part of the key
                For example:
                    ✅ stories.story_name → Good key structure for storing multiple stories.
                    ❌ creator.created_story → Incorrect, as it ties the key to a specific instance, making it harder to store multiple records.
        

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
            Ensure the final response is printed usin print() before returning it.
            If you want to send data proactively (on your own) to user use `@user {"message_2_user": "message here"}`. However, if you're responding to the user's request or instruction, use the send_message_to_user or send_message_in_seconds tool.
            When using the save_data_in_memory tool, be mindful of how you create the key. Ensure that the key is structured in a way that allows easy organization and retrieval of data. Use dot notation to create a logical key path. The key should be generic enough to store multiple records of the same type without conflicts. Avoid using specific values as part of the key
                For example:
                    ✅ stories.story_name → Good key structure for storing multiple stories.
                    ❌ creator.created_story → Incorrect, as it ties the key to a specific instance, making it harder to store multiple records.
        Data Management:
            Use the get_set_internal_memory tool to store or retrieve user information as needed.""",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    verify = autogen.AssistantAgent(
        name="StatusVerifier",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=""""You are a Status Verification Agent in a multi-agent system.
        Role: Your primary responsibility is to track and verify the status of actions performed by other agents. You must provide updates strictly in JSON format with the following response structures:
        Response formats:
            1. Action Completed Successfully: {"status": "completed","action": "current action","action_id": 1/2/3...,"message": "message here","can_perform_without_user_input":"can you perform this action on your own without user input in future. only say no when it is absolutely mandatory and you cannot proceed without it, if you can proceed by checking with other agents you should say yes.  say yes/no if no they give the reason as well e.g. no-i need user's likes and dislike","persona_name":"persona name this action belongs to","fallback_action": "fallback action here"}  // If fallback_action is missing, ask the user: "What measures should be taken if this action fails in the future?" Include their response in fallback_action.
            2. Action Error: {"status": "error","action": "current action","action_id": 1/2/3...,"message": "message here"}
            3. Current Action Updated: {"status": "updated","action": "current action text","updated_action": "updated current action text","action_id": 1/2/3...,"message": "message here","persona_name":"persona name this action belongs to","fallback_action": ""} // If no fallback_action is provided, ask the user for measures to include.
            4. Action pending: {"status": "pending","action": "current action","action_id": 1/2/3...,"message": "what steps are pending message here"}
        Important Instructions:
            1. Strict Completion Criteria:
                i. Only mark an action as "completed" if all steps of the action have been successfully executed.
                ii. For pending or ongoing tasks, instruct the Assistant to complete them.
            2. Ensure Action Accuracy:
                i. Verify that the action was performed correctly as per instructions.
                ii. If the action was not executed correctly, return the original action to the Assistant.
            3. Maintain JSON Consistency:
                i. Always follow the exact JSON structure in your responses.
                ii. Do not perform actions yourself—only report status. 
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
            Ensure the final response is printed using print() before returning it.
            Do not hardcode or default case or a placeholder for exception or empty response cases when the functionality was not satisfied instead throw an error.

        Calling Other Agents:
            When you need to direct a question or route the conversation to a specific agent, use the @ tag followed by the agent's name. Examples include: @Executor or @Helper or @User
        Things You cannot do but Helper Agent can:
            1. Tools Helper Agent can use: Can use tools like send_message_in_seconds, send_message_to_user,send_presynthesize_video_to_user, execute_windows_command, text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory.
            2. Create Scheduled Jobs: For tasks involving timers or scheduled jobs, ask Helper agent to use the create_scheduled_jobs tool.
            3. Data/Memory Management:
                ➜If you want to save some data ask helper agent to use "save_data_in_memory" tool.
                ➜If you wnat to get some data ask helper agent to use "get_data_by_key", "get_saved_metadata" tool.
            4. If you want to send some message to user directly then ask helper agent to use send_message_to_user tool but if you want to send message after sometime then ask helper to use send_message_in_seconds tool.
            5. If you want to send some pre synthesized video to user then ask helper agent to use send_presynthesize_video_to_user tool.
            6. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            7. If you receive a request to perform a task on the user's computer or any other computer, or if the request is related to Chrome or any browser, you should ask @Helper to use the `execute_windows_command` tool."""
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
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(),
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
    
    # for i in config['personas']:
    #     name = i['name']
    #     name = autogen.UserProxyAgent(
    #         name=i['name'],
    #         human_input_mode="NEVER",
    #         default_auto_reply="TERMINATE",
    #         is_termination_msg=lambda x: True if x.get("content").strip()=='' else False,
    #         max_consecutive_auto_reply=0,
    #         code_execution_config=False,
    #     )
    #     name.description = i['description']
    #     custom_agents.append(name)
    #     agents_object[i['name']] = name
    
    helper.register_for_llm(name="text_2_image", description="Text to image Creator")(helper_fun.txt2img)
    assistant.register_for_execution(name="text_2_image")(helper_fun.txt2img)
    
    
    def camera_inp(inp: Annotated[str, "The Question to check from visual context"])->str:
        return helper_fun.get_user_camera_inp(inp,user_id)        
    helper.register_for_llm(name="get_user_camera_inp", description="Get user's visual information to process somethings")(camera_inp)
    assistant.register_for_execution(name="get_user_camera_inp")(camera_inp)  
    
    def save_data_in_memory(key: Annotated[str, "Key path for storing data now & retrieving data later. Use dot notation for nested keys (e.g., 'user.info.name')."],
                            value: Annotated[Optional[Any], "Value you want to store; may be int, str, float, bool, dict, list, json object."] = None) -> str:
        current_app.logger.info('INSIDE save_data_in_memory')
        current_app.logger.info(f"VALUES IN SAVE_DATA_IN_MEMORY: {value}")
        current_app.logger.info(f"VALUES ALREADY AVAILABLE IN AGENT DATA: {agent_data[prompt_id]}")
        keys = key.split('.')
        d = agent_data.setdefault(prompt_id, {})
        
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value
        return f'{agent_data[prompt_id]}'
    
    helper.register_for_llm(name="save_data_in_memory", description="Use this to Store and retrieve data using key-value storage system")(save_data_in_memory)
    assistant.register_for_execution(name="save_data_in_memory")(save_data_in_memory)
    
    def get_saved_metadata() -> str:
        stripped_json = strip_json_values(agent_data[prompt_id])
        return f'{stripped_json}'

    helper.register_for_llm(name="get_saved_metadata", description="Returns the schema of the json from internal memory with all keys but without actual values.")(get_saved_metadata)
    assistant.register_for_execution(name="get_saved_metadata")(get_saved_metadata)
    
    def get_data_by_key(key: Annotated[str, "Key path for retrieving data. Use dot notation for nested keys (e.g., 'user.info.name')."]) -> str:
        keys = key.split('.')
        d = agent_data.get(prompt_id, {})
        
        try:
            for k in keys:
                d = d[k]
            return f'{d}'
        except KeyError:
            return "Key not found in stored data."


    helper.register_for_llm(name="get_data_by_key", description="Returns all data from the internal Memory using key")(get_data_by_key)
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
                       avatar_id: Annotated[int, "Unique identifier for the avatar"],
                       realtime: Annotated[bool,"If True, response is fast but less realistic by default it should be true; if False, response is realistic but slower"]) -> str:
        print('INSIDE Generate_video')
        database_url = 'https://mailer.hertzai.com'
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        print(f"avtar_id: {avatar_id}:\n{text[:10]}....\n")
        
        headers = {'Content-Type': 'application/json'}
        data = {}
        data["text"] = text
        data['flag_hallo'] = 'false'
        data['chattts'] = False
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
            return f"Video Generation task added to queue with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
        else:
            return f"Video Generation completed with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
    
    helper.register_for_llm(name="Generate_video", description="Generate/presynthesize video with text and save it in database")(Generate_video)
    assistant.register_for_execution(name="Generate_video")(Generate_video)
    
    def get_user_uploaded_file() -> str:
        current_app.logger.info('INSIDE get_user_uploaded_file')
        if recent_file_id[user_id]:
            return f'Got user uploaded file the file_id is {recent_file_id[user_id]}'

        return 'No file uploaded from user'
    
    helper.register_for_llm(name="get_user_uploaded_file", description="get user's recent uploaded files")(get_user_uploaded_file)
    assistant.register_for_execution(name="get_user_uploaded_file")(get_user_uploaded_file)
    

    def img2txt(image_url: Annotated[str, "image url of which you want text"],text: Annotated[str, "the details you want from image"]='Describe the Images & Text data in this image in detail') -> str:
        current_app.logger.info('INSIDE img2txt')
        url = "http://azurekong.hertzai.com:8000/llava/image_inference"

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
    
    def create_scheduled_jobs(interval_sec: Annotated[int, "time between two Interval in seconds."], 
                            job_description: Annotated[str, "Description of the job to be performed"],
                            cron_expression: Annotated[Optional[str], "Cron expression for scheduling. Example: '0 9 * * 1-5' (Runs at 9:00 AM, Monday to Friday). If the interval is greater than 60 seconds or it needs to be executed at a dynamic cron time this argument is Mandatory else None"]=None) -> str:
        current_app.logger.info('INSIDE create_scheduled_jobs')
        
        # actual_execution_time = sum(task_time[prompt_id]['times'][-1])
        # if interval_sec < actual_execution_time:
        #     return f"Unable to create scheduled job for the specified interval because the actual execution time ({actual_execution_time} seconds) exceeds the interval between jobs ({interval_sec} seconds). Please use an interval longer than {actual_execution_time} seconds. Would you like to create a scheduled job with this updated interval?"
        
        # if not scheduler.running:
        #     scheduler.start()
        
        # try:
        #     if not interval_sec or int(interval_sec) >60:
        #         trigger = CronTrigger.from_crontab(cron_expression)
        #         job_id = f"job_{int(time.time())}"
        #         scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, int(user_id),int(prompt_id)])
        #         current_app.logger.info('Successfully created scheduler job')
        #         return 'Successfully created scheduler job'
        #     else:
        #         trigger = IntervalTrigger(seconds=int(interval_sec))
        #         job_id = f"job_{int(time.time())}"
        #         scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, int(user_id),int(prompt_id)])
        #         current_app.logger.info('Successfully created scheduler job')
        #         return 'Successfully created scheduler job'
        # except Exception as e:
        #     current_app.logger.error(f'Error in create_scheduled_jobs: {str(e)}')
        #     return f"Error creating scheduled job: {str(e)}"
        return 'Added this schedule job in creation process will do it at the end. you can go ahead and mark this action as completed.'
        
    helper.register_for_llm(name="create_scheduled_jobs", description="Creates time-based jobs using APScheduler to schedule jobs")(create_scheduled_jobs)
    assistant.register_for_execution(name="create_scheduled_jobs")(create_scheduled_jobs)
    
    def send_message_to_user(text: Annotated[str, "Text you want to send to the user"],
                         avatar_id: Annotated[Optional[str], "Unique identifier for the avatar"] = None,
                         response_type: Annotated[Optional[str], "Response mode: 'Realistic' (slower, better quality) or 'Realtime' (faster, lower quality)"] = 'Realtime') -> str:
        current_app.logger.info('INSIDE send_message_to_user')
        current_app.logger.info(f'SENDING DATA 2 user with values text:{text}, avatar_id:{avatar_id}, response_type:{response_type}')
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        #TODO add avatar_id and conv_id and response_type
        thread = threading.Thread(target=send_message_to_user1, args=(user_id, text, '',prompt_id))
        thread.start()
        return f'Message sent successfully to user with request_id: {request_id_list[user_prompt]}-intermediate'
    
    helper.register_for_llm(name="send_message_to_user", description="Sends a message/information to user. You can use this if you want to ask a question")(send_message_to_user)
    assistant.register_for_execution(name="send_message_to_user")(send_message_to_user)
    
    def send_presynthesize_video_to_user(conv_id: Annotated[str, "Conversation ID associated with the text from memory"]) -> str:
        current_app.logger.info('INSIDE send_presynthesize_video_to_user')
        current_app.logger.info(f'SENDING DATA 2 user with value: conv_id:{conv_id}.')
        return 'Message sent successfully to user'
    
    helper.register_for_llm(name="send_presynthesize_video_to_user", description="Sends a presynthesized message/video/dialogue to user using conv_id.")(send_presynthesize_video_to_user)
    assistant.register_for_execution(name="send_presynthesize_video_to_user")(send_presynthesize_video_to_user)
    
    def send_message_in_seconds(text: Annotated[str, "text to send to user"],
                       delay: Annotated[int, "time to wait in seconds before sending text"],
                       conv_id: Annotated[Optional[int], "conv_id for this text if not available make it None"],) -> str:
        current_app.logger.info('INSIDE send_message_in_seconds')
        current_app.logger.info(f'with text:{text}. and waiting time: {delay} conv_id: {conv_id}')
        run_time = datetime.fromtimestamp(time.time() + delay)
        scheduler.add_job(send_message_to_user1, 'date', run_date=run_time, args=[user_id, text, '',prompt_id])
        return 'Message scheduled successfully'
    
    helper.register_for_llm(name="send_message_in_seconds", description="Sends a presynthesized message/video/dialogue to user using conv_id with a timer.")(send_message_in_seconds)
    assistant.register_for_execution(name="send_message_in_seconds")(send_message_in_seconds)
    
    def get_chat_history(text: Annotated[str, "Text related to which you want history"],
                         start: Annotated[Optional[str], "start date in format %Y-%m-%dT%H:%M:%S.%fZ"] = None,
                         end: Annotated[Optional[str], "end date in format %Y-%m-%dT%H:%M:%S.%fZ"] = None) -> str:
        current_app.logger.info('INSIDE get_chat_history')
        return helper_fun.get_time_based_history(text, f'user_{user_id}', start, end)
    helper.register_for_llm(name="get_chat_history", description="Get Chat history based on text & start & end date")(get_chat_history)
    assistant.register_for_execution(name="get_chat_history")(get_chat_history)
    
    def google_search(text: Annotated[str, "Text/Query which you want to search"]) -> str:
        current_app.logger.info('INSIDE google search')
        return helper_fun.top5_results(text)
    helper.register_for_llm(name="google_search", description="web/google/bing search api tool for a given query")(google_search)
    assistant.register_for_execution(name="google_search")(google_search)
    
    def get_user_details()->str:
        current_app.logger.info('INSIDE get user details')
        return helper_fun.parse_user_id(user_id)
    helper.register_for_llm(name="get_user_details", description="Get User details like name, dob, gender")(get_user_details)
    assistant.register_for_execution(name="get_user_details")(get_user_details)
    
    async def execute_windows_command(instructions: Annotated[str, "Command in plain English to execute on the Windows machine"])->str:
        """
        Executes a command on a Windows machine and returns the response within 500 seconds.
        """
        try:
            current_app.logger.info('INSIDE execute_windows_command')
            topic = f'com.hertzai.hevolve.action.{user_id}'
            current_app.logger.info(f'calling {topic} for 5 second')
            response = await subscribe_and_return({'prompt_id':prompt_id},topic,5)  # Wait for the RPC response
            current_app.logger.info(f'Response from call of {topic}: {response}')
            if not response:
                return 'Ask user to to go to hertzai.com login and start the windows companion app'
            crossbar_message = {
                'parent_request_id': request_id_list[user_prompt],
                'user_id': f'{user_id}',
                'prompt_id': '54',
                'instruction_to_vlm_agent': instructions,
                'os_to_control': 'Windows',
                'actions_available_in_os': [],
                'max_ETA_in_seconds': 500,
                'langchain_server':True
            }
            topic = 'com.hertzai.hevolve.action'
            current_app.logger.info(f'calling {topic} for 8000 second')
            response = await subscribe_and_return(crossbar_message,topic)  # Wait for the RPC response
            current_app.logger.info(f'THIS IS RESPONSE type: {type(response)} value: {response}')
            if response['status'] == 'success':
                return 'successfully ran the command in user\' computer.'
            else:
                return 'Not able to perform this action now please try later'
        except Exception as e:
            error_message = traceback.format_exc()  # Capture full traceback
            current_app.logger.error(f"Error executing command:\n{error_message}")
            return {"error": e}

    helper.register_for_llm(name="execute_windows_command", description="Processes user-defined commands on a personal Windows or Android system.")(execute_windows_command)
    assistant.register_for_execution(name="execute_windows_command")(execute_windows_command)

    
    assistant.description = 'this is an assistant agent that coordinates & executes requested tasks & actions'
    executor.description = 'this is an executor agent that Specialized agent for code execution & response handling'
    author.description = 'this is an author/user agent that focused on user support, error resolution, contextual information. Contact this agent when you need any user based information or persona based information or if you want to say something to user'
    chat_instructor.description = 'this is a ChatInstructor agent that provides step-by-step action plans for task execution'
    helper.description = 'this is a helper agent that calls tools, facilitates task completion & assists other agents'
    verify.description = 'this is a verify status agent. which will verify the status of current action that will be called after ChatInstructor gives instruction to complete an action & assistant completes it, this agent will provide updates in a structured JSON format & then call user agent'
    
    def state_transition(last_speaker, groupchat):
        current_app.logger.info(f'Inside state_transition with actions {user_tasks[user_prompt].current_action}')
        current_app.logger.info(f"STATE_TRANSITION - Message[0]: {groupchat.messages[0]}")
        current_app.logger.info(f"STATE_TRANSITION - Message[1]: {groupchat.messages[1]}")
        
        messages = groupchat.messages
        new_role = 'user'
        if messages[-1]['name'] != 'UserProxy':
            new_role = 'AI'
        helper_fun.history(user_id,prompt_id,new_role,messages[-1]['content'])
        
        # if len(groupchat.messages) == 5:
        #     current_app.logger.info('THE LENGTH OF MESSAGES IS 5 APPENDING tool at start')
        #     groupchat.messages.insert(0,{'role':'tool','name':'Assistant','content':'','tool_responses':''})
        # if len(messages) % 10 == 0 or messages[-1]['name'] == 'UserProxy':
        #     current_app.logger.info('CHECKING FOR VIDEO FOR PAST 5MINS')
        #     visual_context = helper_fun.get_visual_context(user_id)
        #     current_app.logger.info(f'GOT RESPONSE AS {visual_context}')
        #     if visual_context:
        #         groupchat.messages.insert(-2,{'content':visual_context,'role':'user','name':'helper'})
        # current_app.logger.info(f'{messages[-1]}')
        current_app.logger.info(f'Inside state_transition with message {messages[-1]["content"][:10]}.. & last_speaker:{last_speaker.name}')
        # crossbar_message = {"text": ["Working on "+messages[-1]['content']+".\n please evaluate the response i am giving to check if it meets the current action"], "priority": 49, "action": 'Thinking', "historical_request_id": [], "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
        # 'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0}, 'bottom_left': {'x': 0, 'y': 0}}}
        # result = client.publish(
        #     f"com.hertzai.hevolve.chat.{user_id}", f'{crossbar_message}')
        metadata = get_saved_metadata()
        # current_app.logger.info(messages[-1])
        if messages[-1]['role'] == 'tool':
            current_app.logger.info('role is tool returning assistant')
            return assistant
        
        
        if not messages[-1]["content"].startswith('Reflect on the sequence') and not messages[-1]["content"].startswith('Focus on the current task at hand'):
            json_obj = retrieve_json(messages[-1]["content"])
            if json_obj:
                try:
                    if 'status' in json_obj:
                        current_app.logger.info(f'got status as:{json_obj["status"]} ')
                        if json_obj['status'].lower() == 'error' and 'message' in json_obj:
                            return author
                        elif json_obj['status'].lower() == 'completed' or json_obj['status'].lower() == 'success':
                            if 'recipe' in json_obj.keys():
                                current_app.logger.info('Recipe created successfully')
                                merged_dict = {**final_recipe[prompt_id], **json_obj}
                                flow = recipe_for_persona[user_prompt]
                                name = f'prompts/{prompt_id}_{flow}_recipe.json'
                                with open(name, "w") as json_file:
                                    json.dump(merged_dict, json_file)
                                current_app.logger.info(f"Dictionary saved to {name}")
                                recipe_for_persona[user_prompt] += 1
                                user_tasks[user_prompt] = Action(config['flows'][recipe_for_persona[user_prompt]]['actions'])
                                final_recipe[prompt_id] = merged_dict
                                return None
                            if 'action_id' in json_obj.keys():
                                if user_tasks[user_prompt].fallback == False and user_tasks[user_prompt].recipe == False:
                                    current_app.logger.info('UPDATED TIMER for this action')
                                    end = time.time()
                                    task_time[prompt_id]['times'].append(end-task_time[prompt_id]['timer'])
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
                            flow = recipe_for_persona[user_prompt]
                            name = f'prompts/{prompt_id}_{flow}_{json_obj["action_id"]}.json'
                            user_tasks[user_prompt].fallback = False
                            user_tasks[user_prompt].recipe = False
                            metadata = strip_json_values(agent_data[prompt_id])
                            json_obj['metadata'] = metadata
                            json_obj['time_took_to_complete'] = task_time[prompt_id]['times'][-1]
                            for i in json_obj['recipe']:
                                if 'tool_name' in i and i['tool_name'] != "":
                                    i['agent_to_perform_this_action'] = 'Helper'
                                elif 'generalized_functions' in i and i['generalized_functions'] != "":
                                    i['agent_to_perform_this_action'] = 'Executor'
                                else:
                                    i['agent_to_perform_this_action'] = 'Assistant'
                            with open(name, "w") as json_file:
                                json.dump(json_obj, json_file)
                            user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                            individual_json[user_prompt] = json_obj
                            current_app.logger.info(f'Saved Individual recipe at: {name}')
                            
                            return chat_instructor   
                except Exception as e:
                    current_app.logger.error(f'GOT SOME ERROR WHILE JSON: {e}')
        
        
        crossbar_message = {"text": [f"{last_speaker.name} "+f'{messages[-1]["content"]}'], "priority": 49, "action": 'Thinking', "historical_request_id": [], "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
        'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0}, 'bottom_left': {'x': 0, 'y': 0}}}
        result = client.publish(
            f"com.hertzai.hevolve.chat.{user_id}", f'{crossbar_message}')            
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
            
        
            
        if last_speaker.name == 'Executor' or last_speaker.name == 'Helper' or last_speaker.name == 'UserProxy' or last_speaker.name == 'UserProxy' or last_speaker.name == 'ChatInstructor':
            
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
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(),
        ]
    )
    
    group_chat = autogen.GroupChat(
        agents=all_agents,
        messages=[],
        max_round=30,
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

def create_time_agents(user_id, prompt_id,role,goal,actions):
    user_prompt = f'{user_id}_{prompt_id}'
    time_actions[user_prompt] = Action(actions)
    
    time_agent = autogen.AssistantAgent(
        name='time_agent',
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message="You are an helpful AI assistant used to perform time based tasks given to you. "
        f"""You can refer below details to perform task:
            Actions: <actionsStart>{user_tasks[user_prompt].actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{final_recipe[prompt_id]}<generalized_functionsEnd><recipeEnd>
            After completing the current action ask the StatusVerifier to verify the status of current action.        
        """
        f"When you want to communicate with {role} connect main agent using 'connect_time_main' tool."
        "Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesize_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory.]"
        "if you have any task which is not doable by these tool check recipe first else create python code to do so"
        "the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video."
        f'IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: `@user {{"message_2_user": "Your message here"}}`'
        "Return 'TERMINATE' when the task is done."
    )
    
    time_user = autogen.UserProxyAgent(
        name=f"user_proxy_{user_id}",
        human_input_mode="NEVER",
        llm_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    helper1 = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message=f"""You are Helper Agent. Help the {role} agent to complete the task:
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than {role},Executor,multi_role_agent.
            4. Tools you have [send_message_in_seconds,send_message_to_user,send_presynthesize_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory.]
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: `@user {{"message_2_user": "Your message here"}}`
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            Actions: <actionsStart>{user_tasks[user_prompt].actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{final_recipe[prompt_id]}<generalized_functionsEnd><recipeEnd>            
            
            When writing code, always print the final response just before returning it.
        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    executor1 = autogen.AssistantAgent(
        name="Executor",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message=f'''You are a executor agent. focused solely on creating, running & debugging code.
            Your responsibilities:
            1. Follow the steps below to achieve the goal: {goal}.
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than {role},Executor,multi_role_agent.
            4. Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesize_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory.]
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the {role}, always use this format: `@{role} {{"message_2_user": "Your message here"}}`
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
            Actions: <actionsStart>{user_tasks[user_prompt].actions}<actionEnd>
            Recipe  & generalized_functions: <recipeStart><generalized_functionsStart>{final_recipe[prompt_id]}<generalized_functionsEnd><recipeEnd>
        
            Note: Your Working Directory is "/home/hertzai2019/newauto/coding" use this if you need,
            Add proper error handling, logging.
            Always provide clear execution results or error messages to the assistant.
            if you get any conversation which is not related to coding ask the manager to route this conversation to user
            When writing code, always print the final response just before returning it.
        ''',
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    multi_role_agent1 = autogen.AssistantAgent(
        name="multi_role_agent",
        llm_config=llm_config,
        code_execution_config=False,
        system_message="""You will send message from multiple different personas your, job is to ask those question to assistant agent
        if you think some text was intent to give to some other agent but i came to you send the same message to user""",
    )
    verify1 = autogen.AssistantAgent(
        name="StatusVerifier",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=""""You are an Status verification agent.
        Role: Track and verify the status of actions. Provide updates strictly in JSON format only when status is completed.
        Response formats:
            1. Action Completed Successfully: {"status": "completed","action": "current action","action_id": 1/2/3...,"message": "message here"}
            2. Action Error: {"status": "error","action": "current action","action_id": 1/2/3...,"message": "message here"}
            2. Action Pending: {"status": "pending","action": "current action","action_id": 1/2/3...,"message": "pending actions here"}
        Important Instructions:
            Only mark an action as "Completed" if the Assistant Agent confirms successful completion.
            For pending tasks or ongoing actions, respond to helper to complete the task.
            Verify the action performed by assistant and make sure the action is performed correctly as per instructions. if action performed was not as per instructions give the pending actions to the helper agent.
            Report status only—do not perform actions yourself.
            
        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    chat_instructor1 = autogen.UserProxyAgent(
        name="ChatInstructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    helper1.register_for_llm(name="text_2_image", description="Text to image Creator")(helper_fun.txt2img)
    time_agent.register_for_execution(name="text_2_image")(helper_fun.txt2img)
    
    
    def camera_inp(inp: Annotated[str, "The Question to check from visual context"])->str:
        return helper_fun.get_user_camera_inp(inp,user_id)        
    helper1.register_for_llm(name="get_user_camera_inp", description="Get user's visual information to process somethings")(camera_inp)
    time_agent.register_for_execution(name="get_user_camera_inp")(camera_inp)  
    
    def save_data_in_memory(key: Annotated[str, "Key path for storing data now & retrieving data later. Use dot notation for nested keys (e.g., 'user.info.name')."],
                            value: Annotated[Optional[Any], "Value you want to store; may be int, str, float, bool, dict, list, json object."] = None) -> str:
        current_app.logger.info('INSIDE save_data_in_memory')
        keys = key.split('.')
        d = agent_data.setdefault(prompt_id, {})
        
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value
        return f'{agent_data[prompt_id]}'
    
    helper1.register_for_llm(name="save_data_in_memory", description="Use this to Store and retrieve data using key-value storage system")(save_data_in_memory)
    time_agent.register_for_execution(name="save_data_in_memory")(save_data_in_memory)
    
    def get_saved_metadata() -> str:
        stripped_json = strip_json_values(agent_data[prompt_id])
        return f'{stripped_json}'

    helper1.register_for_llm(name="get_saved_metadata", description="Returns the schema of the json from internal memory with all keys but without actual values.")(get_saved_metadata)
    time_agent.register_for_execution(name="get_saved_metadata")(get_saved_metadata)
    
    def get_data_by_key(key: Annotated[str, "Key path for retrieving data. Use dot notation for nested keys (e.g., 'user.info.name')."]) -> str:
        keys = key.split('.')
        d = agent_data.get(prompt_id, {})
        
        try:
            for k in keys:
                d = d[k]
            return f'{d}'
        except KeyError:
            return "Key not found in stored data."


    helper1.register_for_llm(name="get_data_by_key", description="Returns all data from the internal Memory")(get_data_by_key)
    time_agent.register_for_execution(name="get_data_by_key")(get_data_by_key)
    
    def get_user_id() -> str:
        current_app.logger.info('INSIDE get_user_id')
        return f'{user_id}'

    
    helper1.register_for_llm(name="get_user_id", description="Returns the unique identifier (user_id) of the current user.")(get_user_id)
    time_agent.register_for_execution(name="get_user_id")(get_user_id)
    
    def get_prompt_id() -> str:
        current_app.logger.info('INSIDE get_prompt_id')
        return f'{prompt_id}'

    
    helper1.register_for_llm(name="get_prompt_id", description="Returns the unique identifier (prompt_id) associated with the current prompt or conversation.")(get_prompt_id)
    time_agent.register_for_execution(name="get_prompt_id")(get_prompt_id)
    
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
        data['chattts'] = False
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
            return f"Video Generation task added to queue with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
        else:
            return f"Video Generation completed with conv_id:{conv_id}. Ask the helper to save this conv_id in the same collection from which the story used to generate the video was retrieved, for future reference"
    
    helper1.register_for_llm(name="Generate_video", description="Generate/presynthesize video with text and save it in database")(Generate_video)
    time_agent.register_for_execution(name="Generate_video")(Generate_video)
    
    def recent_files() -> str:
        current_app.logger.info('INSIDE get_user_uploaded_file')
        if recent_file_id[user_id]:
            return f'Got user uploaded file the file_id is {recent_file_id[user_id]}'

        return 'No file uploaded from user'
    
    helper1.register_for_llm(name="get_user_uploaded_file", description="get user's recent uploaded files")(recent_files)
    time_agent.register_for_execution(name="get_user_uploaded_file")(recent_files)
    

    def img2txt(image_url: Annotated[str, "image url of which you want text"],text: Annotated[str, "the details you want from image"]='Describe the Images & Text data in this image in detail') -> str:
        current_app.logger.info('INSIDE img2txt')
        url = "http://azurekong.hertzai.com:8000/llava/image_inference"

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
    
    helper1.register_for_llm(name="get_text_from_image", description="Image to Text")(img2txt)
    time_agent.register_for_execution(name="get_text_from_image")(img2txt)
    
    def create_scheduled_jobs(interval_sec: Annotated[int, "time between two Interval in seconds."], 
                            job_description: Annotated[str, "Description of the job to be performed"],
                            cron_expression: Annotated[Optional[str], "Cron expression for scheduling. Example: '0 9 * * 1-5' (Runs at 9:00 AM, Monday to Friday). If the interval is greater than 60 seconds or it needs to be executed at a dynamic cron time this argument is Mandatory else None"]=None) -> str:
        current_app.logger.info('INSIDE create_scheduled_jobs')
        
        # actual_execution_time = sum(task_time[prompt_id]['times'][-1])
        # if interval_sec < actual_execution_time:
        #     return f"Unable to create scheduled job for the specified interval because the actual execution time ({actual_execution_time} seconds) exceeds the interval between jobs ({interval_sec} seconds). Please use an interval longer than {actual_execution_time} seconds. Would you like to create a scheduled job with this updated interval?"
        
        # if not scheduler.running:
        #     scheduler.start()
        
        # try:
        #     if not interval_sec or int(interval_sec) >60:
        #         trigger = CronTrigger.from_crontab(cron_expression)
        #         job_id = f"job_{int(time.time())}"
        #         scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, int(user_id),int(prompt_id)])
        #         current_app.logger.info('Successfully created scheduler job')
        #         return 'Successfully created scheduler job'
        #     else:
        #         trigger = IntervalTrigger(seconds=int(interval_sec))
        #         job_id = f"job_{int(time.time())}"
        #         scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, int(user_id),int(prompt_id)])
        #         current_app.logger.info('Successfully created scheduler job')
        #         return 'Successfully created scheduler job'
        # except Exception as e:
        #     current_app.logger.error(f'Error in create_scheduled_jobs: {str(e)}')
        #     return f"Error creating scheduled job: {str(e)}"
        return 'Added this schedule job in creation process will do it at the end. you can go ahead and mark this action as completed.'
        
    helper1.register_for_llm(name="create_scheduled_jobs", description="Creates time-based jobs using APScheduler to schedule jobs")(create_scheduled_jobs)
    time_agent.register_for_execution(name="create_scheduled_jobs")(create_scheduled_jobs)
    
    def send_message_to_user(text: Annotated[str, "Text to send to the user"],
                         avatar_id: Annotated[Optional[str], "Unique identifier for the avatar"] = None,
                         response_type: Annotated[Optional[str], "Response mode: 'Realistic' (slower, better quality) or 'Realtime' (faster, lower quality)"] = 'Realtime') -> str:
        current_app.logger.info('INSIDE send_message_to_user')
        current_app.logger.info(f'SENDING DATA 2 user with values text:{text}, avatar_id:{avatar_id}, response_type:{response_type}')
        request_id = str(uuid.uuid4()).replace("-", "")[:11]
        thread = threading.Thread(target=send_message_to_user1, args=(user_id, text, '',prompt_id))
        thread.start()
        return f'Message sent successfully to user with request_id: {request_id_list[user_prompt]}-intermediate'
    
    helper1.register_for_llm(name="send_message_to_user", description="Sends a message/information to user. You can use this if you want to ask a question")(send_message_to_user)
    time_agent.register_for_execution(name="send_message_to_user")(send_message_to_user)
    
    def send_presynthesize_video_to_user(conv_id: Annotated[str, "Conversation ID associated with the text from memory"]) -> str:
        current_app.logger.info('INSIDE send_presynthesize_video_to_user')
        current_app.logger.info(f'SENDING DATA 2 user with value: conv_id:{conv_id}.')
        return 'Message sent successfully to user'
    
    helper1.register_for_llm(name="send_presynthesize_video_to_user", description="Sends a presynthesized message/video/dialogue to user using conv_id.")(send_presynthesize_video_to_user)
    time_agent.register_for_execution(name="send_presynthesize_video_to_user")(send_presynthesize_video_to_user)
    
    def send_message_in_seconds(text: Annotated[str, "text to send to user"],
                       delay: Annotated[int, "time to wait in seconds before sending text"],
                       conv_id: Annotated[Optional[int], "conv_id for this text if not available make it None"],) -> str:
        current_app.logger.info('INSIDE send_message_in_seconds')
        current_app.logger.info(f'with text:{text}. and waiting time: {delay} conv_id: {conv_id}')
        run_time = datetime.fromtimestamp(time.time() + delay)
        scheduler.add_job(send_message_to_user1, 'date', run_date=run_time, args=[user_id, text, '',prompt_id])
        return 'Message scheduled successfully'
    
    helper1.register_for_llm(name="send_message_in_seconds", description="Sends a presynthesized message/video/dialogue to user using conv_id with a timer.")(send_message_in_seconds)
    time_agent.register_for_execution(name="send_message_in_seconds")(send_message_in_seconds)
    
    
    context_handling = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(),
        ]
    )
    context_handling.add_to_agent(time_agent)
    context_handling.add_to_agent(helper1)
    context_handling.add_to_agent(executor1)
    context_handling.add_to_agent(multi_role_agent1)
    context_handling.add_to_agent(verify1)
    
    time_agent_object = {}
    time_agent_object['time_agent'] = time_agent
    time_agent_object['time_user'] = time_user
    time_agent_object['helper1'] = helper1
    time_agent_object['executor1'] = executor1
    time_agent_object['multi_role_agent1'] = multi_role_agent1
    time_agent_object['verify1'] = verify1
    time_agent_object['chat_instructor1'] = chat_instructor1
    
    time_agent.description = 'Designed to handle specific tasks by interacting directly with other agents or the user. It acts as the primary orchestrator for task management and ensures tasks are completed efficiently'
    time_user.description = 'Acts as a user, performing tasks assigned by the Assistant Agent. It simulates user actions and provides results or feedback as required.'
    helper1.description = 'this is a helper agent that calls tools, facilitates task completion & assists other agents'
    executor1.description = 'this is an executor agent that Specialized agent for code execution & response handling'
    multi_role_agent1.description = 'Acts as an external agent with multi-functional capabilities. Note: This agent should never be directly invoked.'
    verify1.description = 'this is a verify status agent. which will verify the status of current action that will be called after ChatInstructor gives instruction to complete an action & assistant completes it, this agent will provide updates in a structured JSON format & then call user agent'
    chat_instructor1.description = 'this is a ChatInstructor agent that provides step-by-step action plans for task execution'
    
    def state_transition1(last_speaker, groupchat):
        current_app.logger.info('INSIDE TIMER STATE TRANSITION')      
        messages = groupchat.messages
        # visual_context = helper_fun.get_visual_context(user_id)
        # if visual_context:
        #     groupchat.messages.insert(-1,{'content':visual_context,'role':'user','name':'helper'})
        try:
            pattern = r'\{.*?\}' # getting all json from text
            matches = re.findall(pattern, messages[-1]["content"], re.DOTALL)   
            json_objects = [json.loads(match) for match in matches]
            current_app.logger.info(f'Got Json as {len(json_objects)}')
            if json_objects:
                last_json = json_objects[-1]
                current_app.logger.info(f'last json as {last_json}')
                if 'status' in last_json.keys() and last_json['status'].lower() == 'completed':
                    current_app.logger.info('GOT COMPLETED FOR ACTION')
                    try:
                        time_actions[user_prompt].current_action += 1
                    except:
                        current_app.logger.error('GOT ERROR WHILE UPDATING CURRENT ACTION')
                        time_actions[user_prompt].current_action += 1
                    return chat_instructor1
                    
                currentaction_id = last_json['action_id']
                if final_recipe[prompt_id]['actions'][currentaction_id-1]['can_perform_without_user_input'] == 'yes':
                    return time_agent
        except Exception as e:
            current_app.logger.error(f'Got Error while getting json for current actionid: {e}')
            
        pattern3 = r"@statusverifier"
        if re.search(pattern3, messages[-1]["content"].lower()):
            current_app.logger.info("String contains @StatusVerifier returnig StatusVerifier")
            return verify1
    
        current_app.logger.info(f'Inside state_transition with message :10 {messages[-1]["content"][:10]} & last_speaker {last_speaker.name}')
        if last_speaker.name == f"user_proxy_{user_id}" or last_speaker.name == "multi_role_agent" or last_speaker.name == "helper" or last_speaker.name == "Executor":
            return time_agent
        current_app.logger.info(f'Checking for @user or @user in message')
        if '@user' in messages[-1]["content"].lower():
            current_app.logger.info('GOT @USER in message')
            temp_message = messages[-1]["content"]
            temp_message = temp_message.replace("'",'"')
            json_match = re.search(r'{[\s\S]*}', temp_message)
            if json_match:
                try:
                    current_app.logger.info('GOT Json')
                    current_app.logger.info(f'got json object')
                    json_part = json_match.group(0)
                    current_app.logger.info('Sending user the message')
                    json_obj = json.loads(json_part)
                    send_message_to_user1(user_id,json_obj['message_2_user'],'',prompt_id)
                except:
                    pass
                return "auto"
        
            
        if messages[-1]["role"] == 'function':
            current_app.logger.info('The last speaker was function returning assistant') 
            return time_agent
        if 'exitcode:' in messages[-1]["content"]:
            current_app.logger.info('Got exitcode in text returning assistant')
            return time_agent
        if 'TERMINATE' in messages[-1]["content"].upper():
            current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        return "auto"
    
    select_speaker_transforms = transform_messages.TransformMessages(
        transforms=[
            transforms.MessageHistoryLimiter(max_messages=50,keep_first_message=True),
            transforms.MessageTokenLimiter(max_tokens=4000, max_tokens_per_message=1000, min_tokens=0),
            ToolMessageHandler(),
        ]
    )
    time_group_chat = autogen.GroupChat(
        agents=[time_agent, helper1, time_user,multi_role_agent1,executor1,chat_instructor1,verify1],
        messages=[],
        max_round=10,
        select_speaker_transform_messages=select_speaker_transforms,
        speaker_selection_method=state_transition1,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=False
    )
    
    time_manager = autogen.GroupChatManager(
        groupchat=time_group_chat,
        llm_config={"cache_seed": None,"config_list": config_list}
    )
    
    time_agent_object['time_group_chat'] = time_group_chat
    time_agent_object['time_manager'] = time_manager
    return time_agent_object
    
    
    
    
    
    

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
    
    #TOOL CALL AND REPONSE CHECK
    #current_msg.get('tool_calls') and next_msg.get('role') != 'tool':
    if len(group_chat.messages)>2 and 'tool_calls' in group_chat.messages[-1]:
        current_app.logger.warning('GOT INPUT BUT LAST MESSAGE IS tool_calls should wait for tool response')
        return 'Processing a tool now please try later'
        
    if Failure:
        current_app.logger.warning(f'CHECK THIS OUT group_chat.messages:{group_chat.messages[-5:]}')
        current_app.logger.warning(f'CHECK THIS OUT group_chat.messages:{len(group_chat.messages)}')
        for i in range(len(group_chat.messages)):
            group_chat.messages[i]['role'] = 'user'
        clear_history = False
        if user_tasks[user_prompt].fallback == True or user_tasks[user_prompt].recipe == True:
            actions_prompt = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action-1)
            message = 'Lets continue the work we were doing if action is completed then ask status verifier Agent to Please tell the status of the action'
            text = f'Action {user_tasks[user_prompt].current_action+1}: {message} '
        else:
            try:
                message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)
                text = f'Action {user_tasks[user_prompt].current_action+1}: {message} '
            except:
                message = ""
                text = f'Action {user_tasks[user_prompt].current_action}: {message} '

    if len(messages[user_prompt])>0:
        # last_agent, last_message = manager.resume(messages=messages[user_prompt])
        try:
            result = agents_object['user'].initiate_chat(recipient=manager, message=text, clear_history=clear_history,silent=False)
        except Exception as e:
            current_app.logger.error(f'Got some error it can be multiple tools called at one error:{e}')
            current_app.logger.error(traceback.format_exc())
            # current_app.logger.error(f'len of group chat :{group_chat.messages}')
            return 'Our Agent is facing issues in creating this agent please try later'
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
        task_time[prompt_id] = {'timer':time.time(),'times':[]}
        result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
    
    current_app.logger.info("\n=== Chat Summary ===")
    current_app.logger.info("\n=== Full response ===")
    # current_app.logger.info(result)
    

    
    while True:
        file_path = f'prompts/{prompt_id}.json'
        with open(file_path, 'r') as f:
            data = json.load(f)
            role = data['flows'][recipe_for_persona[user_prompt]]['persona']
        current_app.logger.info('inside while')
        if group_chat.messages[-1]['name'] == 'ChatInstructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
            current_app.logger.info(f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content'][:10]}..")
            json_obj = retrieve_json(group_chat.messages[-2]["content"])
            if not json_obj:
                json_obj = individual_json[user_prompt]
            if json_obj and type(json_obj)==dict and 'status' in json_obj.keys():
                if json_obj['status'].lower() == 'completed' and 'recipe' not in json_obj.keys():
                    if user_tasks[user_prompt].current_action != int(json_obj['action_id']):
                        user_tasks[user_prompt].fallback = True
                    current_app.logger.info(f'UPDATIN CURRENT ACTION AS :{int(json_obj["action_id"])}')
                    user_tasks[user_prompt].current_action = int(json_obj['action_id'])                
            else:
                current_app.logger.warning(f'it is not a json object the error is:')
                current_app.logger.info('it is not a json object You should ask status verifier to give response in proper format & not move ahead to next action')
                if group_chat.messages[-1]['role'] == 'tool':
                    current_app.logger.info('GOT role is tool')
                    break
                if user_tasks[user_prompt].fallback == True or user_tasks[user_prompt].recipe == True:
                    actions_prompt = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action-1)
                    message = f'Lets continue the work we were doing, if action is completed then ask @statusverifier Agent to Please tell the status of the action {user_tasks[user_prompt].current_action}: {actions_prompt}'
                else:
                    actions_prompt = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)
                    message = f'Lets continue the work we were doing, if action is completed then ask @statusverifier Agent to Please tell the status of the action {user_tasks[user_prompt].current_action+1}: {actions_prompt}'
                result = agents_object['helper'].initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                continue
            current_app.logger.info('resuming chat')
            if user_tasks[user_prompt].current_action>=len(user_tasks[user_prompt].actions):
                if user_tasks[user_prompt].recipe == True:
                    user_tasks[user_prompt].recipe = False
                    user_tasks[user_prompt].fallback = False
                    metadata = strip_json_values(agent_data[prompt_id])
                    message = '''Focus on the current task at hand and create a detailed recipe that includes only the necessary steps for this action from history, along with a suitable name. Provide the output in the following JSON format:
                    { "status", "done", "action": "'''+str(user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action-1))+'''","fallback_action":"", "persona":"","action_id": '''+f'{user_tasks[user_prompt].current_action}'+''', "recipe": [{{"steps":"steps here","tool_name":"Only include tool name here if used for this step.","generalized_functions": "Only include this field if any Python code is created, otherwise omit it entirely."}}],"can_perform_without_user_input":"can you perform this action on your own without user input in future. only say no when it is absolutely mandatory and you cannot proceed without it, if you can proceed by checking with other agents you should say yes.  say yes/no if no they give the reason as well e.g. no-i need user's likes and dislike", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer action_id is required as an entrypoint from list of existing action_ids to perform this job","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }
                    Recipe Requirements:
                    1. Generalized Python Functions: Give the code which was created and excuted successfully without any error handling edge cases. leave it blank when there is no code nedded to perform the action
                    2. Avoid directly storing any specific information provided by the author in the recipe. Use placeholders for variables instead.
                    3. Ensure that coding and non-coding steps are not combined within the same function.
                    4. For all Python functions, include comprehensive docstrings to explain their purpose, parameters, and usage. This should especially clarify non-coding steps that require utilizing the assistant's language capabilities.
                    5. If any internal tool is used to complete a step, provide detailed instructions on how to call or utilize that tool instead of providing the code for that step.
                    '''+f'6. The persona must be one of the following: {role}. No other personas are allowed.'
                elif user_tasks[user_prompt].fallback == True:
                    user_tasks[user_prompt].recipe = True
                    user_tasks[user_prompt].fallback = False
                    message = f" Action {user_tasks[user_prompt].current_action} fallback:ask user what actions should be taken if current actions fail in the future after you get the response from user give the conversaation to StatusVerifier agent"      
                else:
                    # if recipe_for_persona[user_prompt]  < total_persona_actions[user_prompt]:
                    #     recipe_for_persona[user_prompt] += 1
                    user_tasks[user_prompt].new_json.append(json_obj)
                    user_tasks[user_prompt].current_action += 1
                    # name = f'prompts/{prompt_id}_new.json'
                    # with open(name, "w") as json_file:
                    #     json.dump(user_tasks[user_prompt].new_json, json_file)
                    current_app.logger.info('updating updated action in .json')
                    individual_recipe = []
                    flow = recipe_for_persona[user_prompt]
                    for i in range(1,(user_tasks[user_prompt].current_action)):
                        current_app.logger.info(f'checking for prompts/{prompt_id}_{flow}_{i}.json')
                        try:
                            with open(f"prompts/{prompt_id}_{flow}_{i}.json", 'r') as f:
                                config = json.load(f)
                                individual_recipe.append(config)
                        except Exception as e:
                            current_app.logger.error(f'Got error as :{e} while checking for prompts/{prompt_id}_{flow}_{i}.json')
                            
                    group_chat.messages[-1]['content'] = f'{individual_recipe}'
                    assistant_agent.update_system_message = 'Check if the current_action depends on any other action, regardless of order it can be before or after this action. If yes, return the list of action IDs that this action depends on to ChatInstructor (e.g., [1,2]). Otherwise, return an empty array []. \nIMPORTANT: Respond strictly in an array [] format.'
                    flow = recipe_for_persona[user_prompt]
                    for num,action in enumerate(user_tasks[user_prompt].actions,1):
                        try:
                            group_chat.messages[-1]['content'] = f'{individual_recipe}'
                            message = f'''Check if the current_action depends on any other action, regardless of order it can be before or after this action. If yes, return the list of action IDs that this action depends on to ChatInstructor (e.g., [1,2]). Otherwise, return an empty array []. \nIMPORTANT: Respond strictly in an array [] format.\n current_action: {action}'''
                            result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                            for i in range(1,4):
                                text = group_chat.messages[-i]['content']
                                match = re.search(r'\[.*?\]', text)
                                if match:
                                    break
                            if match:
                                action_ids = eval(match.group())
                                
                                file_path = f'prompts/{prompt_id}_{flow}_{num}.json'
                                with open(file_path, 'r') as f:
                                    data = json.load(f)
                                data['actions_this_action_depends_on'] = action_ids
                                with open(file_path, 'w') as f:
                                    json.dump(data, f, indent=4)
                            else:
                                file_path = f'prompts/{prompt_id}_{flow}_{num}.json'
                                with open(file_path, 'r') as f:
                                    data = json.load(f)
                                data['actions_this_action_depends_on'] = []
                                with open(file_path, 'w') as f:
                                    json.dump(data, f, indent=4)
                        except Exception as e:
                            current_app.logger.info(f'GOT ERROR AT EVAL OF LIST :{e}')
                            file_path = f'prompts/{prompt_id}_{flow}_{num}.json'
                            with open(file_path, 'r') as f:
                                data = json.load(f)
                            data['actions_this_action_depends_on'] = []
                            with open(file_path, 'w') as f:
                                json.dump(data, f, indent=4)
                            continue
                    
                    individual_recipe = []
                    for i in range(1,(user_tasks[user_prompt].current_action)):
                        current_app.logger.info(f'checking for prompts/{prompt_id}_{flow}_{i}.json')
                        try:
                            with open(f"prompts/{prompt_id}_{flow}_{i}.json", 'r') as f:
                                config = json.load(f)
                                individual_recipe.append(config)
                        except Exception as e:
                            current_app.logger.error(f'Got error as :{e} while checking for prompts/{prompt_id}_{flow}_{i}.json')
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
                        role = data['flows'][recipe_for_persona[user_prompt]]['persona']
                    message = '''Reflect on the sequence of tasks and create scheduled_tasks with proper persona name and action_entry_point. Provide the output in the following JSON format:
                        { "status", "completed","dependency":[{"action_id":"action id in integer here e.g. 1,2","actions_this_action_depends_on":[e.g. 1,2,3]}], "recipe": "you should keep it blank.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer `action_id` from the list of existing `action_ids` is required as the starting point to perform this job.","action_exit_point":"An integer `action_id` up to which the job should be performed to complete the task. It can be greater than or equal to the entry point.","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ], "visual_scheduled_tasks": [ { "cron_expression": "Create this only if a visual time-based job is present; if no visual time-based job exists, do not create it.","persona":"", "job_description": "Provide a description of the visual scheduled job without specifying the time or frequency" } ] }'''

                    final_recipe[prompt_id] = {"status":"completed","actions":updated_actions}
                    assistant_agent.update_system_message = '''Reflect on the sequence of tasks and create scheduled_tasks with proper persona name and action_entry_point. Provide the output in the following JSON format:
                    { "status", "completed","dependency":[{"action_id":"action id in integer here e.g. 1,2","actions_this_action_depends_on":[e.g. 1,2,3]}], "recipe": "you should keep it blank.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer `action_id` from the list of existing `action_ids` is required as the starting point to perform this job.","action_exit_point":"An integer `action_id` up to which the job should be performed to complete the task. It can be greater than or equal to the entry point.","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ], "visual_scheduled_tasks": [ { "cron_expression": "Create this only if a visual time-based job is present; if no visual time-based job exists, do not create it.","persona":"", "job_description": "Provide a description of the visual scheduled job without specifying the time or frequency" } ] }'''
                    current_app.logger.info(f'user_tasks[user_prompt].current_action:{user_tasks[user_prompt].current_action} == len(user_tasks[user_prompt].actions)')
                    chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                    last_message = group_chat.messages[-1]
                    current_app.logger.info(f'HI I AM HERE AFTER FINAL SCHEDULED JSON NOW I WILL next actions')
                    current_app.logger.info(f'recipe_for_persona[user_prompt]:{recipe_for_persona[user_prompt]} total_persona_actions[user_prompt]:{total_persona_actions[user_prompt]}')
                    if recipe_for_persona[user_prompt]  < total_persona_actions[user_prompt]:
                        current_app.logger.info(f'Completed ONE FLOW NOW WE SHOULD WORK ON NEXT FLOW')
                        current_app.logger.info(f'DELETED CURRENT AGENTS AND CREATE NEW')
                        with open(f"prompts/{prompt_id}.json", 'r') as f:
                            config = json.load(f)
                        # recipe_for_persona[user_prompt] += 1
                        user_tasks[user_prompt] = Action(config['flows'][recipe_for_persona[user_prompt]]['actions'])
                        del user_agents[user_prompt]
                        x = get_response_group(user_id,text,prompt_id)
                        continue
                    scheduler_check[user_prompt] = True
                    json_response = final_recipe[prompt_id]
                    # if json_response and 'status' in json_response.keys(): 
                    #     merged_dict = {**final_recipe[prompt_id], **json_response}
                    #     current_app.logger.info('Recipe created successfully')
                    #     time_agents[user_prompt] = create_time_agents(user_id,prompt_id,'creator','',[]) #TODO Replace [] with actions
                    #     #TODO REMOVE FOR LOOP USE SCHEDULER ALL AT ONCE WITH 1 SEC INTERVAL
                    #     for jobs in merged_dict['scheduled_tasks']:
                    #         time_based_execution(jobs['job_description'],user_id,prompt_id,jobs['action_entry_point'])
                    #     flow = recipe_for_persona[user_prompt]
                    #     name = f'prompts/{prompt_id}_{flow}_recipe.json'
                    #     with open(name, "w") as json_file:
                    #         json.dump(merged_dict, json_file)
                    #     url = f'https://mailer.hertzai.com/update_agent_prompt?prompt_id={prompt_id}'
                    #     headers = {'Content-Type': 'application/json'}
                    #     res = requests.patch(url,headers=headers)
                    #     current_app.logger.info('Completed from here')
                    #     return 'Agent Created Successfully'
                    return 'Agent created successfully'
                result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
            else:
                # user_tasks[user_prompt].current_action = int(json_obj['action_id'])
                current_app.logger.info(f'current action {user_tasks[user_prompt].current_action} & fallback {user_tasks[user_prompt].fallback} & recipe {user_tasks[user_prompt].recipe}')
                user_tasks[user_prompt].new_json.append(json_obj)
                try:
                    message = user_tasks[user_prompt].get_action(user_tasks[user_prompt].current_action)
                except:
                    flow = recipe_for_persona[user_prompt]
                    individual_recipe = []
                    for i in range(1,(user_tasks[user_prompt].current_action)):
                        current_app.logger.info(f'checking for prompts/{prompt_id}_{flow}_{i}.json')
                        try:
                            with open(f"prompts/{prompt_id}_{flow}_{i}.json", 'r') as f:
                                config = json.load(f)
                                individual_recipe.append(config)
                        except Exception as e:
                            current_app.logger.error(f'Got error as :{e} while checking for prompts/{prompt_id}_{flow}_{i}.json')
                    group_chat.messages[-1]['content'] = f'{individual_recipe}'
                    assistant_agent.update_system_message = 'Check if the current_action depends on any other action, regardless of order it can be before or after this action. If yes, return the list of action IDs that this action depends on to ChatInstructor (e.g., [1,2]). Otherwise, return an empty array []. \nIMPORTANT: Respond strictly in an array [] format.'
                    for num,action in enumerate(user_tasks[user_prompt].actions,1):
                        message = f'''Check if the current_action depends on any other action, regardless of order it can be before or after this action. If yes, return the list of action IDs that this action depends on to ChatInstructor (e.g., [1,2]). Otherwise, return an empty array []. \nIMPORTANT: Respond strictly in an array [] format.\n current_action: {action}'''
                        result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                        for i in range(1,4):
                            text = group_chat.messages[-i]['content']
                            match = re.search(r'\[.*?\]', text)
                            if match:
                                break
                        if match:
                            action_ids = eval(match.group())
                            file_path = f'prompts/{prompt_id}_{flow}_{num}.json'
                            with open(file_path, 'r') as f:
                                data = json.load(f)
                            data['actions_this_action_depends_on'] = action_ids
                            with open(file_path, 'w') as f:
                                json.dump(data, f, indent=4)
                        else:
                            file_path = f'prompts/{prompt_id}_{flow}_{num}.json'
                            with open(file_path, 'r') as f:
                                data = json.load(f)
                            data['actions_this_action_depends_on'] = []
                            with open(file_path, 'w') as f:
                                json.dump(data, f, indent=4)
                    
                    individual_recipe = []
                    for i in range(1,(user_tasks[user_prompt].current_action)):
                        current_app.logger.info(f'checking for prompts/{prompt_id}_{flow}_{i}.json')
                        try:
                            with open(f"prompts/{prompt_id}_{flow}_{i}.json", 'r') as f:
                                config = json.load(f)
                                individual_recipe.append(config)
                        except Exception as e:
                            current_app.logger.error(f'Got error as :{e} while checking for prompts/{prompt_id}_{flow}_{i}.json')
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
                        role = data['flows'][recipe_for_persona[user_prompt]]['persona']
                    final_recipe[prompt_id] = {"status":"completed","actions":updated_actions}
                    assistant_agent.update_system_message = '''Reflect on the sequence of tasks and create scheduled_tasks with proper persona name and action_entry_point. Provide the output in the following JSON format:
                    { "status", "completed","dependency":[{"action_id":"action id in integer here e.g. 1,2","actions_this_action_depends_on":[e.g. 1,2,3]}], "recipe": "you should keep it blank.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer `action_id` from the list of existing `action_ids` is required as the starting point to perform this job.","action_exit_point":"An integer `action_id` up to which the job should be performed to complete the task. It can be greater than or equal to the entry point.","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ], "visual_scheduled_tasks": [ { "cron_expression": "Create this only if a visual time-based job is present; if no visual time-based job exists, do not create it.","persona":"", "job_description": "Provide a description of the visual scheduled job without specifying the time or frequency" } ] }'''
                    message = '''Reflect on the sequence of tasks and create scheduled_tasks with proper persona name and action_entry_point. Provide the output in the following JSON format:
                    { "status", "completed","dependency":[{"action_id":"action id in integer here e.g. 1,2","actions_this_action_depends_on":[e.g. 1,2,3]}], "recipe": "you should keep it blank.", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer `action_id` from the list of existing `action_ids` is required as the starting point to perform this job.","action_exit_point":"An integer `action_id` up to which the job should be performed to complete the task. It can be greater than or equal to the entry point.","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ], "visual_scheduled_tasks": [ { "cron_expression": "Create this only if a visual time-based job is present; if no visual time-based job exists, do not create it.","persona":"", "job_description": "Provide a description of the visual scheduled job without specifying the time or frequency" } ] }'''
                    chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                    last_message = group_chat.messages[-1]
                    json_response = retrieve_json(last_message['content'])
                    if json_response and 'status' in json_response.keys(): 
                        merged_dict = {**final_recipe[prompt_id], **json_response}
                        current_app.logger.info('Recipe created successfully')
                        name = f'prompts/{prompt_id}_{flow}_recipe.json'
                        with open(name, "w") as json_file:
                            json.dump(merged_dict, json_file)
                        url = f'https://mailer.hertzai.com/update_agent_prompt?prompt_id={prompt_id}'
                        headers = {'Content-Type': 'application/json'}
                        res = requests.patch(url,headers=headers)
                        current_app.logger.info('Completed from here2')
                        return 'Agent Created Successfully'
                    return 'Agent created successfully'
                current_app.logger.info('checking for fallback and recipe')
                if user_tasks[user_prompt].recipe == True:
                    user_tasks[user_prompt].recipe = False
                    user_tasks[user_prompt].fallback = False
                    metadata = strip_json_values(agent_data[prompt_id])
                    message = '''Focus on the current task at hand and create a detailed recipe that includes only the necessary steps for this action, along with a suitable name. Provide the output in the following JSON format:
                    { "status", "done", "action": "Describe the action performed here","fallback_action":"", "persona":"","action_id": '''+f'{user_tasks[user_prompt].current_action}'+''', "recipe": [{{"steps":"steps here","tool_name":"Only include tool name here if used for this step.","generalized_functions": "Only include this field if any Python code is created, otherwise omit it entirely."}}],"can_perform_without_user_input":"can you perform this action on your own without user input in future. only say no when it is absolutely mandatory and you cannot proceed without it, if you can proceed by checking with other agents you should say yes.  say yes/no if no they give the reason as well e.g. no-i need user's likes and dislike", "scheduled_tasks": [ { "cron_expression": "Create this only if a time-based job is present; if no time-based job exists, do not create it.","persona":"", "action_entry_point":"An integer action_id is required as an entrypoint from list of existing action_ids to perform this job","job_description": "Provide a description of the scheduled job without specifying the time or frequency" } ] }
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
                    task_time[prompt_id]['timer'] = time.time()
                    message = f'Action {user_tasks[user_prompt].current_action+1}: {message} '
                    crossbar_message = {"text": ["Working on "+message+".\n please evaluate the response i am giving to check if it meets the current action"], "priority": 49, "action": 'Thinking', "historical_request_id": [], "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
                    'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0}, 'bottom_left': {'x': 0, 'y': 0}}}
                    result = client.publish(
                        f"com.hertzai.hevolve.chat.{user_id}", f'{crossbar_message}')
                
                result = chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
            current_app.logger.info("\n=== Chat Summary ===")
            current_app.logger.info("\n=== Full response ===")
            # current_app.logger.info(result)
            
        elif group_chat.messages[-1]['content'].startswith('Focus on the current task at hand'):
            result = agents_object['assistant'].initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
            continue      
        else:
            break
            
        if user_tasks[user_prompt].current_action >len(user_tasks[user_prompt].actions):
            current_app.logger.info(f'current action {user_tasks[user_prompt].current_action} is greater than legth {len(user_tasks[user_prompt].actions)}')
            break            

    messages[user_prompt] = group_chat.messages
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]
    
    if f'message_2_user'.lower() in last_message['content'].lower():
        json_obj = retrieve_json(last_message["content"])
        if json_obj:
            try:
                last_message['content'] = json_obj['message_2_user']
            except:
                pass
    
    return last_message['content']

messages = {}
recent_file_id = {}
request_id_list = {}
recipe_for_persona = {}
total_persona_actions = {}

def recipe(user_id, text,prompt_id,file_id,request_id):
    user_prompt = f'{user_id}_{prompt_id}'
    request_id_list[user_prompt] = request_id
    current_app.logger.info('--'*100)
    if file_id:
            recent_file_id[user_id] = file_id

    if user_prompt not in user_tasks.keys():
        scheduler_check[user_prompt] = False
        with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
        user_tasks[user_prompt] = Action(config['flows'][0]['actions'])
        recipe_for_persona[user_prompt] = 0
        total_persona_actions[user_prompt] = len(config['flows'])
        agent_data[prompt_id] = {'user_id':user_id}
    try:
        
        last_response = get_response_group(user_id,text,prompt_id)

    except Exception as e:
        current_app.logger.error(f"Error occurred in create Recipe: {str(e)}")  # Add logging for debugging
        last_response = get_response_group(user_id,text,prompt_id,True,e)
    if scheduler_check[user_prompt] == True:
        
        current_app.logger.info('WORKING on TIMER AGENTS')
        with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
            number_of_flows = len(config['flows'])
            flows = config['flows']
        for i in range(number_of_flows):
            with open(f"prompts/{prompt_id}_{i}_recipe.json", 'r') as f:
                merged_dict = json.load(f)
                final_recipe[prompt_id] = merged_dict
                current_app.logger.info(f'updating the final recipe with prompts/{prompt_id}_{i}_recipe.json')
            current_app.logger.info(f'Working on flow {i} with persona {flows[i]["persona"]}')
            time_agents[user_prompt] = create_time_agents(user_id,prompt_id,flows[i]['persona'],'',flows[i]["actions"])
            if "scheduled_tasks" in merged_dict:
                for jobs in merged_dict['scheduled_tasks']:
                    time_based_execution(jobs['job_description'],user_id,prompt_id,jobs['action_entry_point'],flows[i]["actions"])
        flow = recipe_for_persona[user_prompt]
        name = f'prompts/{prompt_id}_{flow}_recipe.json'
        with open(name, "w") as json_file:
            json.dump(merged_dict, json_file)
        url = f'https://mailer.hertzai.com/update_agent_prompt?prompt_id={prompt_id}'
        headers = {'Content-Type': 'application/json'}
        res = requests.patch(url,headers=headers)
        current_app.logger.info('Completed from here')
        return 'Agent Created Successfully'
    try:
        json_response = retrieve_json(last_response)
        if 'status' in json_response.keys() and last_response['status'].lower() == 'completed': 
            if 'recipe' in json_response.keys():
                url = f'https://mailer.hertzai.com/update_agent_prompt?prompt_id={prompt_id}'
                headers = {'Content-Type': 'application/json'}
                res = requests.patch(url,headers=headers)
                current_app.logger.info('Completed from here3')
                return 'Agent Created Successfully'
            else:
                return json_response['message']
        
    except:
        pass
    return last_response
    

def acknowledgment(user_id,prompt_id,request_id):
    user_prompt = f'{user_id}_{prompt_id}'
    author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_prompt]
    group_chat.messages.append({'content':f'GOT MESSAGE ACKNOWLEDGEMENT FOR {request_id}','role':'user','name':'Helper'})