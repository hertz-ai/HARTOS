from flask import Flask, request, jsonify
from typing import Dict, Tuple
import autogen
import os


# Store user-specific agents and their chat history
user_agents: Dict[str, Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]] = {}

def create_agents_for_user(user_id: str) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
    """Create new assistant and user proxy agents for a user with basic configuration."""
    config_list = [{
        "model": os.getenv("deployment_name"),
        "api_type": "azure",
        "api_key": os.getenv("azure_api_key"),
        "base_url": os.getenv("azure_endpoint"),
        "api_version": "2024-02-15-preview"
    }]

    # Create a basic function calling config
    llm_config = {
        "config_list": config_list,
        "seed": 42
    }

    # Create the assistant agent with context awareness
    assistant = autogen.AssistantAgent(
        name=f"assistant_{user_id}",
        llm_config=llm_config,
        max_consecutive_auto_reply=10,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        code_execution_config={"work_dir": "coding", "use_docker": False},
        system_message='''You are a Helpful Assistant
        '''
    )

    # Create the user proxy agent
    user_proxy = autogen.UserProxyAgent(
        name=f"user_proxy_{user_id}",
        human_input_mode="NEVER",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
        max_consecutive_auto_reply=10,
        code_execution_config={"work_dir": "coding", "use_docker": False}
    )

    return assistant, user_proxy

def get_agent_response(assistant: autogen.AssistantAgent, user_proxy: autogen.UserProxyAgent, message: str) -> str:
    """Get a single response from the agent for the given message."""
    try:
        # Get the current chat history
        current_chat = user_proxy.chat_messages.get(assistant.name, [])
        
        # Create context from previous messages (last 5 messages for efficiency)
        context = current_chat[-5:] if current_chat else []
        context_str = "\n".join([f"{msg['role']}: {msg['content']}" for msg in context])
        
        # Append context to the message if there's history
        enhanced_message = message
        if context:
            enhanced_message = f"Previous conversation:\n{context_str}\n\nCurrent message: {message}"

        # Send message and get response
        response = user_proxy.initiate_chat(assistant, message=enhanced_message)
        
        key = list(user_proxy.chat_messages.keys())[0]
    
        
        return user_proxy.chat_messages[key][-1]['content']

    except Exception as e:
        return f"Error getting response: {str(e)}"


recipe_text = '''This recipe is available for you to reuse..

        <begin recipe>
        #### Steps:
        1. **Get User ID**: Ask the user for the user ID in a conversational style.
        2. **Hit the API**: Perform a POST request to the specified API endpoint with the provided user ID.
        3. **Format Response**: Format the response from the API in a table.

        ### Generalized Python Functions

        Below are the generalized Python functions to perform similar tasks in the future. Note that coding steps and non-coding steps are separated.

        #### Function to Get User ID

        ```python
        def get_user_id() -> int:
            """
            Prompt the user to provide their user ID.

            Returns:
                int: The user ID provided by the user.

            Note:
                This step requires interaction with the user to get the user ID.
            """
            user_id = input("Please provide the user ID: ")
            return int(user_id)
        ```

        #### Function to Hit the API

        ```python
        import requests

        def fetch_user_info(user_id: int) -> dict:
            """
            Fetch user information from the API using the provided user ID.

            Args:
                user_id (int): The user ID to fetch information for.

            Returns:
                dict: The response data from the API.

            Note:
                This step involves making an HTTP POST request to the API endpoint.
            """
            url = 'https://mailer.hertzai.com/getstudent_by_user_id'
            payload = {'user_id': user_id}

            response = requests.post(url, json=payload)
            if response.status_code == 200:
                return response.json()
            else:
                raise Exception(f"Request failed with status code: {response.status_code}")
        ```

        #### Function to Format Response

        ```python
        def format_response(response_data: dict) -> None:
            """
            Format and print the response data in a table format.

            Args:
                response_data (dict): The response data from the API.

            Note:
                This step involves formatting the data for better readability.
            """
            print("| Key                     | Value                                                                                      |")
            print("|-------------------------|--------------------------------------------------------------------------------------------|")
            for key, value in response_data.items():
                print(f"| {key:<24} | {str(value):<90} |")
        ```

        ### Combined Execution Function

        ```python
        def main():
            """
            Main function to execute the sequence of tasks.

            Note:
                This function combines the non-coding and coding steps to perform the entire sequence.
            """
            try:
                # Non-coding step: Get user ID
                user_id = get_user_id()

                # Coding step: Fetch user information
                user_info = fetch_user_info(user_id)

                # Coding step: Format response
                format_response(user_info)

                print("The entire sequence of tasks has been successfully completed.")
            except Exception as e:
                print(f"An error occurred: {e}")
        ```

        ### Usage

        To use the above functions, simply call the `main` function. This function will handle the entire sequence of tasks, ensuring that coding and non-coding steps are separated.

        ```python
        if __name__ == "__main__":
            main()
        ```

        ### Docstring Clarification

        - **Non-coding steps**: These involve interactions with the user or other manual actions that do not require programming, such as getting input from the user.
        - **Coding steps**: These involve writing and executing code, such as making HTTP requests and formatting data.
        </end recipe>


        Here is a new task:
        get data for user_id:
        '''


def chat(user_id,text):
    try:
        use_recipe = True

        # Get or create agents for this user
        if user_id not in user_agents:
            user_agents[user_id] = create_agents_for_user(user_id)

        assistant, user_proxy = user_agents[user_id]

        if use_recipe:
            user_message = recipe_text + text
        # Get response from the agent
        response = get_agent_response(assistant, user_proxy, user_message)

        # Get chat history length for debugging
        history_length = len(user_proxy.chat_messages.get(assistant.name, []))

        return response
    except Exception as e:
        print(f'Some ERROR IN REUSE RECIPE {e}')
        raise
