from langchain import OpenAI, LLMChain, PromptTemplate
from langchain.agents import (
    ZeroShotAgent, Tool, AgentExecutor, ConversationalAgent,
    ConversationalChatAgent, LLMSingleActionAgent, AgentOutputParser,
    load_tools, initialize_agent, AgentType
)
from langchain.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate
)
from langchain.chains import LLMMathChain, OpenAPIEndpointChain
from langchain.chains.conversation.memory import ConversationSummaryMemory, ConversationBufferWindowMemory
from langchain.chains.openai_functions.openapi import get_openapi_chain
from langchain.chat_models import ChatOpenAI
from langchain.experimental.plan_and_execute import PlanAndExecute, load_agent_executor, load_chat_planner
from langchain.llms import OpenAI, OpenAIChat
from langchain.llms.base import LLM
from langchain.memory import ConversationBufferMemory, ReadOnlySharedMemory, ZepMemory
from langchain.requests import Requests
from langchain.schema import AgentAction, AgentFinish, OutputParserException, HumanMessage, AIMessage, SystemMessage
from langchain.tools import OpenAPISpec, APIOperation, StructuredTool
from langchain.tools.python.tool import PythonREPLTool
from langchain.utilities import GoogleSearchAPIWrapper
from flask import Flask, jsonify, request
import json
import os
import re
import logging
import requests
import pytz
from datetime import datetime, timezone
from typing import List, Union, Optional, Mapping, Any
from langchain.agents.conversational_chat.output_parser import ConvoOutputParser
import time
import tiktoken
from pytz import timezone
from datetime import datetime
from waitress import serve
import logging
from logging.handlers import RotatingFileHandler


## logging info
logging.basicConfig(level=logging.DEBUG)
handler = RotatingFileHandler('flask_app.log', maxBytes=100000, backupCount=3)

# Set the logging level for the file handler
handler.setLevel(logging.DEBUG)

# Create a logging format
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)


user_id = 0
recognized_intent = []
req_total_tokens = 0
res_total_tokens = 0
encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")
#api and keys

os.environ["OPENAI_API_KEY"] = "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"
os.environ["GOOGLE_CSE_ID"] = "9589161c491c4493e"
os.environ["GOOGLE_API_KEY"] = "AIzaSyCTEiyRiS8mfZlUp3Lc1JwmmyK4sZI_8Lo"
os.environ["NEWS_API_KEY"] = "291350f6b8fd4df982f343888a4cabd5"
os.environ["SERPAPI_API_KEY"] = "15916f6b8a0a976ab7f92ed1c4e3bc9bb40c73b40404ad2bbf219c5091394cb0"
search = GoogleSearchAPIWrapper(k=4)
ZEP_API_URL = "http://4.224.46.164:8000"
ZEP_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.5NTb669plV5rLNHGQ5qKh2eLuTa5sWQn2w3BVnxAD1I"

#openAPI spec
spec = OpenAPISpec.from_file(
    "./openapi.yaml"
)



