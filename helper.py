from collections import deque
import requests
import re
import autogen

from autogen.agentchat.contrib.capabilities import transform_messages, transforms
import json
from flask import current_app
from typing import List, Dict, Tuple, Annotated
import pickle
from PIL import Image
import uuid
from datetime import datetime, timedelta
import time
import redis
from langchain.schema import AgentAction, AgentFinish, OutputParserException, HumanMessage, AIMessage, SystemMessage
import pytz
from langchain.utilities import GoogleSearchAPIWrapper
import aiohttp
import asyncio
import os
from bs4 import BeautifulSoup
from langchain.memory import ZepMemory
# from autobahn.twisted.wamp import ApplicationSession, ApplicationRunner
# from twisted.internet.defer import inlineCallbacks
with open("config.json", 'r') as f:
    config = json.load(f)


os.environ["OPENAI_API_KEY"] = config['OPENAI_API_KEY']
os.environ["GOOGLE_CSE_ID"] = config['GOOGLE_CSE_ID']
os.environ["GOOGLE_API_KEY"] = config['GOOGLE_API_KEY']
os.environ["NEWS_API_KEY"] = config['NEWS_API_KEY']
os.environ["SERPAPI_API_KEY"] = config['SERPAPI_API_KEY']

ACTION_API = config['ACTION_API']
STUDENT_API = config['STUDENT_API']
ZEP_API_URL = config['ZEP_API_URL']
ZEP_API_KEY = config['ZEP_API_KEY']

search = GoogleSearchAPIWrapper(k=4)
redis_client = redis.StrictRedis(
    host='azure_all_vms.hertzai.com', port=6369, db=0)


# class CrossbarClient(ApplicationSession):

#     @inlineCallbacks
#     def onJoin(self, details):
#         print("Connected to Crossbar.io!")

#     @inlineCallbacks
#     def call_rpc(self, topic, params):
#         """Calls an RPC function dynamically with the given topic and parameters."""
#         try:
#             result = yield self.call(topic, *params)
#             print(f"RPC Call to {topic} Result:", result)
#             return result
#         except Exception as e:
#             print(f"Error calling RPC {topic}: {e}")
#             return None

# runner = ApplicationRunner(url="ws://aws_rasa.hertzai.com:8088/", realm="realm1")
# rpc_client = runner.run(CrossbarClient, start_reactor=False)

async def fetch(session, url):
    try:
        async with session.get(url) as response:
            start_time = time.time()
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            end_time = time.time()
            elapsed_time = end_time - start_time
            print(f"time taken to crawl {url} is {elapsed_time}")
            return soup.get_text()
    except Exception as e:
        print(f"An error occurred while fetching {url}: {e}")
        return ""

async def async_main(urls):
    async with aiohttp.ClientSession() as session:
        tasks = [fetch(session, url) for url in urls]
        return await asyncio.gather(*tasks)
    
def top5_results(query):
    final_res = []
    top_2_search_res = search.results(query, 2)
    top_2_search_res_link = [res['link'] for res in top_2_search_res]
    try:
        text = asyncio.run(async_main(top_2_search_res_link))
        # Removing punctuation and extra characters
        print(text)
        cleaned_text = re.sub(r'[^\w\s]', '', text[0] +
                              " "+text[1])  # Remove punctuation
        # Remove extra newlines and leading/trailing whitespaces
        cleaned_text = re.sub(r'\n+', '\n', cleaned_text).strip()
    except RuntimeError as e:
        print(f"Runtime error occurred: {e}")

    final_res.append({'text': cleaned_text, 'source': top_2_search_res_link})
    print(f"res:-->{final_res}")

    if len(final_res) == 0:
        return search.results(query, 4)

    return final_res



def parse_user_id(user_id:int):
    url = 'http://azurekong.hertzai.com:8000/db/getstudent_by_user_id'

    headers = {
        'Content-Type': 'application/json'
    }

    payload = json.dumps({
        "user_id": user_id
    })

    response = requests.request("POST", url, headers=headers, data=payload)
    return response.text

