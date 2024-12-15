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

scheduler = BackgroundScheduler()
scheduler.start()

user_agents: Dict[str, Tuple[autogen.ConversableAgent, autogen.ConversableAgent]] = {}
config_list = [{
        "model": "hertzai-4o",
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
    "system_message": """You are a specialized executor agent focused solely on creating, running and debugging code.
    Your responsibilities:
    1. Execute code provided by the assistant
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


def execute_python_file(job_description:str,user_id: int):
    print('inside calling user agent at time')
    if user_id not in user_agents:
        print('user_id is not present')
    else:
        author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_id]
        current_time = datetime.now()
        text = f'This is the time now {current_time}\n you must perform this task {job_description}'
        agents_object['helper'].initiate_chat(recipient=manager, message=text, clear_history=False,silent=False)
        
        
        #return ther response to user
        


class Action:
    def __init__(self,actions):
        self.actions = actions
        self.current_action = 0
        self.fallback = False
    
    def get_action(self,current_action):
        return self.actions[current_action]

def create_agents(user_id: str,task) -> Tuple[autogen.ConversableAgent, autogen.ConversableAgent]:
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
    
    # Create assistant agent
    assistant = autogen.AssistantAgent(
        name="assistant",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message="""You are a General agent which will perform some action provided by the chat_instructor.
        You should get the clear understanding of the action that you are given, you should complete the action on your own.
        
        If you want to run some code, create the code and ask the executor agent to run it, executor agent will run the code and return back response.
        If you need any information or having some issues or getting some error you should frame the question/error properly and ask user agent about it.
        
        Flow after getting the action from chat_instructor:
        1. perform the action given by the chat_instructor, you can take help from helper and executor
        2. If your action is completed:
            2.A) ask user on what to do if things go wrong at this action in this format {"status":"error","action":"current action","action_id":1,"message":"message here"}after asking this follow the below steps
            i. after asking 2.A pass the conversation to status_verifier agent and ask it to give response in proper format as instructed
            ii. if there is no error ask the status_verifier to return the success response in instructed format
            iii. after user confirmation ask the chat_instructor for new action
            
        3. If you are not able to complete the action:
            i. if there are any error then create error summary and ask user about that error in this format {"status":"error","action":"current action","action_id":1,"message":"message here"}
            ii. never assume anything on your own ask for user help if needed in this format {"status":"error","action":"current action","message":"message here"}
            
        IMPORTANT INSTRUCTION: [helper, status_verifier, User, executor, chat_instructor] all these are agents and not function you can ask them something but can never call them as function call.
        Agents you can talk: helper, status_verifier, User, executor, chat_instructor.
        Tools you can call: create_scheduled_jobs, txt2img.
        For any thing involving delayed/timer based execution or scheduled jobs/continous monitoring call the create_scheduled_jobs tool it will create a scheduled jobs.
        Remember: Maintain clear communication, prioritize accuracy over speed, and ensure proper handoff when delegating actions.
        Note: Your Working Directory is "/home/hertzai2019/newauto/coding/" use this if you need.
        If you need any information first ask the question and then route the request to user.
        
        """+f"Extra Information: below are the list of actions the chat_manager is gonna give you keep this in mind but dont use this directly\n{task.actions}",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    
    helper = autogen.AssistantAgent(
        name="helper",
        llm_config=llm_config,
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        system_message="""Help the assistant agent to complete the actions do not coordinate with other agents, after your response always pass the conversation to assistant""",
        is_termination_msg=lambda x: True if "TERMINATE" in x.get("content") else False,
    )
    verify = autogen.AssistantAgent(
        name="status_verifier",
        llm_config=llm_config,
        code_execution_config=False,
        system_message=""""You are a status verification agent. You track and verify the status of actions. When asked about an action, you'll check its status and provide updates in a structured JSON format./n
            Response formats:/n
            If action is completed successfully:
            {"status": "completed","action": "current action","action_id": 1,"message": "message here","fallback_action": "fallback action here"  // If no fallback_action is provided, ask user "What measures should be taken if this action fails in the future?" and include their response here}/n
            If there is any error:
            {"status": "error","action": "current action","action_id": 1,"message": "message here"}/n
            If there is any update in action:
            {"status": "updated","action": "current action","updated_action": "updated action","action_id": 1,"message": "message here","fallback_action": ""} // If no fallback_action is provided, ask user "What measures should be taken if this action fails in the future?" and include their response here}/n
            Rules:
            1. For completed actions, always ensure fallback_action is present
            2. If fallback_action is missing, ask user for appropriate fallback measures before providing the response
            3. Only return responses in the above JSON formats
            4. Only verify and report status - do not perform any other actions
            5. Maintain consistent JSON structure as shown above""",
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
        name="executor",
        code_execution_config={"last_n_messages":2,"work_dir": "coding", "use_docker": False},
        **executor_config
    )
    
    chat_instructor = autogen.UserProxyAgent(
        name="chat_instructor",
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
            is_termination_msg=lambda x: True if x.get("content").strip()=='' else False,
            max_consecutive_auto_reply=0,
            code_execution_config=False,
        )
        name.description = i['description']
        custom_agents.append(name)
        agents_object[i['name']] = name
    
    @helper.register_for_execution()
    @assistant.register_for_llm(api_style="function",description="Text to image Creator")
    def txt2img(text: Annotated[str, "Text to create image"]) -> str:
        print('INSIDE TXT2IMG')
        url = f"http://aws_rasa.hertzai.com:5459/txt2img?prompt={text}"

        payload = ""
        headers = {}

        response = requests.post(url, headers=headers, data=payload)
        return response.json()['img_url']
    
    @helper.register_for_execution()
    @assistant.register_for_llm(api_style="function", description="Creates time-based jobs using APScheduler to schedule jobs")
    def create_scheduled_jobs(cron_expression: Annotated[str, "Cron expression for scheduling"], 
                            job_description: Annotated[str, "Description of the job to be performed"],
                            user_id: Annotated[int, "User ID"] = 5) -> str:
        print('INSIDE create_scheduled_jobs')
        if not scheduler.running:
            scheduler.start()
        
        try:
            trigger = CronTrigger.from_crontab(cron_expression)
            job_id = f"job_{int(time.time())}"
            scheduler.add_job(execute_python_file, trigger=trigger, id=job_id, args=[job_description, user_id])
            print('Successfully created scheduler job')
            return 'Successfully created scheduler job'
        except Exception as e:
            print(f'Error in create_scheduled_jobs: {str(e)}')
            return f"Error creating scheduled job: {str(e)}"
        
      
    assistant.description = 'General-purpose agent that coordinates & executes requested tasks & actions'
    executor.description = 'Specialized agent for code execution & response handling'
    author.description = 'Agent focused on user support, error resolution, contextual information. Contact this agent when you need any user based information or if you want to say something to user'
    chat_instructor.description = 'Strategic agent that provides step-by-step action plans for task execution'
    helper.description = 'Support agent that facilitates task completion & assists other agents'
    verify.description = 'After chat_instruction gives instruction & assistant completes it, this agent will provide updates in a structured JSON format and then call user agent'
    
    def state_transition(last_speaker, groupchat):
        messages = groupchat.messages
        if last_speaker == executor or last_speaker == helper or last_speaker == author or last_speaker == chat_instructor:
            return assistant
        json_obj = None
        
        try:
            json_obj = eval(messages[-1]["content"])
            print(f'got json object {json_obj}')
        except:
            # print('it is not a json object')
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
                if 'action_id' in json_obj.keys():
                    print(f'action id in json before update {task.current_action}')
                    task.current_action = int(json_obj['action_id'])
                    print(f'action id in json after update {task.current_action}')
                    
                if json_obj['status'].lower() == 'error':
                    return author
                elif json_obj['status'].lower() == 'completed':
                    if 'action_id' in json_obj.keys():
                        task.actions[json_obj['action_id']-1] = json_obj['action']
                    return chat_instructor
                elif json_obj['status'].lower() == 'updated':
                    if 'action_id' in json_obj.keys():
                        task.actions[json_obj['action_id']-1] = json_obj['updated_action']
                        
                    return chat_instructor
                
            except:
                return 'auto'
        
        if 'TERMINATE' in messages[-1]["content"].upper():
            print('TERMINATING BECAUSE OF TERMINATE')
            # retrieve: action 1 -> action 2
            return None
        else:
            return 'auto'
    
    all_agents = [assistant, executor, author, chat_instructor,helper,verify]
    print(f'len of agent before custom agents {len(all_agents)}')
    all_agents.extend(custom_agents)
    print(f'len of agent after custom agents {len(all_agents)}')
    group_chat = autogen.GroupChat(
        agents=all_agents,
        messages=[],
        max_round=15,
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

def get_response_group(user_id,text):
    
    # Get or create agents for this user
    if user_id not in user_agents:
        author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = create_agents(user_id,task)
        user_agents[user_id] = (author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object)
        messages[user_id] = []
    else:
        author, assistant_agent, executor, group_chat, manager, chat_instructor,agents_object = user_agents[user_id]

    if len(messages[user_id])>0:
        # last_agent, last_message = manager.resume(messages=messages[user_id])
        agents_object['user'].initiate_chat(recipient=manager, message=text, clear_history=False,silent=False)
    else:
        message = '''Chat instructor, please provide the first action.'''
        author.initiate_chat(
            manager,
            message=message,
            speaker_selection={"speaker": "assistant"},
            silent=False
        )
        
    while True:
        print('inside while')
        if group_chat.messages[-1]['name'] == 'chat_instructor' and group_chat.messages[-1]['content'] == 'TERMINATE':
            print('resuming chat')
            if task.current_action==len(task.actions):
                task.current_action += 1
                print(f'Save this actions as final verified actions {task.actions}')
                message = '''Reflect on the sequence and create a recipe containing all the necessary steps and a name for it.
                Provide the response in JSON format as {"status":"completed","recipe": "","scheduled_tasks":[{"cron_expression":"","job_description":""}]}. 
                The recipe should: 
                    Suggest well-documented, generalized Python function(s) to perform similar tasks for coding steps in the future.
                    Avoid storing information directly from the author in the recipe; instead, create placeholders for such variables and use them.
                    Ensure coding steps and non-coding steps are never mixed in the same function.
                    Include detailed docstrings in the function(s) to clarify the non-coding steps required to use the assistant's language skills.'''
                print(f'{message}')
                chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
            else:
                print(f'current action {task.current_action} and fallback {task.fallback}')
                message = task.get_action(task.current_action)
                if task.fallback == True:
                    
                    message = f" Action {task.current_action} fallback:ask user what actions should be taken if current actions fail in the future after you get the response from user give the conversaation to status_verifier"
                else:
                    task.current_action = task.current_action+1
                    message = f'Action {task.current_action}: {message} '
                task.fallback = not task.fallback
                print(f'{message}')
                chat_instructor.initiate_chat(recipient=manager, message=message, clear_history=False,silent=False)
                
        else:
            break
            
        if task.current_action >len(task.actions):
            print(f'current action {task.current_action} is greater than legth {len(task.actions)}')
            break
            
            

    messages[user_id] = group_chat.messages
    last_message = group_chat.messages[-1]
    if last_message['content'] == 'TERMINATE':
        last_message = group_chat.messages[-2]
    return last_message

messages = {}

def recipe(user_id, text,prompt_id):
    try:
        useagent = False
        
        last_response = get_response_group(user_id,text,prompt_id)
        
        return last_response
        
    except Exception as e:
        print(f"Error occurred in create Recipe: {str(e)}")  # Add logging for debugging
        raise
