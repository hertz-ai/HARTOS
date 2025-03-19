from collections import deque
import requests
import re
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
            if 'tool_responses' in processed_messages[0]:
                del processed_messages[0]['tool_responses']
                processed_messages[0]['role'] = 'user'
                processed_messages[0]['name'] = 'Helper'
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
        
        current_app.logger.debug("processed_messages")
        # current_app.logger.debug(processed_messages)
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

def get_visual_context(user_id):
    '''
        This function help to extract action that user have perfomed till time
    '''
    action_url = f"{ACTION_API}?user_id={user_id}"

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
        action_texts = []
        if video_context_texts:
            action_texts.append('<Last_5_Minutes_Visual_Context_Start>')
            action_texts.extend(video_context_texts)
            action_texts.append('<Last_5_Minutes_Visual_Context_End>')
            action_texts.append(
                'If a person is identified in Visual_Context section that\'s most probably the user (me) & most likely not taking any selfie.')

        if len(action_texts) == 0:
            action_texts = ['user has not performed any actions yet.']
            return None

        actions = ", ".join(action_texts)
        # Get the current time

        # Format the time in the desired format
        formatted_time = datetime.now(pytz.utc).astimezone(
            india_tz).strftime('%Y-%m-%d %H:%M:%S')

        actions = actions + ". List of actions ends. <PREVIOUS_USER_ACTION_END> \n " + "Today's datetime in "+time_zone + "is: " + formatted_time + \
            " in this format:'%Y-%m-%dT%H:%M:%S' \n Whenever user is asking about current date or current time at particular location then use this datetime format by asking what user's location is. Use the previous sentence datetime info to answer current time based questions coupled with google_search for current time or full_history for historical conversation based answers. Take a deep breath and think step by step.\n"
        # user detail api

        return actions
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
