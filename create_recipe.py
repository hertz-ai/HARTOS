import autogen
from typing import Dict, Tuple
import os
from typing import Annotated
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import requests
import uuid
import time
from typing_extensions import Literal
from autogen.coding import DockerCommandLineCodeExecutor
import re
from autogen import register_function
import json
from autogen import ConversableAgent
from flask import current_app

from crossbarhttp import Client
client = Client('http://aws_rasa.hertzai.com:8088/publish')

scheduler = BackgroundScheduler()
scheduler.start()

user_agents: Dict[str, Tuple[autogen.ConversableAgent, autogen.ConversableAgent]] = {}
config_list = [{
        "model": "gpt-4o-mini",
        "api_type": "azure",
        "api_key": "8f3cd49e1c3346128ba77d09ee9c824c",
        "base_url": "https://hertzai-gpt4.openai.azure.com/",
        "api_version": "2024-02-15-preview"
    }]



executor_config = {
    "llm_config": {
        "config_list": config_list,
        "temperature": 0.4,
    },
    "system_message": """You are a executor agent. focused solely on creating, running and debugging code.
    Your responsibilities:
    1. Execute code provided by the assistant agent
    2. Report execution results, errors, or output
    3. If there are errors:
       - Identify the issue
       - Propose and implement fixes
       - Report back to the assistant
    
    Note: Your Working Directory is "/home/hertzai2019/newauto/coding" use this if you need,
    if you get any sh base command create a bash script add all sh command in that file and run that file to execute it once
    Do not engage in general conversation - that's the assistant's role.
    Add proper error handling, logging.
    Always provide clear execution results or error messages to the assistant.
    if you get any conversation which is not related to coding ask the manager to route this conversation to user"""
}


def send_message_to_user(user_id,response,inp):
    url = 'http://aws_rasa.hertzai.com:9890/autogen_response'
    body = json.dumps({'user_id':user_id,'message':response,'inp':inp})
    headers = {'Content-Type': 'application/json'}
    res = requests.post(url,data=body,headers=headers)
    


def execute_python_file(job_description:str,user_id: int):
    current_app.logger.info('inside calling user agent at time')
    if user_id not in user_agents:
        current_app.logger.info('user_id is not present')
    else:
        author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_id]
        current_time = datetime.now()
        text = f'This is the time now {current_time}\n you must perform this task {job_description}'
        agents_object['helper'].initiate_chat(recipient=manager, message=text, clear_history=False,silent=False)

        last_message = group_chat.messages[-1]
        if last_message['content'] == 'TERMINATE':
            last_message = group_chat.messages[-2]
        
        send_message_to_user(user_id,last_message,task_description)
        


class Action:
    def __init__(self,actions):
        self.actions = actions
        self.current_action = 0
        self.fallback = False
        self.new_json = []
    
    def get_action(self,current_action):
        return self.actions[current_action]