#custom GPT
class CustomGPT(LLM):

    count:int = 0
    previous_intent: Optional[str]=None
    call_gpt4:Optional[int]=0
    total_tokens:int = 0


    @property
    def _llm_type(self) -> str:
        return "custom"

    def _call(self, prompt: str, stop: Optional[List[str]] = None) -> str:
        global req_total_tokens
        global res_total_tokens
        start_time = time.time()
        self.count += 1
        # self.total_tokens = 0
        print(f'calling for {self.count} times')

        print("len---->",len(prompt.split(" ")))
        #encoding = tiktoken.get_encoding("gpt-3.5-turbo")
        num_tokens = len(encoding.encode(prompt))
        req_total_tokens += num_tokens
        print("len---->",num_tokens)
        if self.count >= 5 or self.call_gpt4 ==1:
            print("call_gpt",self.call_gpt4)
            # if self.call_gpt4:
            #     print("calling gpt 4 while redundant intent")
            # else:
            #     print("counter reach limit of 5")
            response = requests.post(
                "http://aws_rasa.hertzai.com:5454/chat",
                json={
                "model": "gpt-4",
                "data": [{"role":"user","content":prompt}],
                "max_token":1000
                }
            )
        else:
            response = requests.post(
                "http://aws_rasa.hertzai.com:5454/chat",
                json={
                "model": "gpt-3.5-turbo",
                "data": [{"role":"user","content":prompt}],
                "max_token":1000
                }
            )

        response.raise_for_status()
        print("hellpppppppppppppppp-->", response.json()["text"])
        global recognized_intent
        try:
            intents = json.loads(response.json()["text"])
            curr_intent = intents["action"]
            # print("current_intent",curr_intent,"previous_intent",self.previous_intent)
            if self.previous_intent == curr_intent:
                self.call_gpt4 = 1
            self.previous_intent = curr_intent
            recognized_intent.append(intents["action"])
        except:
            recognized_intent=["Final Answer"]
        # time.sleep(10)

        end_time = time.time()
        elapsed_time = end_time - start_time
        print("time taken for this call is", elapsed_time)
        num_tokens = len(encoding.encode(response.json()["text"].replace('\n', ' ').replace('\t', '')))
        res_total_tokens += num_tokens
        return response.json()["text"].replace('\n', ' ').replace('\t', '')

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {

        }

#helper functions
def get_memory(user_id:int):
    session_id = "user_"+str(user_id)
    memory = ZepMemory(
        session_id=session_id,
        url=ZEP_API_URL,
        memory_key="chat_history",
        api_key=ZEP_API_KEY,
        return_messages=True
    )
    return memory

def get_action_user_details(user_id):


    action_url = f"http://aws_hevolve.hertzai.com:6006/action_by_user_id?user_id={user_id}"

    payload = {}
    headers = {}

    response = requests.request(
        "GET", action_url, headers=headers, data=payload)

    unwanted_actions=['Casual Conversation', 'Topic confirmation', 'Topic not found', 'Topic Confirmation', 'Topic Listing', 'Probe', 'Question Answering', 'Fallback']
    data = response.json()
    action_texts = [obj["action"] + ' on '+ obj["created_date"] for obj in data if obj["action"] not in unwanted_actions]
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

    actions = actions + ". List of actions ends. <PREVIOUS_USER_ACTION_END> \n " + "Today's datetime in "+time_zone + "is: "+  formatted_time +  " in this format:'%Y-%m-%dT%H:%M:%S.%f' \n Whenever user is asking about current date or current time at perticular location then use this datetime format. Use the previous sentence datetime info to answer current time based questions coupled with google_search for current time or full_history for historical conversation based answers. Take a deep breath and think step by step.\n"



    # user detail api

    url = "http://aws_hevolve.hertzai.com:6006/getstudent_by_user_id"
    payload = json.dumps({
        "user_id": user_id
    })
    headers = {
        'Content-Type': 'application/json'
    }
    response = requests.request("POST", url, headers=headers, data=payload)
    # print()

    user_data = response.json()

    user_details = f'''Below are the information about the user.
    user_name: {user_data["name"]} (Call the user by this name only when required and not always),gender: {user_data["gender"]}, who_pays_for_course: {user_data["who_pays_for_course"]}(Entity Responsible for Paying the Course Fees), preferred_language: {user_data["preferred_language"]}(User's Preferred Language), date_of_birth: {user_data["dob"]}, english_proficiency: {user_data["english_proficiency"]}(User's English Proficiency Level), created_date: {user_data["created_date"]}(user creation date), standard: {user_data["standard"]}(User's Standard in which user studying)
   '''
    return user_details, actions