def topological_sort(actions):
    # Create adjacency list and in-degree dictionary
    adj_list = {action["action_id"]: [] for action in actions}
    in_degree = {action["action_id"]: 0 for action in actions}
    action_map = {action["action_id"]: action for action in actions}  # Map ID to full action

    # Build the graph
    for action in actions:
        if action["actions_this_action_depends_on"]:  # Ensure it's not None
            for dep in action["actions_this_action_depends_on"]:
                if dep != action["action_id"]:  # Ignore self-dependency
                    adj_list[dep].append(action["action_id"])
                    in_degree[action["action_id"]] += 1

    # Initialize queue with actions having in-degree 0 (no dependencies)
    queue = deque([aid for aid in in_degree if in_degree[aid] == 0])

    sorted_actions = []
    processed_count = 0  # Track number of processed actions

    while queue:
        aid = queue.popleft()
        sorted_actions.append(action_map[aid])  # Append action to sorted list
        processed_count += 1

        # Reduce in-degree of dependent actions
        for neighbor in adj_list[aid]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # If processed actions are less than total actions, a cycle exists
    if processed_count != len(actions):
        # Find the actions still having in-degree > 0 (part of cycle)
        cyclic_actions = [aid for aid in in_degree if in_degree[aid] > 0]
        print("Cyclic dependency detected! The following actions are involved in a cycle:")
        cyclic_ids = []
        for aid in cyclic_actions:
            cyclic_ids.append(action_map[aid]['action_id'])  # Print full action details
        print(cyclic_ids)
        return False, None, cyclic_ids

    return True, sorted_actions, None

def fix_actions(array_of_actions,cyclic_ids):
    url = "http://aws_rasa.hertzai.com:5459/gpt3"
    text = f"""From the Below json array of action we are getting cyclic dependency. the action_ids which are creating the cyclic dependecy are {cyclic_ids}.
            You can Refer the below array of actions \n{array_of_actions}\n and return the corrected action dependency without cyclic dependency.
            complete json array without cyclic dependency, RESPONSE FORMAT: e.g. [{{"action_id":"An integer action_id","actions_this_action_depends_on":[]}}] 
            IMPORTANT INSTRUCTIONS: Do not add any unnecessary hallucinated dependencies in actions
            Output array:"""
    payload = json.dumps({
    "text": text,
    "model": "3",
    "temperature": 0,
    "max_tokens": 3000,
    "top_p": 1,
    "frequency_penalty": 0
    })
    headers = {
    'Content-Type': 'application/json'
    }
    try:
        response = requests.request("POST", url, headers=headers, data=payload)
        response = response.json()
        print(response)
        x = eval(response['text'])
        print(f'got json object')
        return x
    except Exception as e:
        print(f'GOT ERROR WHILE JSON FIX:{e}')
        return None
    
    
def gpt_call(prompt):
    url = "http://aws_rasa.hertzai.com:5459/gpt3"
    text = prompt
    payload = json.dumps({
    "text": text,
    "model": "3",
    "temperature": 0,
    "max_tokens": 3000,
    "top_p": 1,
    "frequency_penalty": 0
    })
    headers = {
    'Content-Type': 'application/json'
    }
    try:
        response = requests.request("POST", url, headers=headers, data=payload)
        response = response.json()
        print(response)
        return response['text']
    except Exception as e:
        print(f'GOT ERROR WHILE JSON FIX:{e}')
        return None
    
def gpt_mini(prompt,request_id,history):
    url = "http://aws_rasa.hertzai.com:5459/gpt-json"
    prompt = f'{prompt} conside below as history {history}'
    response = requests.post(
        url,
        json={
            "model": "gpt-4o",
            "data": [{"role": "user", "content": prompt}],
            "max_token": 1000,
            "request_id": request_id
        })
    print(f"gpt 4o-mini response is {response}")
    print(f"gpt 4o-mini response is {response.json()}")
    return response.json()["text"]


