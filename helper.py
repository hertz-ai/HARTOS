from collections import deque
import requests
import re
import json
from flask import current_app
from typing import List, Dict, Tuple, Annotated
import pickle
from PIL import Image
import uuid
from datetime import datetime
import time
import redis

redis_client = redis.StrictRedis(
    host='azure_all_vms.hertzai.com', port=6369, db=0)


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
        
        # current_app.logger.info(f'FIRST MESSAGE:-> {processed_messages[0]}')
        # Check if the first message has a role of 'tool'
        if processed_messages and processed_messages[0].get('role') == 'tool':
            current_app.logger.info('GOT TOOL AS FIRST MESSAGE POPPING IT')
            # processed_messages.pop(0)
            processed_messages[0]['role'] = 'user'
            del processed_messages[0]['tool_responses']
        
        # Only process up to second-to-last message
        if not messages or len(messages) < 2:
            return messages
        
        for i in range(len(processed_messages) - 2):
            current_msg = processed_messages[i]
            next_msg = processed_messages[i + 1]
            
            # Case 1: Current message has tool_calls but next message isn't a tool
            if current_msg.get('tool_calls') and next_msg.get('role') != 'tool':
                # Fix next message to be a tool message
                # next_msg['role'] = 'tool'
                del current_msg['tool_calls']
                current_msg['role'] = 'user'
                
            # Case 2: Current message doesn't have tool_calls but next is a tool
            elif not current_msg.get('tool_calls') and next_msg.get('role') == 'tool':
                # Convert next message to user and remove tool_calls
                next_msg['role'] = 'user'
                if 'tool_calls' in next_msg:
                    del next_msg['tool_calls']
            
            # Case 3: Current message has tool_calls but next message isn't a tool
            elif current_msg.get('role') == 'tool' and next_msg.get('role') == 'tool':
                # Fix next message to be a tool message
                next_msg['role'] = 'user'
        
            
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

import os

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
        
