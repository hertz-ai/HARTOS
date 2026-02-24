from langchain_classic import OpenAI, LLMChain, PromptTemplate
from langchain_classic.agents import (
    ZeroShotAgent, Tool, AgentExecutor, ConversationalAgent,
    ConversationalChatAgent, LLMSingleActionAgent, AgentOutputParser,
    load_tools, initialize_agent, AgentType
)
from langchain_classic.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate
)
from langchain_classic.chains import LLMMathChain, OpenAPIEndpointChain
from langchain_classic.chains.conversation.memory import ConversationSummaryMemory, ConversationBufferWindowMemory
from langchain_classic.chains.openai_functions.openapi import get_openapi_chain
from langchain_classic.chat_models import ChatOpenAI
from langchain_classic.experimental.plan_and_execute import PlanAndExecute, load_agent_executor, load_chat_planner
from langchain_classic.llms import OpenAI, OpenAIChat
from langchain_classic.llms.base import LLM
from langchain_classic.memory import ConversationBufferMemory, ReadOnlySharedMemory, ZepMemory
from langchain_classic.requests import Requests
from langchain_classic.schema import AgentAction, AgentFinish, OutputParserException, HumanMessage, AIMessage, SystemMessage
from langchain_classic.tools import OpenAPISpec, APIOperation, StructuredTool
from langchain_classic.tools.python.tool import PythonREPLTool
from langchain_classic.utilities import GoogleSearchAPIWrapper
from flask import Flask, jsonify, request
import json
import os
import re
import logging
import requests
import pytz
from datetime import datetime, timezone
from typing import List, Union, Optional, Mapping, Any, Dict
from langchain_classic.agents.conversational_chat.output_parser import ConvoOutputParser
import time
import tiktoken
from pytz import timezone
from datetime import datetime
from waitress import serve
from logging.handlers import RotatingFileHandler
from typing import Union
from langchain_classic.agents import AgentOutputParser
from langchain_classic.agents.conversational_chat.prompt import FORMAT_INSTRUCTIONS
from langchain_classic.output_parsers.json import parse_json_markdown
from langchain_classic.schema import AgentAction, AgentFinish, OutputParserException
from langchain_classic.tools.requests.tool import RequestsGetTool, TextRequestsWrapper
from pydantic import BaseModel, Field, root_validator
from threadlocal import thread_local_data

## logging info
logging.basicConfig(level=logging.DEBUG)
handler = RotatingFileHandler('flask_app.log', maxBytes=100000, backupCount=3)

# Set the logging level for the file handler
handler.setLevel(logging.DEBUG)

# Create a logging format
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)

app = Flask(__name__)

app.logger.addHandler(handler)

# Test logging
app.logger.info('Logger initialized')

#openAPI spec
_helper_func_dir = os.path.dirname(os.path.abspath(__file__))
try:
    _openapi_path = os.path.join(_helper_func_dir, "openapi.yaml")
    if os.path.isfile(_openapi_path):
        spec = OpenAPISpec.from_file(_openapi_path)
    else:
        spec = None
        logging.getLogger(__name__).warning("openapi.yaml not found at %s — skipping", _openapi_path)
except Exception as _e:
    spec = None
    logging.getLogger(__name__).warning("Failed to load openapi.yaml: %s", _e)

try:
    _config_path = os.path.join(_helper_func_dir, "config.json")
    with open(_config_path, 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}
    logging.getLogger(__name__).warning("config.json not found at %s — using empty config", _config_path)



# global variables
encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")

#api and keys — use .get() to avoid KeyError when config.json is missing keys
for _env_key in ('OPENAI_API_KEY', 'GOOGLE_CSE_ID', 'GOOGLE_API_KEY', 'NEWS_API_KEY', 'SERPAPI_API_KEY'):
    if config.get(_env_key):
        os.environ.setdefault(_env_key, config[_env_key])
ZEP_API_URL = config.get('ZEP_API_URL', '')
ZEP_API_KEY = config.get('ZEP_API_KEY', '')
GPT_API = config.get('GPT_API', '')
STUDENT_API = config.get('STUDENT_API', '')
ACTION_API = config.get('ACTION_API', '')
FAV_TEACHER_API = config.get('FAV_TEACHER_API', '')
DREAMBOOTH_API = config.get('DREAMBOOTH_API', '')
STABLE_DIFF_API = config.get('STABLE_DIFF_API', '')
LLAVA_API = config.get('LLAVA_API', '')
BOOKPARSING_API = config.get('BOOKPARSING_API', '')
CRAWLAB_API = config.get('CRAWLAB_API', '')


def get_memory(user_id:int):
    '''
        Get memory object from zep
    '''
    session_id = "user_"+str(user_id)
    memory = ZepMemory(
        session_id=session_id,
        url=ZEP_API_URL,
        memory_key="chat_history",
        api_key=ZEP_API_KEY,
        return_messages=True,
        input_key="input"
    )
    return memory

