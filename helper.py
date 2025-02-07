from collections import deque
import requests
import re
import json
from flask import current_app

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