def strip_json_values(data):
    if isinstance(data, dict):
        return {key: strip_json_values(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [strip_json_values(item) for item in data]
    elif isinstance(data, str):
        return f"redacted {type(data)}"  # Truncate to 8 characters and add " redact"
    elif isinstance(data, (int, float, bool)) or data is None:
        return f'redacted {type(data)}'  # Keep primitive types as is
    else:
        return f"redacted {type(data)}"

def fix_json(json_text):
    url = "http://aws_rasa.hertzai.com:5459/gpt3"
    text = """You are an expert JSON fixer. Your task is to correct a given JSON string, ensuring it is compatible with Python’s `eval()`.  

    ### Instructions:  
    1. **Fix Formatting Issues:**  
    - Convert single quotes (`'`) to double quotes (`"`) where necessary (except inside stringified JSON).  
    - Ensure correct placement of commas, brackets, and braces.  
    - Fix missing or extra quotes.  
    - Properly escape special characters like newlines (`\n`).  

    2. **Convert JSON to Python-Compatible Format:**  
    - Ensure `true`, `false`, and `null` are replaced with `True`, `False`, and `None`.  
    - If the JSON contains a string representation of a dictionary inside a field (e.g., `'{"key": "value"}'`), ensure it remains correctly formatted.  

    3. **Preserve Key-Value Data:**  
    - Do not change any key names or values, only correct formatting.  

    4. **Output Only the Fixed JSON:**  
    - Provide only the corrected JSON without explanations or extra text.  

    ### Input JSON: """+f"{json_text}"+"""
    Output Json: 
    """
    payload = json.dumps({
    "text": text,
    "model": "3",
    "temperature": 0,
    "max_tokens": 3000,
    "top_p": 1,
    "frequency_penalty": 0
    })
    headers = {
    'Content-Type': 'application/json'
    }
    try:
        response = requests.request("POST", url, headers=headers, data=payload)
        response = response.json()
        x = eval(response['text'])
        current_app.logger.info(f'got json object')
        return x
    except Exception as e:
        current_app.logger.info(f'GOT ERROR WHILE JSON FIX:{e}')
        return None


def retrieve_json(json_message):
    json_obj = None
    try:
        json_obj = eval(json_message)
        current_app.logger.info(f'got json object')
        return json_obj
    except:
        json_obj = None
    
    try:
        json_match = re.search(r'{[\s\S]*}', json_message)
        if json_match:
            json_part = json_match.group(0)
            json_obj = json.loads(json_part)
            current_app.logger.info(f'got json object')
            return json_obj
        return None
    except:
        json_obj = fix_json(json_message)
        return json_obj
    
    
class ToolMessageHandler():
    """Handles tool messages in the conversation history.
    
    This transformation checks the first message (index 0) in the conversation history.
    If the message role is 'tool', it removes that message and returns the remaining messages.
    Otherwise, it returns the conversation history unchanged.
    """
    
    def __init__(self):
        """
        Initialize the ToolMessageHandler.
        No configuration parameters are needed for this simple transformation.
        """
        pass
        
    def apply_transform(self, messages: List[Dict]) -> List[Dict]:
        """Applies the tool message handling transformation to the conversation history.
        
        Args:
            messages (List[Dict]): The list of messages representing the conversation history.
            
        Returns:
            List[Dict]: A new list containing the messages, with the first tool message removed if present.
        """
        if not messages:
            return messages
        
        # Make a copy to avoid modifying the original list
        processed_messages = messages.copy()
        
        # Only process up to second-to-last message
        if not messages or len(messages) < 2:
            return messages
        
        # current_app.logger.info(f'FIRST MESSAGE:-> {processed_messages[0]}')
        # Check if the first message has a role of 'tool'
        if processed_messages and processed_messages[0].get('role') == 'tool':
            current_app.logger.info('GOT TOOL AS FIRST MESSAGE CHANGING IT')
            # current_app.logger.info(f'{processed_messages[0]}')
            # processed_messages.pop(0)
            processed_messages[0]['role'] = 'user'
            processed_messages[0]['name'] = 'Helper'
            if 'tool_responses' in processed_messages[0]:
                del processed_messages[0]['tool_responses']
                processed_messages[0]['role'] = 'user'
                processed_messages[0]['name'] = 'Helper'
            processed_messages = processed_messages[1:]
            # current_app.logger.info(f'AFTER CHANGE: {processed_messages[0]}')
        
        
        for i in range(len(processed_messages) - 1):
            current_msg = processed_messages[i]
            next_msg = processed_messages[i + 1]
            
            # Case 1: Current message has tool_calls but next message isn't a tool
            if current_msg.get('tool_calls') and next_msg.get('role') != 'tool':
                current_app.logger.warning(f'CHANGE IN {i}')
                current_app.logger.warning(f'CURRENT MSG HAS TOOL_CALLS AND NEXT IS NOT TOOL RESPONSE')
                # current_app.logger.warning(f'CURRENT MSG {current_msg}')
                del current_msg['tool_calls']
                current_msg['role'] = 'user'
                current_msg['content'] = ' '
                current_msg['name'] = 'Helper'
                # current_app.logger.warning(f'CURRENT MSG AFTER DELETE {current_msg}')
                
            # Case 2: Current message doesn't have tool_calls but next is a tool
            elif not current_msg.get('tool_calls') and next_msg.get('role') == 'tool':
                current_app.logger.warning(f'CHANGE IN {i}')
                current_app.logger.warning(f'CHANGE IN NEXT MESSAFE TO USER')
                # current_app.logger.warning(f'Next MESSAGE BEFORE CHANGE {next_msg}')
                
                # Convert next message to user and remove tool_calls
                next_msg['role'] = 'user'
                if 'tool_responses' in next_msg:
                    del next_msg['tool_responses']
                    next_msg['role'] = 'user'
                    next_msg['name'] = 'Helper'
                # current_app.logger.warning(f'Next MESSAGE AFTER CHANGE {next_msg}')
            
            # Case 3: Current message has tool_calls but next message isn't a tool
            elif current_msg.get('role') == 'tool' and next_msg.get('role') == 'tool':
                current_app.logger.warning(f'CHANGE IN {i}')
                current_app.logger.warning(f'CURRENT ROLE TO USER')
                # Fix next message to be a tool message
                current_msg['role'] = 'user'
        
        current_app.logger.info("processed_messages")
        # current_app.logger.info(processed_messages[0])
        return processed_messages
        
    def get_logs(self, pre_transform_messages: List[Dict], post_transform_messages: List[Dict]) -> Tuple[str, bool]:
        """Generates logs about the transformation.
        
        Args:
            pre_transform_messages (List[Dict]): Messages before transformation
            post_transform_messages (List[Dict]): Messages after transformation
            
        Returns:
            Tuple[str, bool]: A tuple containing the log message and whether a transformation occurred
        """
        if len(pre_transform_messages) > len(post_transform_messages):
            return "Removed tool message from the beginning of conversation.", True
        return "No tool message was removed.", False

class Action:
    def __init__(self,actions):
        self.actions = actions
        self.current_action = 0
        self.fallback = False
        self.new_json = []
        self.recipe = False
    
    def get_action(self,current_action):
        return self.actions[current_action]
    
    def get_action_byaction_id(self,action_id):
        for i in self.actions:
            if i['action_id'] == action_id:
                return i
        return None

def txt2img(text: Annotated[str, "Text to create image"]) -> str:
    current_app.logger.info('INSIDE txt2img')
    url = f"http://aws_rasa.hertzai.com:5459/txt2img?prompt={text}"

    payload = ""
    headers = {}

    response = requests.post(url, headers=headers, data=payload)
    return response.json()['img_url']


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

def get_user_camera_inp(inp: Annotated[str, "The Question to check from visual context"],user_id:int) -> str:
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
    else:
        return 'failed to get visual context ask user to check if the camera is turned on'
        


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
        metadata = {}
        if start_date:
            metadata['start_date'] = start_date
        if end_date:
            metadata['end_date'] = end_date

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

def parse_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")

def get_visual_context(user_id,mins=5):
    '''
        This function help to extract action that user have perfomed till time
    '''
    # action_url = f"{ACTION_API}?user_id={user_id}"
    action_url = f"https://mailer.hertzai.com/get_visual_bymins?user_id={user_id}&mins={mins}"
    # Todo: get, and populate timezone from client
    time_zone = "Asia/Kolkata"

    india_tz = pytz.timezone(time_zone)

    payload = {}
    headers = {}

    response = requests.request(
        "GET", action_url, headers=headers, data=payload)

    if response.status_code == 200:
        data = response.json()
        filtered_data_video = [
            obj for obj in data if obj["zeroshot_label"] == 'Video Reasoning']
        # Process video data
        video_context_texts = []
        for obj in filtered_data_video:
            action = obj["action"]
            date = parse_date(obj["created_date"])
            gpt3_label = obj["gpt3_label"]
            if gpt3_label == 'Visual Context':
                now = datetime.now()
                # Check if the action is older than 5 minutes
                if (now - date) > timedelta(minutes=5):
                    continue
            first_action_text = f"{action} on {date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}"

            video_context_texts.append(first_action_text)
        if video_context_texts:
            return video_context_texts[:10]
        else:
            return None
    else:
        return None

def get_memory(user_id: int):
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

def history(user_id,prompt_id,role,message):
    try:
        memory = get_memory(user_id=int(user_id))
    except:
        return "Invalid user ID"
    if memory:
        if role == 'user':
            memory.chat_memory.add_message(
                HumanMessage(content=message),
                metadata={'prompt_id': prompt_id}
            )
        else:
            memory.chat_memory.add_message(
                AIMessage(content=message),
                metadata={'prompt_id': prompt_id}
            )
        return "Messages are saved!!!"
    else:
        return "Memory object not found"


config_list = [{
    "model": "gpt-4o-mini",
    "api_type": "azure",
    "api_key": "4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf",
    "base_url": "https://hertzai-gpt4.openai.azure.com/",
    "api_version": "2024-02-15-preview",
    "price":[0.00015,0.0006]
}]

llm_config = {
    "config_list": config_list,
    "cache_seed": None
}
def create_visual_agent(user_id,prompt_id):
    visual_agent = autogen.AssistantAgent(
        name='visual_agent',
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message="You are an helpful AI assistant used to perform visual based tasks given to you. "
    )
    
    visual_user = autogen.UserProxyAgent(
        name=f"UserProxy",
        human_input_mode="NEVER",
        llm_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    helper2 = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message=f"""You are Helper Agent. Help the visual_agent to complete the task:
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than ,Executor,multi_role_agent.
            4. Tools you have [txt2img, img2txt, save_data_in_memory, get_data_from_memory, get_user_id, get_prompt_id, Generate_video, get_user_uploaded_file, get_user_camera_inp, get_chat_history, create_scheduled_jobs] if you have any task which is not doable by these tool check recipe first else create python code to do so
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the, always use this format: @user {{'message_2_user':'message here'}}
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.            
            When writing code, always print the final response just before returning it.
        """,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    executor2 = autogen.AssistantAgent(
        name="Executor",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message=f'''You are a executor agent. focused solely on creating, running & debugging code.
            Your responsibilities:
            2. Use the provided Recipe for more details related to the actions.
            3. Only use the "send_message_to_roles" tool when contacting personas other than,Executor,multi_role_agent.
            4. Tools Helper Agent can use [send_message_in_seconds,send_message_to_user,send_presynthesize_video_to_user,text_2_image, get_user_camera_inp, get_user_uploaded_file, create_scheduled_jobs, get_text_from_image, Generate_video, get_user_id, get_prompt_id, get_data_by_key, get_saved_metadata and save_data_in_memory]
            5. Keep track of action and only go to next action when the current action is completed successfully
            6. Always use code from recipe given below
            7. If there is any action which is like to perform a task continously you should not do it.
            8. IMPORTANT INSTRUCTION FOR CODING: Avoid using time.sleep in any code.
            9. IMPORTANT instruction: If you want to ask something or send something to the user, always use this format: @user {{'message_2_user':'message here'}}
            10. the response of Generate_video tool will be conv_id you should save that conv_id along with the text you used to generate video so that the next you can use the conv_id to use the generated video.
        
            Note: Your Working Directory is "/home/hertzai2019/newauto/coding" use this if you need,
            Add proper error handling, logging.
            Always provide clear execution results or error messages to the assistant.
            if you get any conversation which is not related to coding ask the manager to route this conversation to user
            When writing code, always print the final response just before returning it.
        ''',
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    multi_role_agent2 = autogen.AssistantAgent(
        name="multi_role_agent",
        llm_config=llm_config,
        code_execution_config=False,
        system_message="""You will send message from multiple different personas your, job is to ask those question to assistant agent
        if you think some text was intent to give to some other agent but i came to you send the same message to user""",
    )
    verify2 = autogen.AssistantAgent(
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
    
    chat_instructor2 = autogen.UserProxyAgent(
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
    context_handling.add_to_agent(visual_agent)
    context_handling.add_to_agent(helper2)
    context_handling.add_to_agent(executor2)
    context_handling.add_to_agent(multi_role_agent2)
    context_handling.add_to_agent(verify2)
    
    return visual_agent, visual_user, helper2, executor2, multi_role_agent2, verify2, chat_instructor2
    