def get_action_user_details(user_id):

    '''
        This function help to extract action that user have perfomed till time
    '''
    action_url = f"{ACTION_API}?user_id={user_id}"

    payload = {}
    headers = {}

    response = requests.request(
        "GET", action_url, headers=headers, data=payload)

    unwanted_actions=['Topic Cofirmation','Langchain','Assessment Ended','Casual Conversation', 'Topic confirmation', 'Topic not found', 'Topic Confirmation', 'Topic Listing', 'Probe', 'Question Answering', 'Fallback']
    data = response.json()
    #action_texts = [obj["action"] + ' on '+ obj["created_date"] for obj in data if obj["action"] not in unwanted_actions]
    # Filter out unwanted actions
    filtered_data = [obj for obj in data if obj["action"] not in unwanted_actions]

    # Dictionary to store the first and last occurrence dates for each action
    action_occurrences = {}

    # Iterate over the filtered data
    for obj in filtered_data:
        action = obj["action"]
        date = parse_date(obj["created_date"])

        if action not in action_occurrences:
            action_occurrences[action] = [date, date]
        else:
            first_date, last_date = action_occurrences[action]
            first_date = min(first_date, date)
            last_date = max(last_date, date)
            action_occurrences[action] = [first_date, last_date]

    # Construct the final list of actions with first and last occurrences
    action_texts = []
    for action, dates in action_occurrences.items():
        first_date, last_date = dates
        first_action_text = f"{action} on {first_date.strftime('%Y-%m-%dT%H:%M:%S')}"
        action_texts.append(first_action_text)
        if first_date != last_date:
            last_action_text = f"{action} on {last_date.strftime('%Y-%m-%dT%H:%M:%S')}"
            action_texts.append(last_action_text)
    if len(action_texts)==0:
        action_texts=['user has not performed any actions yet.']

    actions = ", ".join(action_texts)
    # Get the current time
    now = datetime.now()
    now1 = datetime.now()
    current_time = now1.strftime("%H:%M:%S")

    time_zone = "Asia/Kolkata"
    # Format the time in the desired format
    formatted_time = datetime.now(timezone(time_zone)).strftime('%Y-%m-%d %H:%M:%S.%f')

    actions = actions + ". List of actions ends. <PREVIOUS_USER_ACTION_END> \n " + "Today's datetime in "+time_zone + "is: "+  formatted_time +  " in this format:'%Y-%m-%dT%H:%M:%S.%f' \n Whenever user is asking about current date or current time at particular location then use this datetime format by asking what user's location is. Use the previous sentence datetime info to answer current time based questions coupled with google_search for current time or full_history for historical conversation based answers. Take a deep breath and think step by step.\n"
    # user detail api

    url = STUDENT_API
    payload = json.dumps({
        "user_id": user_id
    })
    headers = {
        'Content-Type': 'application/json'
    }
    response = requests.request("POST", url, headers=headers, data=payload)
    user_data = response.json()

    user_details = f'''Below are the information about the user.
    user_name: {user_data["name"]} (Call the user by this name only when required and not always),gender: {user_data["gender"]}, who_pays_for_course: {user_data["who_pays_for_course"]}(Entity Responsible for Paying the Course Fees), preferred_language: {user_data["preferred_language"]}(User's Preferred Language), date_of_birth: {user_data["dob"]}, english_proficiency: {user_data["english_proficiency"]}(User's English Proficiency Level), created_date: {user_data["created_date"]}(user creation date), standard: {user_data["standard"]}(User's Standard in which user studying)
   '''
    return user_details, actions

def get_time_based_history(prompt:str, session_id:str, start_date:str, end_date:str):

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
        url=ZEP_API_URL,
        api_key=ZEP_API_KEY,
        memory_key="chat_history",
    )


    try:

        metadata={
            "start_date": start_date,
            "end_date":  end_date
        }

        messages = memory.chat_memory.search(prompt,metadata=metadata)
        try:
            extracted_metadata = [message.message['metadata'] for message in messages]
            list_req_ids = [data.get('request_Id', None) for data in extracted_metadata]
            thread_local_data.set_reqid_list(list_req_ids)
        except Exception as e:
            app.logger.info(f"Error while getting req ids {e}")

        # messages = [message.dict() for message in messages]
        serialized_results = []
        for result in messages:
            serialized_result = result.dict(exclude_unset=True)
            #Process the 'message' field to include only specific subfields
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
        final_res = {'res_in_filter':messages}
        app.logger.info(f"final-->{final_res}")
        end_time = time.time()
        elapsed_time = end_time - start_time
        return json.dumps(final_res)
    except Exception as e:
        app.logger.info(f"Exception {e}")
        messages = memory.chat_memory.search(prompt)
        app.logger.info(f"final messages in except-->{messages}")
        try:
            extracted_metadata = [message.message['metadata'] for message in messages]
            list_req_ids = [data.get('request_Id', None) for data in extracted_metadata]
            thread_local_data.set_reqid_list(list_req_ids)
        except Exception as e:
            app.logger.info(f"Error while getting req ids {e}")
        end_time = time.time()
        elapsed_time = end_time - start_time
        app.logger.info("time taken for zep is {elapsed_time}")
        return json.dumps({'res':[message.message['content'] for message in messages]})