def create_agents(user_id: str,task,prompt_id) -> Tuple[autogen.ConversableAgent, autogen.ConversableAgent]:
    """Create new assistant and user agents for a given user_id"""
    
    llm_config = {
        "temperature": 0.7,
        "config_list": [{
        "model": 'hertzai-4o',
        "api_type": "azure",
        "api_key": '8f3cd49e1c3346128ba77d09ee9c824c',
        "base_url": 'https://hertzai-gpt4.openai.azure.com/',
        "api_version": "2024-02-15-preview"
    }],
    }
    
    custom_agents = []
    agents_object = {}
    
    list_of_persona = [x['name'] for x in details['number_of_persona']]
    # Create assistant agent
    # Create assistant agent
    assistant = autogen.AssistantAgent(
        name="Assistant",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message="""You are an Assistant agent who will execute actions provided by the ChatInstructor.
        You should get a clear understanding of the action that were given, you can take help of Helper and Executor Agents to perform the actions.        
        If you want to run some code, create the code and ask the Executor agent to run it.
        If you need any information or have some issues or getting some error you should frame the question/error properly and ask user agent about it.
        If the action says to create a continous scheduled task you should confirm that it is not too frequent and confirm the user about it before creating the scheduled task using the function tool.
        Ask user to Associate each action to a persona and if there are multiple persona then give response to every persona associated to action based on action.
        For every action you ask the user for which persona this action is related
        list of persona name available: """+f"{list_of_persona}"+""" // use this only while associating persona_name to action 
        Flow after getting every new action from ChatInstructor. Never assume and Always Ask the user(tag the user e.g. @user only do this for user) that this action(also mention the action you got from ChatInstructor) is related to which persona and then proceed completing the action
        1. perform the action given by the ChatInstructor, you can take help from helper and executor
        2. If your action is completed:
            2.A) ask user on what to do if things go wrong at this action response format {"status":"error","action":"current action","action_id":1,"message":"message here"}after asking this follow the below steps
            i. after asking 2.A pass the conversation to StatusVerifier agent and ask it to give response in proper format as instructed
            ii. if there is no error ask the StatusVerifier to return the completed response in instructed format {"status":"completed","action":"current action","action_id":1,"message":"message here","persona_name":"persona name this action belongs to"}
            iii. after user confirmation ask the ChatInstructor for new action
            
        3. If you are not able to complete the action:
            i. if there are any error then create error summary and ask user about that error response format {"status":"error","action":"current action","action_id":1,"message":"message here"}
            ii. never assume anything on your own ask for user help if needed response format {"status":"error","action":"current action","message":"message here"}
            
        Capabilites you have: You can see user via user camera, if visual camera feed not available then request user to turn on camera to perform an action which requires camera input, All your responses to user are played as if you are talking in video call(with talking head animation & audio via TTS).

        For anything involving delayed/timer/scheduled tasks or scheduled jobs/continous monitoring call the create_scheduled_jobs tool it will create a scheduled job.
        Remember: Maintain clear communication, prioritize accuracy over speed, and ensure proper handoff when delegating tasks.
        Note: Your Working Directory is "/home/hertzai2019/newauto/coding/" use this if you need it.
        If you need any information ask the question to the user(tag the user e.g. @user only do this for user).
        Speak casual, occasionally playful, & respectful, while keeping it natural, funny, colloquial, & relatable. Expressions should be clear, accurate, grammatically, & contextually correct, avoiding tense confusion. Switch to a more formal tone only if the user keeps it formal.
        """+f"Extra Information: below are the list of actions the chat_manager is gonna give you keep this in mind but dont use this directly\n{user_tasks[user_id].actions}",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    helper = autogen.AssistantAgent(
        name="Helper",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message="""You are Helper Agent, Help the assistant agent to complete the actions do not coordinate with other agents, after your response always pass the conversation to assistant""",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    verify = autogen.AssistantAgent(
        name="StatusVerifier",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=""""You are a status verification agent. You track and verify the status of actions. When asked about an action, you'll check its status and provide updates in a structured JSON format./n
        list of persona name available: {{list_of_persona}} // use this only while associating persona_name to action
        Response formats:/n
        If action is completed successfully:
        {"status": "completed","action": "current action","action_id": 1,"message": "message here","persona_name":"persona name this action belongs to","fallback_action": "fallback action here"  // If no fallback_action is provided, ask user "What measures should be taken if this action fails in the future?" and include their response here}/n
        If there is any error:
        {"status": "error","action": "current action","action_id": 1,"message": "message here"}/n
        If there is any update in action:
        {"status": "updated","action": "current action","updated_action": "updated action","action_id": 1,"message": "message here","persona_name":"persona name this action belongs to","fallback_action": "","entire_actions":[return all actions ask assistant to give array of all actions]} // If no fallback_action is provided, ask user "What measures should be taken if this action fails in the future?" and include their response here}/n
        Rules:
        1. For completed actions, always ensure fallback_action is present
        2. If fallback_action is missing, ask user for appropriate fallback measures before providing the response
        3. Only return responses in the above JSON formats
        4. Only verify and report status - do not perform any other actions
        5. Maintain consistent JSON structure as shown above"""+f"\nExtra Information: below are the list of actions the chat_manager is gonna give you keep this in mind but dont use this directly only use this is there is any update in any action and you want to update the actions and return the entire array as entire_actions\n{user_tasks[user_id].actions}",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    author = autogen.UserProxyAgent(
        name="User",
        human_input_mode="NEVER",
        is_termination_msg=lambda x: True if x.get("content").strip()=='' else False,
        max_consecutive_auto_reply=0,
        code_execution_config=False,
    )
    
    executor = autogen.AssistantAgent(
        name="Executor",
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        **executor_config
    )
    
    chat_instructor = autogen.UserProxyAgent(
        name="ChatInstructor",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        default_auto_reply="TERMINATE",
        code_execution_config=False,
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    agents_object['assistant'] = assistant
    agents_object['helper'] = helper
    agents_object['author'] = author
    agents_object['user'] = author
    agents_object['executor'] = executor
    agents_object['verify'] = verify
    agents_object['chat_instructor'] = chat_instructor
    
    for i in details['number_of_persona']:
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
    executor.register_for_execution(name="text_2_image")(txt2img)
    
    
    def recent_files() -> str:
        current_app.logger.info('INSIDE recent_files')
        if recent_file_id[user_id]:
            return f'Got user uploaded file the file_id is {recent_file_id[user_id]}'

        return 'No file uploaded from user'
    
    helper.register_for_llm(name="get_recent_files", description="get user's recent uploaded files")(recent_files)
    executor.register_for_execution(name="get_recent_files")(recent_files)
    

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
    
    helper.register_for_llm(name="get_image_from_text", description="Image to Text")(img2txt)
    executor.register_for_execution(name="get_image_from_text")(img2txt)
    
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
        
    helper.register_for_llm(name="create_scheduled_jobs", description="Creates time-based jobs using APScheduler to schedule jobs")(create_scheduled_jobs)
    executor.register_for_execution(name="create_scheduled_jobs")(create_scheduled_jobs)
      
    assistant.description = 'this is an assistant agent that coordinates & executes requested tasks & actions'
    executor.description = 'this is an executor agent that Specialized agent for code execution & response handling'
    author.description = 'this is an author/user agent that focused on user support, error resolution, contextual information. Contact this agent when you need any user based information or if you want to say something to user'
    chat_instructor.description = 'this is a ChatInstructor agent that provides step-by-step action plans for task execution'
    helper.description = 'this is a helper agent that facilitates task completion & assists other agents'
    verify.description = 'this is a verify status agent. which will verify the status of current action that will be called after ChatInstructor gives instruction to complete an action & assistant completes it, this agent will provide updates in a structured JSON format and then call user agent'
    
    def state_transition(last_speaker, groupchat):
        current_app.logger.info('Inside state_transition')
        messages = groupchat.messages
        current_app.logger.info(f'Inside state_transition with message {messages[-1]["content"]} and last_speaker:{last_speaker.name}')
        crossbar_message = {"text": [messages[-1]["content"]], "priority": 99, "action": 'Agent', "historical_request_id": [], "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
        'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0}, 'bottom_left': {'x': 0, 'y': 0}}}
        result = client.publish(
            f"com.hertzai.hevolve.chat.{user_id}", crossbar_message)
        
        try:
            json_obj = eval(messages[-1]["content"])
            current_app.logger.info(f'got json object {json_obj}')
        except:
            json_obj = None
            # current_app.logger.info('it is not a json object')
            pass
        if not json_obj:
            try:
                json_match = re.search(r'{[\s\S]*}', messages[-1]["content"])
                if json_match:
                    json_part = json_match.group(0)
                    json_obj = json.loads(json_part)
            except:
                pass
        if json_obj:
            try:   
                if json_obj['status'].lower() == 'error':
                    return author
                elif json_obj['status'].lower() == 'completed' or json_obj['status'].lower() == 'success':
                    if 'recipe' in json_obj.keys():
                        current_app.logger.info('Recipe created successfully')
                        name = f'prompts/{prompt_id}_recipe.json'
                        with open(name, "w") as json_file:
                            json.dump(json_obj, json_file)
                        current_app.logger.info(f"Dictionary saved to {name}")
                    if 'action_id' in json_obj.keys():
                        user_tasks[user_id].actions[json_obj['action_id']-1] = json_obj['action']
                        user_tasks[user_id].new_json.append(json_obj)
                    return chat_instructor
                elif json_obj['status'].lower() == 'updated':
                    if 'entire_actions' in json_obj.keys() and type(json_obj['entire_actions'])==list:
                        entire_actions = json_obj['entire_actions']
                        user_tasks[user_id].actions = entire_actions
                        user_tasks[user_id].current_action = 0
                        user_tasks[user_id].fallback = False
                    elif 'action_id' in json_obj.keys():
                        user_tasks[user_id].actions[json_obj['action_id']-1] = json_obj['updated_action']
                        user_tasks[user_id].new_json.append(json_obj)
                        
                    return chat_instructor   
            except:
                pass
        
        if last_speaker.name == 'Executor' or last_speaker.name == 'User' or last_speaker.name == 'user' or last_speaker.name == 'ChatInstructor':
            current_app.logger.info('Got last speaker as executor or helper or author or chat_instructor and reutrning next speaker as assistant')
            return assistant
        json_obj = None
        
        if last_speaker == verify:
            current_app.logger.info('Got last speaker as verify_status and returning next speaker as chat_instructor')
            return chat_instructor
        try:
            if messages[-1]["content"] == '':
                current_app.logger.info(f'Got content as blank {messages[-1]}')
                return 'auto'
        except Exception as e:
            current_app.logger.error(f'Got error when content as blank with error as :{e}')
        #checking if message is routed to user via tag
        pattern = r"@user"   
        try:
            if re.search(pattern, messages[-1]["content"]):
                current_app.logger.info("String contains @user returnng author")
                messages[-1]["content"] = messages[-1]["content"].replace('@user','')
                return author
        except Exception as e:
            current_app.logger.error(f'Got error when searching for @user in last message :{e}')

        
        if 'TERMINATE' in messages[-1]["content"].upper():
            current_app.logger.info('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        else:
            return 'auto'
    
    
    all_agents = [assistant, executor, author, chat_instructor,helper,verify]
    current_app.logger.info(f'len of agent before custom agents {len(all_agents)}')
    all_agents.extend(custom_agents)
    current_app.logger.info(f'len of agent after custom agents {len(all_agents)}')
    group_chat = autogen.GroupChat(
        agents=all_agents,
        messages=[],
        max_round=15,
        # select_speaker_message_template='''You manage a team that Completes a list of Actions provided by ChatInstructor Agent.
        # The Agents available in the team are: Assistant, Helper, Executor, ChatInstructor, StatusVerifier and User''',
        # select_speaker_prompt_template=f"Read the above conversation, select the next person from [Assistant, Helper, Executor, ChatInstructor, StatusVerifier and User] and only return the role as agent.",
        # speaker_selection_method="auto",  # using an LLM to decide
        speaker_selection_method=state_transition,  # using an LLM to decide
        allow_repeat_speaker=False,  # Prevent same agent speaking twice
        send_introductions=True
    )
    
    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config={"config_list": config_list}
    )
    
    
    
    return author, assistant, executor, group_chat, manager, chat_instructor, agents_object

details = { "status": "completed",
"name": "news agent",
"conversational_agent": True,
"number_of_persona": [{"name":"user","description":"user"}],
"goal": "to give news every 5 min",
"flows": [
    {
        "flow_name": "news_agent",
        "actions": [
            "Initiate a conversation by creating the image of some thing and give the image url to user and ask what this image is",
            "evaluate user's respone congratualte or correct the user accordingly and again create a image and follow the initial step",
            "everyday at 12:52 create a image of dragon and give url to user and ask what that image is"
        ],
        "sub_goal": "to give flash card based learning experience to kids with speech delay"
    }
]
}

task = Action(details['flows'][0]['actions'])
user_tasks = {}

def get_response_group(user_id,text,prompt_id):
    
    # Get or create agents for this user
    if user_id not in user_agents:
        author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = create_agents(user_id,user_tasks[user_id],prompt_id)
        user_agents[user_id] = (author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object)
        messages[user_id] = []
    else:
        author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_id]

    if len(messages[user_id])>0:
        # last_agent, last_message = manager.resume(messages=messages[user_id])
        agents_object['user'].initiate_chat(recipient=manager, message=text, clear_history=False,silent=False)
    else:
        message = user_tasks[user_id].get_action(user_tasks[user_id].current_action)
        user_tasks[user_id].fallback = not user_tasks[user_id].fallback
        message = f'Action {user_tasks[user_id].current_action+1}: {message} '
        crossbar_message = {"text": ["Working on "+message+" please wait"], "priority": 99, "action": 'Agent', "historical_request_id": [], "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
        'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0}, 'bottom_left': {'x': 0, 'y': 0}}}
        result = client.publish(
            f"com.hertzai.hevolve.chat.{user_id}", crossbar_message)
        chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
        
    while True:
        current_app.logger.info('inside while')
        if group_chat.messages[-1]['name'] == 'ChatInstructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
            # current_app.logger.info(f"group_chat.messages[-1]['content'] {group_chat.messages[-1]['content']}")
            current_app.logger.info(f"group_chat.messages[-2]['content'] {group_chat.messages[-2]['content']}")
            try:
                json_obj = eval(group_chat.messages[-2]["content"])
                current_app.logger.info(f'got json object {json_obj}')
            except:
                try:
                    json_match = re.search(r'{[\s\S]*}', messages[-1]["content"])
                    if json_match:
                        json_part = json_match.group(0)
                        json_obj = json.loads(json_part)
                except:
                    current_app.logger.info('it is not a json object You should ask status verifier to give response in proper format and not move ahead to next action')
                    message = 'StatusVerifier Agent Please verify the status of the action performed. Respond in the following format {"status": "status here","action": "current action","action_id": 1,"message": "message here","fallback_action": "fallback action here"}'
                    assistant_agent.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                    continue
            current_app.logger.info('resuming chat')
            if user_tasks[user_id].current_action==len(user_tasks[user_id].actions):
                user_tasks[user_id].new_json.append(json_obj)
                user_tasks[user_id].current_action += 1
                name = f'prompts/{prompt_id}_new.json'
                with open(name, "w") as json_file:
                    json.dump(user_tasks[user_id].new_json, json_file)
                current_app.logger.info('updating updated action in .json')
                details['flows'][0]['actions'] = user_tasks[user_id].actions
                name = f'prompts/{prompt_id}.json'
                with open(name, "w") as json_file:
                    json.dump(details, json_file)
                current_app.logger.info(f'Save this actions as final verified actions {user_tasks[user_id].actions}')
                message = '''Reflect on the sequence and create a recipe containing all the necessary steps and a name for it.
                Provide the response in JSON format as:
                {"status":"completed","steps":[{ "action": "Action here", "action_id": 1, "persona": "the persona this action belongs to" }],"recipe": "","scheduled_tasks":[{"cron_expression":"","job_description":""}]}. 
                The recipe should: 
                    Suggest well-documented, generalized Python function(s) to perform similar tasks for coding steps in the future.
                    Avoid storing information directly from the author in the recipe; instead, create placeholders for such variables and use them.
                    Ensure coding steps and non-coding steps are never mixed in the same function.
                    Include detailed docstrings in the function(s) to clarify the non-coding steps required to use the assistant's language skills.'''
                current_app.logger.info(f'{message}')
                chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
            else:
                user_tasks[user_id].current_action = json_obj['action_id']
                current_app.logger.info(f'current action {user_tasks[user_id].current_action} and fallback {user_tasks[user_id].fallback}')
                user_tasks[user_id].new_json.append(json_obj)
                message = user_tasks[user_id].get_action(user_tasks[user_id].current_action)
                if user_tasks[user_id].fallback == True:
                    
                    message = f" Action {user_tasks[user_id].current_action} fallback:ask user what actions should be taken if current actions fail in the future after you get the response from user give the conversaation to StatusVerifier agent"
                else:
                    user_tasks[user_id].current_action = user_tasks[user_id].current_action+1
                    message = f'Action {user_tasks[user_id].current_action}: {message} '
                user_tasks[user_id].fallback = not user_tasks[user_id].fallback
                current_app.logger.info(f'{message}')
                crossbar_message = {"text": ["Working on "+message], "priority": 99, "action": 'Agent', "historical_request_id": [], "preffered_language": 'en-US', "options": [], "newoptions": [], "bot_type": 'Agent', "page_image_url": "", "analogy_image_url": '', "request_id": "123456", "zoom_bounding_box": {
                'top_left': {'x': 0, 'y': 0}, 'top_right': {'x': 0, 'y': 0}, 'bottom_right': {'x': 0, 'y': 0}, 'bottom_left': {'x': 0, 'y': 0}}}
                result = client.publish(
                    f"com.hertzai.hevolve.chat.{user_id}", crossbar_message)
                chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                
        else:
            break
            
        if user_tasks[user_id].current_action >len(user_tasks[user_id].actions):
            current_app.logger.info(f'current action {user_tasks[user_id].current_action} is greater than legth {len(user_tasks[user_id].actions)}')
            break            

    messages[user_id] = group_chat.messages
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]
    return last_message['content']

messages = {}
recent_file_id = {}

def recipe(user_id, text,prompt_id,file_id):
    current_app.logger.info('--'*100)
    if file_id:
            recent_file_id[user_id] = file_id

    if user_id not in user_tasks.keys():
        with open(f"prompts/{prompt_id}.json", 'r') as f:
            config = json.load(f)
        user_tasks[user_id] = Action(config['flows'][0]['actions'])
    try:
        
        last_response = get_response_group(user_id,text,prompt_id)
        
        try:
            json_response = eval(last_response)
            if 'status' in json_response.keys(): 
                if 'recipe' in json_response.keys():
                    return 'Agent Created Successfully'
                else:
                    return json_response['message']
            
        except:
            pass
        return last_response
        
    except Exception as e:
        current_app.logger.info(f"Error occurred in create Recipe: {str(e)}")  # Add logging for debugging
        raise