def get_time_based_history(prompt:str, session_id:str, start_date:str, end_date:str):
    ZEP_API_URL = "http://4.224.46.164:8000"
    # print(type(start_date))
    start_time = time.time()
    memory = ZepMemory(
        session_id=session_id,
        url=ZEP_API_URL,
        api_key=ZEP_API_KEY,
        memory_key="chat_history",
    )


    # messages = [message.message["content"] for message in messages if message.dist>0.8 and message.message["role"]!="system" and message.message["role"]!="ai"]

    try:

        metadata={
            "start_date": start_date,
            "end_date":  end_date
        }
        #    "where": {"jsonpath": '$.system.entities[*] ? (@.Label == "WORK_OF_ART")'},


        messages = memory.chat_memory.search(prompt,metadata=metadata)

        print("messages----->", messages)

        #filtered_messages = [[message.message['content'] for message in messages if message.message["role"]!="system" and datetime.fromisoformat(start_date.replace('Z', '+00:00')).replace(tzinfo=timezone.utc) <= datetime.fromisoformat(message.message['created_at'].replace('Z', '+00:00')).replace(tzinfo=timezone.utc) <= datetime.fromisoformat(end_date.replace('Z', '+00:00')).replace(tzinfo=timezone.utc) and message.dist>0.8 ]]
        #filtered_messages = [message.message['content'] for message in messages if message.message["role"] != "system" and
        #                 datetime.strptime(start_date, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc) <=
        #                 datetime.strptime(message.message['created_at'], '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc) <=
        #                 datetime.strptime(end_date, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc) and
        #                 message.dist > 0.8 ]
        #print("filter_messages ----->",filtered_messages)
        final_res = {'res_in_filter':messages}
        print(final_res)
        end_time = time.time()
        elapsed_time = end_time - start_time
        print("time taken for zep is", elapsed_time)
        return json.dumps(final_res)
    except:
        #return [message.message['content'] for message in messages]
        messages = memory.chat_memory.search(prompt)
        # print(final_res)
        end_time = time.time()
        elapsed_time = end_time - start_time
        print("time taken for zep is", elapsed_time)
        return json.dumps({'res':[message.message['content'] for message in messages]})


def parsing_string(string):
    try:
        prompt, start_date, end_date = [s.strip() for s in string.split(",")]
        global user_id
        session_id = 'user_'+str(user_id)
        return get_time_based_history(prompt, session_id, start_date, end_date)
    except:
        # Get the current time
        now = datetime.utcnow()

        # Format the time in the desired format
        formatted_time = now.strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'
        session_id = "user_"+str(user_id)
        return get_time_based_history(string, session_id, formatted_time, formatted_time)