def parsing_string(string):
    '''
        this function will extract infromation for above function ie prompt start date end date
    '''
    try:
        prompt, start_date, end_date = [s.strip() for s in string.split(",")]
        session_id = 'user_'+str(thread_local_data.get_user_id())
        return get_time_based_history(prompt, session_id, start_date, end_date)
    except:
        now = datetime.utcnow()
        formatted_time = now.strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'
        session_id = "user_"+str(thread_local_data.get_user_id())
        return get_time_based_history(string, session_id, formatted_time, formatted_time)

def parse_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")



def parse_character_animation(string):
    '''
        Dreambooth character animation api
        input string
        how this function works
        1 get user information based on user_id
        2 get fav teacher
        3 call dreambooth api with fav teacher name
    '''
    try:
        prompt = string
        student_id_url = STUDENT_API

        payload = json.dumps({
        "user_id": thread_local_data.get_user_id()
        })
        headers = {
        'Content-Type': 'application/json'
        }

        response = requests.request("POST", student_id_url, headers=headers, data=payload)
        favorite_teacher_id = response.json()["favorite_teacher_id"]

        get_image_by_id_url = f"{FAV_TEACHER_API}/{favorite_teacher_id}"

        payload = {}
        headers = {}

        response = requests.request("GET", get_image_by_id_url, headers=headers, data=payload)

        image_name=response.json()["image_name"]

        image_name = image_name.replace("vtoonify_", "", 1)
        folder_name = image_name.split(".")[0]

        inference_url = f"{DREAMBOOTH_API}/generate_images"
        payload = json.dumps({
            "weights_dir": f"/usr/app/diffusers/examples/dreambooth/{folder_name}_result",
            "prompt": prompt
        })
        headers = {
            'Content-Type': 'application/json'
        }
        response = requests.request("POST", inference_url, headers=headers, data=payload)
        return response.json()["image_url"]
    except:
        return "something went wrong"



def parse_text_to_image(inp):
    '''
        stable diffusion
    '''
    try:

        url = f'{STABLE_DIFF_API}?prompt={inp}'
        payload = {}

        headers = {}
        response = requests.request("POST", url, headers=headers, data=payload)
        return response.json()["img_url"]
    except Exception as e:
        return f"{e} Not able to generating image at this moment please try later"

def parse_image_to_text(inp):
    '''
        LlaVA implemetation
    '''

    try:
        inp_list = inp.split(',')
        url = f'{LLAVA_API}'
        payload = {
            'url': inp_list[0],
            'prompt': inp_list[1]
        }
        files=[]
        headers={}
        response = requests.request("POST", url, headers=headers, data=payload, files=files)

        return response.text
    except Exception as e:
        return f'{e} Not able to generating answer at this moment please try later'

def parse_link_for_crwalab(inp):
    '''

        Use this function when user give url for any webpage or pdf

    '''
    inp_list = inp.split(',')
    input_url = inp_list[0]
    link_type = inp_list[1].strip(' ')
    print
    try:
        inp_list = inp.split(',')
        input_url = inp_list[0]
        link_type = inp_list[1].strip(' ')
        if link_type == 'pdf':
            try:
                cwd = os.getcwd()
                upload_folder_path = f'{cwd}/upload/'
                pdf_file_name = input_url.split("/")[-1]

                # Local path to save the PDF
                if not os.path.exists(upload_folder_path):
                    # If it does not exist, create it
                    os.makedirs(upload_folder_path)
                pdf_save_path = f'{upload_folder_path}/{pdf_file_name}'

                response = requests.get(input_url)
                with open(pdf_save_path, 'wb') as file:
                    file.write(response.content)




                payload = {
                    'user_id': thread_local_data.get_user_id(),
                    'request_id': thread_local_data.get_request_id()
                }

                # Open the file and send it in the POST request
                with open(pdf_save_path, 'rb') as file:
                    files = [('file', (pdf_file_name, file, 'application/pdf'))]
                    response = requests.post(BOOKPARSING_API, data=payload, files=files)

                os.remove(pdf_save_path)

                return f"your request has been sent and pdf is getting uploading into our system {response.text}"
            except Exception as e:
                app.logger.info(f"Got exception in book parsing api {e}")
                return "sorry I am not able to process your request at this moment"

        elif link_type == 'website':
            try:

                payload = {
                    'link': input_url,
                    'user_id': thread_local_data.get_user_id(),
                    'request_id': thread_local_data.get_request_id()
                }
                files=[

                ]
                headers = {}
                response = requests.request("POST", CRAWLAB_API, headers=headers, data=payload, files=files)

                return f"your url got uploaded and data extraction is being processes {response.text}"
            except Exception as e:
                app.logger.info(f"Got exception in crawlab api {e}")
                return "sorry I am not able to process your request at this moment"

        else:
            return "Sorry I am unable to process your request with this url type"
    except Exception as e:
        pass