def parse_character_animation(string):
    try:
        global user_id
        prompt = string
        student_id_url = "http://aws_hevolve.hertzai.com:6006/getstudent_by_user_id"

        payload = json.dumps({
        "user_id": user_id
        })
        headers = {
        'Content-Type': 'application/json'
        }

        response = requests.request("POST", student_id_url, headers=headers, data=payload)
        favorite_teacher_id = response.json()["favorite_teacher_id"]

        get_image_by_id_url = f"http://aws_hevolve.hertzai.com:6006/get_image_by_id/{favorite_teacher_id}"

        payload = {}
        headers = {}

        response = requests.request("GET", get_image_by_id_url, headers=headers, data=payload)

        image_name=response.json()["image_name"]

        image_name = image_name.replace("vtoonify_", "", 1)
        folder_name = image_name.split(".")[0]

        inference_url = "http://aws_panohead.hertzai.com:5055/generate_images"
        payload = json.dumps({
            "weights_dir": f"/home/ubuntu/content/{folder_name}/stable_diffusion_weights/zwx/800",
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
    try:
        
        url = f'http://aws_rasa.hertzai.com:5459/txt2img?prompt={inp}'
        payload = {}

        headers = {}
        response = requests.request("POST", url, headers=headers, data=payload)
        return response.json()["img_url"]
    except Exception as e:
        return f"{e} Not able to generating image at this moment please try later"
        
        


#constants
chain = get_openapi_chain(spec)
#llm = ChatOpenAI(model_name="gpt-3.5-turbo-16k")
#llm = ChatOpenAI(temperature=0, model="gpt-4")
#llm = CustomGPT()
llm_math = LLMMathChain(llm=ChatOpenAI(model_name="gpt-3.5-turbo"))



# output parser
# from __future__ import annotations

from typing import Union

from langchain.agents import AgentOutputParser
from langchain.agents.conversational_chat.prompt import FORMAT_INSTRUCTIONS
from langchain.output_parsers.json import parse_json_markdown
from langchain.schema import AgentAction, AgentFinish, OutputParserException


class CustomConvoOutputParser(AgentOutputParser):
    """Output parser for the conversational agent."""

    def get_format_instructions(self) -> str:
        return FORMAT_INSTRUCTIONS

    def parse(self, text: str) -> Union[AgentAction, AgentFinish]:
        try:
            response = parse_json_markdown(text)
            action, action_input = response["action"], response["action_input"]
            if action == "Final Answer":
                return AgentFinish({"output": action_input}, text)
            else:
                return AgentAction(action, action_input, text)
        except Exception as e:
            # str = ""
            print(text)
            time.sleep
            if '"Final Answer"' in text:
                # Extract the JSON part from the string
                escape_chars = ['\n', '\t', '\r', '\"', "\'", '\\', "'''", '"""']
                start_index = text.index('{')
                try:
                    end_index = text.rindex('}') + 1
                except:
                    text += '"}'
                    end_index = text.rindex('}') + 1
                json_string = text[start_index:end_index]
                try:
                    parsed_json = parse_json_markdown(json_string)
                except Exception as e:
                    parsed_json = parse_json_markdown(json_string.replace('\n', '').replace('\t', '').replace('\r', '').replace('\"', '').replace("\'", '').replace('\\', '').replace("'''", '').replace('"""', '').replace('`',''))
                action_input = parsed_json["action_input"]
                return AgentFinish({"output": action_input}, text)
                # print(action_input_text)
            else:
                print(text)
                start_index = text.index('{')
                try:
                    end_index = text.rindex('}') + 1
                except:
                    text += '"}'
                    end_index = text.rindex('}') + 1
                json_string = text[start_index:end_index]
                response = parse_json_markdown(json_string)
                action, action_input = response["action"], response["action_input"]
                return AgentAction(action, action_input, text)
                # raise OutputParserException(f"Could not parse LLM output: {text}") from e

    @property
    def _type(self) -> str:
        return "conversational_chat"





# main function
def get_ans(user_id, query):
    user_details, actions = get_action_user_details(user_id=user_id)
    llm = CustomGPT()
    # memory = ConversationSummaryMemory(llm=llm, memory_key="chat_history",
    #     return_messages=True)
    print("query------>",query)
    memory=get_memory(user_id=user_id)
    tools = load_tools(["google-search"])
    #calling_animation = f"use this for character animation use this user {user_id}, and extract prompt from user"
    tool = [
        Tool(
            name='Calculator',
            func=llm_math.run,
            description='Useful for when you need to answer questions about math.'
        ),
        Tool(
            name="OpenAPI_Specification",
            func=chain.run,
            description="Use this feature only when the user's request specifically pertains to one of the following scenarios:\
            Image Creation: When a request involves generating an image using text, this feature should be engaged. The entire text prompt must be used as it is unless otherwise requested to enhance further detail of prompt for the image generation process. If additional enahancement is needed , enrich the prompt to image generation with greater detail for learning.\
            Student Information: If a request is made for information regarding students, this functionality should be utilized to retrieve the necessary details.\
            Query Available Books: When the user is inquiring about available books, this feature should be used to locate and provide information about the required texts.\
            Any CRUD operation which is not a READ or anything related to curriculum should not use this tool,  It is vital to ensure that the intent precisely falls within one of the above  categories before engaging this functionality.\
            Don't use this to create a custom curriculum for user"
        ),
        Tool(
            name="FULL_HISTORY",
            func=parsing_string,
            description=f"""Utilize this utility exclusively when the information required predates the current day and pertains to the ongoing user. The necessary input for this tool comprises a list of values separated by commas.
            The list should encompass a user-generated query, designated by user input text, a commencement date denoted as start_date, and an end date labeled as end_date. The start_date denotes the initiation date for the user information search and should consistently adhere to the ISO 8601 format. Meanwhile, the end_date, also conforming to the ISO 8601 format, signifies the conclusion date for the search.
            In cases where the end_date is indeterminable, the current datetime should be employed. For example, if the objective is to retrieve a user's dialogue spanning from the preceding day up to the present day (assuming today's date is 2023-07-13T10:19:56.732291Z), the input would resemble: 'what zep can do, 2023-07-12T10:19:56.732291Z, 2023-07-13T10:19:56.732291Z'. Remove any references to time based words like yesterday, today, last year since the date range you provide already accounts for that. e.g. if user has asked what did we discuss the day before yesterday then the text argument should just be what did we discuss followed by  start and end datetime.
            Strive to apply this tool judiciously for scenarios in which retrospective user information is imperative. The inputs should be meticulously arranged  to facilitate the extraction of accurate and pertinent data within the specified timeframe. Never use this tool for so what is the response to my last comment?"""
        ),
        Tool(
            name="Text to image",
            func=parse_text_to_image,
            description="Based on user query generate visual representation of text. Extract prompt from user query and use it as input for function"  
        ),
        Tool(
            name="Animate_Character",
            func=parse_character_animation,
            description='''Use this tool exclusively for animating the selected character or teacher as requested by the user; it is not intended for general requests or for animating random individuals. The user should specify their animation request in a query, such as 'Show me in a spacesuit' or 'Animate yourself as a cartoon standing in front of the Taj Mahal.' Once the request is made, the tool will generate the animation and return a URL link to the user that directs them to the animated image. Note that this tool is specifically designed to handle requests that involve animating a pre-selected character. It should not be used for general image generation tasks that don't pertain to animating the user's chosen character or teacher. For example, if a user queries 'Show me dancing in the rain,' and they have previously selected a specific character or teacher, the tool should be used to generate this animated scenario. However, if the user's request is something like 'Generate an image of a sunset,' which does not directly involve animating the selected character or teacher, then this tool should not be used.'''
        )
    ]
    tools += tool

    #print(type(tools))

    # tools.append(PythonREPLTool())



    prefix = f"""Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

        <GENERAL_INSTRUCTION_START>
        Context:
        Imagine that you are the world's leading teacher, possessing knowledge in every field. Consider the consequences of each response you provide.
        Your answers must be meaningful and delivered as quickly as possible. As a highly educated and informed teacher, you have access to an extensive wealth of information.
        Your primary goal as a teacher is to assist students by answering their questions, providing accurate and up-to-date information.
        Please create a distinct personality for yourself, and remember never to refer to the user as a human or yourself as mere AI.\
        your response should not be more than 200 words.
        <GENERAL_INSTRUCTION_END>
        User details:
        <USER_DETAILS_START>
        {user_details}
        <USER_DETAILS_END>
        <CONTEXT_START>
        Before you respond, consider the context in which you are utilized. You are Hevolve, a highly intelligent educational AI developed by HertzAI.
        You are designed to answer questions, provide revisions, conduct assessments, teach various topics, create personalised curriculum and assist with research for both students and working professionals.
        Your expertise draws from various knowledge sources like books, websites, and white papers. Your responses will be conveyed to the user through a video, using an avatar and text-to-speech technology, and can be translated into various languages.
        Consider the user's location, time and context of previous dialogues with time to create a proper prompt for tools and follow up in-context questions.
        <CONTEXT_END>
        These are all the actions that the user has performed up to now:
        <PREVIOUS_USER_ACTION_START>
        {actions}

        <OUTPUT_FORMAT_INSTRUCTION_START>
        Always format your answer into parsable json format
        example:
        <
            'action':'action taken by agent'
            'action_input':'input for the current action'
        >
        <OUTPUT_FORMAT_INSTRUCTION_END>
        Conversation History:
        <HISTORY_START>
        """
    suffix = """
        <HISTORY_END>
        Only if this above conversation history is not sufficient to fulfill the user's request then use below FULL_HISTORY tool. If results can be accomplished with above information skip tools section and move to format instructions.

        TOOLS

        ------

        Assistant can use tools to look up information that may be helpful in answering the user's
        question. The tools you can use are:

        <TOOLS_START>
        {{tools}}
        <TOOLS_END>
        <FORMAT_INSTRUCTION_START>
        {format_instructions}
        <FORMAT_INSTRUCTION_END>

        always create parsable output

        Here is the User and AI conversation in reverse chronological order:

        USER'S INPUT:
        -------------
        <USER_INPUT_START>
        Latest USER'S INPUT For which you need to respond: {{{{input}}}}
        <USER_INPUT_END>
        """


    TEMPLATE_TOOL_RESPONSE = """TOOL RESPONSE:
        ---------------------
        {observation}

        USER'S INPUT
        --------------------

        Okay, so what is response for this tool. If using information obtained from the tools you must mention it explicitly without mentioning the tool names - I have forgotten all TOOL RESPONSES! Remember to respond with a markdown code snippet of a json blob with a single action, and NOTHING else."""


    prompt = ConversationalChatAgent.create_prompt(
        tools,
        system_message=prefix,
        human_message=suffix
    )


    #chat Agent
    llm_chain = LLMChain(llm=llm, prompt=prompt)

    custom_parser = CustomConvoOutputParser()
    agent = ConversationalChatAgent(llm_chain=llm_chain, tools=tools, verbose=True, output_parser=custom_parser,template_tool_response=TEMPLATE_TOOL_RESPONSE)
    agent_chain = AgentExecutor.from_agent_and_tools(
        agent=agent, tools=tools, verbose=True, memory=memory,
    )
    ans = agent_chain.run(query)
    # agent = initialize_agent(tools, llm, agent=AgentType.OPENAI_FUNCTIONS, verbose=True)
    # ans = agent.run(query)
    global recognized_intent
    global req_total_tokens
    global res_total_tokens
    return ans, recognized_intent, req_total_tokens, res_total_tokens


app = Flask(__name__)


@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()

    global user_id
    user_id = data.get('user_id', None)

    prompt = data.get('prompt', None)
    ans, rec_intent, req_token_count, res_token_count = get_ans(user_id=user_id, query=prompt)
    global res_total_tokens
    global req_total_tokens
    req_total_tokens = 0
    res_total_tokens = 0
    global recognized_intent
    recognized_intent = []

    return jsonify({'response': ans, 'intent':rec_intent, 'req_token_count': req_token_count, 'res_token_count':res_token_count})

@app.route('/add_history', methods=['POST'])
def history():
    data = request.get_json()
    human_msg = data['human_msg']
    ai_msg = data['ai_msg']
    try:
        memory = get_memory(user_id=int(data['user_id']))
    except:
        return "Invalid user ID"
    if memory:
        memory.chat_memory.add_message(
            HumanMessage(content=human_msg),
        )
        memory.chat_memory.add_message(
            AIMessage(content=ai_msg),
        )
        return jsonify({'response':"Messages are saved!!!"}), 200
    else:
        return jsonify({'response':"Memory object not found"}), 400


@app.route('/status', methods=['GET'])
def status():
    return jsonify({'response':'Working...'})




if __name__ == '__main__':
    serve(app, host='0.0.0.0', port=5000)
