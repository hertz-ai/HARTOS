from langchain import OpenAI, LLMChain, PromptTemplate
from langchain.agents import (
    ZeroShotAgent, Tool, AgentExecutor, ConversationalAgent,
    ConversationalChatAgent, LLMSingleActionAgent, AgentOutputParser,
    load_tools, initialize_agent, AgentType
)
import time
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
from langchain_groq import ChatGroq
# from langchain.experimental.plan_and_execute import PlanAndExecute, load_agent_executor, load_chat_planner
from langchain.llms import OpenAI, OpenAIChat
from langchain.llms.base import LLM
from langchain.memory import ConversationBufferMemory, ReadOnlySharedMemory, ZepMemory
from langchain.requests import Requests
from langchain.schema import AgentAction, AgentFinish, OutputParserException, HumanMessage, AIMessage, SystemMessage
from langchain.tools import OpenAPISpec, APIOperation, StructuredTool
# from langchain.tools.python.tool import PythonREPLTool
from langchain.utilities import GoogleSearchAPIWrapper
from flask import Flask, jsonify, request
import json
import os
import re
import logging
import requests
import pytz
from datetime import datetime, timezone
from typing import List, Union, Optional, Mapping, Any, Dict
from langchain.agents.conversational_chat.output_parser import ConvoOutputParser
import time
import tiktoken
from pytz import timezone
from datetime import datetime
from waitress import serve
from logging.handlers import RotatingFileHandler
from typing import Union
from langchain.agents import AgentOutputParser
from langchain.agents.conversational_chat.prompt import FORMAT_INSTRUCTIONS
from langchain.output_parsers.json import parse_json_markdown
from langchain.schema import AgentAction, AgentFinish, OutputParserException
from langchain.tools.requests.tool import RequestsGetTool, TextRequestsWrapper
from pydantic import BaseModel, Field, root_validator
from threadlocal import thread_local_data
import crossbarhttp
from langchain.retrievers.document_compressors import cohere_rerank
import asyncio
import aiohttp
import sys
from threading import Thread
from dotenv import load_dotenv
load_dotenv()
# os.environ['LANGCHAIN_TRACING_V2'] = 'true'
# os.environ['LANGCHAIN_ENDPOINT'] = 'https://api.smith.langchain.com'
# os.environ['LANGCHAIN_API_KEY'] = os.getenv("LANGCHAIN_API_KEY")
# os.environ["LANGCHAIN_PROJECT"] = os.getenv("LANGCHAIN_PROJECT")
groq_api_key = os.environ['GROQ_API_KEY']

class RequestLogRecord(logging.LogRecord):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Safely get the req_id from thread-local storage
        self.req_id = thread_local_data.get_request_id()


## logging info
# Use the custom log record factory
logging.setLogRecordFactory(RequestLogRecord)
logging.basicConfig(level=logging.DEBUG)
stream_handler = logging.StreamHandler(sys.stdout)
handler = RotatingFileHandler('langchain.log', maxBytes=100000, backupCount=0)

# Set the logging level for the file handler
handler.setLevel(logging.DEBUG)

# Create a logging format
req_id = thread_local_data.get_request_id()
formatter = logging.Formatter('%(asctime)s - %(name)s- [RequestID: %(req_id)s] - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

app = Flask(__name__)

app.logger.addHandler(stream_handler)
app.logger.addHandler(handler)

# Test logging
app.logger.info('Logger initialized')

#openAPI spec
spec = OpenAPISpec.from_file(
    "./openapi.yaml"
)



with open("config.json", 'r') as f:
    config = json.load(f)



# global variables
encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")

#api and keys
# app.logger.log(config['OPENAI_API_KEY'])
os.environ["OPENAI_API_KEY"] = config['OPENAI_API_KEY']
os.environ["GOOGLE_CSE_ID"] = config['GOOGLE_CSE_ID']
os.environ["GOOGLE_API_KEY"] = config['GOOGLE_API_KEY']
os.environ["NEWS_API_KEY"] = config['NEWS_API_KEY']
os.environ["SERPAPI_API_KEY"] = config['SERPAPI_API_KEY']
ZEP_API_URL = config['ZEP_API_URL']
ZEP_API_KEY = config['ZEP_API_KEY']
GPT_API = config['GPT_API']
STUDENT_API= config['STUDENT_API']
ACTION_API = config['ACTION_API']
FAV_TEACHER_API = config['FAV_TEACHER_API']
DREAMBOOTH_API= config['DREAMBOOTH_API']
STABLE_DIFF_API = config['STABLE_DIFF_API']
LLAVA_API = config['LLAVA_API']
BOOKPARSING_API = config['BOOKPARSING_API']
CRAWLAB_API = config['CRAWLAB_API']
RAG_API = config['RAG_API']
DB_URL = config['DB_URL']
## task scheduling and logging
from enum import Enum

class TaskStatus(Enum):
    INITIALIZED = "INITIALIZED"
    SCHEDULED = "SCHEDULED"
    EXECUTING = "EXECUTING"
    TIMEOUT = "TIMEOUT"
    COMPELETED = "COMPELETED"
    ERROR = "ERROR"


class TaskNames(Enum):
    GET_ACTION_USER_DETAILS = "GET_ACTION_USER_DETAILS"
    GET_TIME_BASED_HISTORY = "GET_TIME_BASED_HISTORY"
    ANIMATE_CHARACTER = "ANIMATE_CHARACTER"
    STABLE_DIFF = "STABLE_DIFF"
    LLAVA = "LLAVA"
    CRAWLAB = "CRAWLAB"
    USER_ID_RETRIEVER = "USER_ID_RETRIEVER"


# google search API
search = GoogleSearchAPIWrapper(k=4)

#constants
#llm = ChatOpenAI(model_name="gpt-3.5-turbo-16k")
#llm = ChatOpenAI(temperature=0, model="gpt-4")
#llm = CustomGPT()
# The above code is creating an instance of the `LLMMathChain` class with an `open_ai_llm` attribute
# initialized with a `ChatOpenAI` object using the model name "gpt-3.5-turbo".

# llm_math = LLMMathChain(ChatOpenAI(model_name="gpt-3.5-turbo"))
llm_math = LLMMathChain(llm=ChatOpenAI(model_name="gpt-3.5-turbo"))
# llm_math = LLMMathChain(llm= ChatGroq(groq_api_key=groq_api_key,
#                model_name = "mixtral-8x7b-32768"))

llm= ChatGroq(groq_api_key=groq_api_key,model_name = "llama3-70b-8192", temperature=1)

# app.logger.info(llm.invoke("hi how are you?"))

chain = get_openapi_chain(spec)


client = crossbarhttp.Client('http://aws_rasa.hertzai.com:8088/publish')


# create prompt
def create_prompt(tools):
    user_details, actions = get_action_user_details(user_id=thread_local_data.get_user_id())
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

        Conversation History:
        <HISTORY_START>
        """
    suffix = """
        <HISTORY_END>
        Only if this above conversation history is not sufficient to fulfill the user's request then use below FULL_HISTORY tool. Important: If results can be accomplished with above information skip tools section and move to format instructions.

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

        always create parsable output.

        Here is the User and AI conversation in reverse chronological order:

        USER'S INPUT:
        -------------
        <USER_INPUT_START>
        Latest USER'S INPUT For which you need to respond (consult recent history only when needed for more context): {{{{input}}}}
        <USER_INPUT_END>
        """

    prompt = ConversationalChatAgent.create_prompt(
        tools,
        system_message=prefix,
        human_message=suffix,
        input_variables=["input", "agent_scratchpad", "chat_history"]
    )
    # prompt_string = prompt.render()
    # prompt.rende
    return prompt

def get_tools(req_tool, is_first: bool = False):

    if is_first:
        tools = load_tools(["google-search"])
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
                Don't use this to create a custom curriculum for user",


            ),
            Tool(
                name="FULL_HISTORY",
                func=parsing_string,
                description=f"""Utilize this tool exclusively when the information required predates the current day & pertains to the ongoing user query or when there is a need to recall certain things we spoke earlier. The necessary input for this tool comprises a list of values separated by commas.
                The list should encompass a user-generated query, designated by user input text, a commencement date denoted as start_date, and an end date labeled as end_date. The start_date denotes the initiation date for the user information search and should consistently adhere to the ISO 8601 format. Meanwhile, the end_date, also conforming to the ISO 8601 format, signifies the conclusion date for the search.
                In cases where the end_date is indeterminable, the current datetime should be employed. For example, if the objective is to retrieve a user's dialogue spanning from the preceding day up to the present day (assuming today's date is 2023-07-13T10:19:56.732291Z), the input would resemble: 'what zep can do, 2023-07-12T10:19:56.00000Z, 2023-07-13T10:19:56.732291Z'. If query has any form of date or time by user, then start end datetime can be exact rather than till today for more accurate results. Remove any references to time based words (e.g. yesterday, today, datetimes, last year) since the date range you provide already accounts for that. e.g. if user has asked what did we discuss the day before yesterday then the text argument should just be empty since it does not have any named entity for fuzzy search followed by start and end datetime.
                Strive to apply this tool judiciously for scenarios in which retrospective user information is imperative. If Full history tool response is present, forget other histories, the inputs should be meticulously arranged to facilitate the extraction of accurate and pertinent data within the specified timeframe. Never use this tool for what is the response to my last comment?
                Remember whatever user query is regarding search history understand what user is asking about and rephrase it properly then send to tool. Before framing the final tool response from this tool consult corresponding created_at date time to give more accurate response"""
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
            ),
            Tool(
                name="Image_Inference_Tool",
                func=parse_image_to_text,
                description='''When a user provides a query containing an image download URL and a related question about that image, utilize this tool for support. Your objective is to extract both the image URL and the user's inquiry or prompt pertaining to that image from their query, and then convert these elements into comma seperated string. The format should be as follows: "image_url, user_query".
                '''
            ),
            Tool(
                name="Data_Extraction_From_URL",
                func=parse_link_for_crwalab,
                description='''
                Your task is to extract a URL and its type (either 'pdf' or 'website') from a user's query. Upon receiving a query that contains a URL and a specified URL type, you are to use a tool designed for this purpose. The objective is to accurately identify both the URL and its type from the query. Once identified, these elements should be formatted into a comma-separated string, adhering to the format: "url, url_type".
                '''
            ),
            Tool(
                name="User_details_tool",
                func=parse_user_id,
                description="If a request is made for information regarding students or users, this functionality should be utilized to retrieve the necessary details. input for this api should Always be current user_id. Except current user id you should say you cannot have access other user's details."
            )

        ]
        tools += tool
        return tools

    else:
        tools_dict = {1:'google_search', 2:'Calculator', 3:'OpenAPI_Specification', 4:'FULL_HISTORY', 5:'Text to image', 6:'Image_Inference_Tool', 7:'Data_Extraction_From_URL', 8:'User_details_tool'}
        tool_desc = {
            'google_search': '''Search Google for recent results and retrieve URLs that are suitable for web crawling. Ensure that the search responses include the source URL from which the data was extracted. Always present this URL in the response as an HTML anchor tag. This approach ensures clear attribution and easy navigation to the original source for each piece of extracted information. Give urls for the source''',
            'Calculator': '''Useful for when you need to answer questions about math.''',
            'OpenAPI_Specification':'''Use this feature only when the user's request specifically pertains to one of the following scenarios:\
                Image Creation: When a request involves generating an image using text, this feature should be engaged. The entire text prompt must be used as it is unless otherwise requested to enhance further detail of prompt for the image generation process. If additional enahancement is needed , enrich the prompt to image generation with greater detail for learning.\
                Student Information: If a request is made for information regarding students, this functionality should be utilized to retrieve the necessary details.\
                Query Available Books: When the user is inquiring about available books, this feature should be used to locate and provide information about the required texts.\
                Any CRUD operation which is not a READ or anything related to curriculum should not use this tool,  It is vital to ensure that the intent precisely falls within one of the above  categories before engaging this functionality.\
                Don't use this to create a custom curriculum for user''',
            'FULL_HISTORY':'''Utilize this tool exclusively when the information required predates the current day & pertains to the ongoing user query or when there is a need to recall certain things we spoke earlier. The necessary input for this tool comprises a list of values separated by commas.
                The list should encompass a user-generated query, designated by user input text, a commencement date denoted as start_date, and an end date labeled as end_date. The start_date denotes the initiation date for the user information search and should consistently adhere to the ISO 8601 format. Meanwhile, the end_date, also conforming to the ISO 8601 format, signifies the conclusion date for the search.
                In cases where the end_date is indeterminable, the current datetime should be employed. For example, if the objective is to retrieve a user's dialogue spanning from the preceding day up to the present day (assuming today's date is 2023-07-13T10:19:56.732291Z), the input would resemble: 'what zep can do, 2023-07-12T10:19:56.00000Z, 2023-07-13T10:19:56.732291Z'. If query has any form of date or time by user, then start end datetime can be exact rather than till today for more accurate results. Remove any references to time based words (e.g. yesterday, today, datetimes, last year) since the date range you provide already accounts for that. e.g. if user has asked what did we discuss the day before yesterday then the text argument should just be empty since it does not have any named entity for fuzzy search followed by start and end datetime.
                Strive to apply this tool judiciously for scenarios in which retrospective user information is imperative. If Full history tool response is present, forget other histories, the inputs should be meticulously arranged to facilitate the extraction of accurate and pertinent data within the specified timeframe. Never use this tool for what is the response to my last comment?
                Remember whatever user query is regarding search history understand what user is asking about and rephrase it properly then send to tool. Before framing the final tool response from this tool consult corresponding created_at date time to give more accurate response''',
            'Text to image':'''Based on user query generate visual representation of text. Extract prompt from user query and use it as input for function''',
            'Image_Inference_Tool':'''When a user provides a query containing an image download URL and a related question about that image, utilize this tool for support. Your objective is to extract both the image URL and the user's inquiry or prompt pertaining to that image from their query, and then convert these elements into comma seperated string. The format should be as follows: "image_url, user_query".''',
            'Data_Extraction_From_URL':'''Your task is to extract a URL and its type (either 'pdf' or 'website') from a user's query. Upon receiving a query that contains a URL and a specified URL type, you are to use a tool designed for this purpose. The objective is to accurately identify both the URL and its type from the query. Once identified, these elements should be formatted into a comma-separated string, adhering to the format: "url, url_type".''',
            'User_details_tool':'''If a request is made for information regarding students or users, this functionality should be utilized to retrieve the necessary details. input for this api should Always be current user_id. Except current user id you should say you cannot have access other user's details.'''
        }
        tools_func = {
            'google_search':top5_results,
            'Calculator':llm_math.run,
            'OpenAPI_Specificationd':chain.run,
            'FULL_HISTORY':parsing_string,
            'Text to image':parse_text_to_image,
            'Image_Inference_Tool':parse_image_to_text,
            'Data_Extraction_From_URL':parse_link_for_crwalab,
            'User_details_tool':parse_user_id
        }
        if req_tool == "google_search":
            req_tool = "Google Search Snippets"
        if req_tool is not None and req_tool in tools_dict.values():
            tool_description = tool_desc[req_tool]
            tool_func = tools_func[req_tool]
            req_tool_from_user = [
                Tool(
                    name=req_tool,
                    func = tool_func,
                    description=tool_description
                )
            ]
            tools = load_tools(["google-search"])
            tools += req_tool_from_user

        else:
            tool_description = ""
            tool_func=""
            tools = load_tools(["google-search"])
            # tools += req_tool_from_user




        tool = [

            Tool(
                name='Calculator',
                func=llm_math.run,
                description='Useful for when you need to answer questions about math.'
            ),
            Tool(
                name="OpenAPI_Specification",
                func=chain.run,
                description="Use the specialized feature for image generation, student information retrieval, and querying available books, while avoiding its use for non-READ CRUD operations or custom curriculum creation.",
            ),
            Tool(
                name="FULL_HISTORY",
                func=parsing_string,
                description=f"""Use the tool for retrieving historical user information within a specified timeframe, including user-generated queries, start and end dates in ISO 8601 format, and carefully rephrase queries related to search history for accurate responses, avoiding use for responses to the last comment."""
            ),
            Tool(
                name="Text to image",
                func=parse_text_to_image,
                description="Based on user query generate visual representation of text. Extract prompt from user query and use it as input for function"
            ),
            # Tool(
            #     name="Animate_Character",
            #     func=parse_character_animation,
            #     description='''Use this tool exclusively for animating the selected character or teacher as requested by the user; it is not intended for general requests or for animating random individuals. The user should specify their animation request in a query, such as 'Show me in a spacesuit' or 'Animate yourself as a cartoon standing in front of the Taj Mahal.' Once the request is made, the tool will generate the animation and return a URL link to the user that directs them to the animated image. Note that this tool is specifically designed to handle requests that involve animating a pre-selected character. It should not be used for general image generation tasks that don't pertain to animating the user's chosen character or teacher. For example, if a user queries 'Show me dancing in the rain,' and they have previously selected a specific character or teacher, the tool should be used to generate this animated scenario. However, if the user's request is something like 'Generate an image of a sunset,' which does not directly involve animating the selected character or teacher, then this tool should not be used.'''
            # ),
            Tool(
                name="Image_Inference_Tool",
                func=parse_image_to_text,
                description='''Utilize the tool to extract and format both the image URL and the user's inquiry from a query containing an image download URL into a comma-separated string: "image_url, user_query".'''
            ),
            Tool(
                name="Data_Extraction_From_URL",
                func=parse_link_for_crwalab,
                description='''Utilize the designated tool to extract and format a URL and its type (either 'pdf' or 'website') from a user's query into a comma-separated string: "url, url_type".'''
            ),
            Tool(
                name="User_details_tool",
                func=parse_user_id,
                description="Utilize this functionality to retrieve information about students or users, requiring the current user_id as the only acceptable input; access to other user details is not allowed."
            )

        ]
        final_tool = []
        for new_tool in tool:
            if new_tool not in tools:
                final_tool.append(new_tool)

        tools += final_tool

        tool_strings = "\n".join(f"\n> {tool.name}: {tool.description}" for tool in tools)
        return tool_strings

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
        start_time = time.time()
        self.count += 1
        # self.total_tokens = 0
        app.logger.info(f'calling for {self.count} times')

        app.logger.info(f"len---->{len(prompt.split(' '))}")
        #encoding = tiktoken.get_encoding("gpt-3.5-turbo")
        num_tokens = len(encoding.encode(prompt))
        thread_local_data.update_req_token_count(num_tokens)
        app.logger.info(f"len---->{num_tokens}")

        app.logger.info(f"first time calling {len(prompt)}")

        if self.count > 1 and thread_local_data.get_global_intent() != self.previous_intent:
            tools = get_tools(thread_local_data.get_global_intent())
            start_index = prompt.find("<TOOLS_START>")
            end_index = prompt.find("<TOOLS_END>") + len("<TOOLS_END>")
            prompt = prompt[:start_index]+ tools + prompt[end_index:]
            app.logger.info(f"second time calling {len(prompt)}")

            # prompt = create_prompt(tools)
            app.logger.info(prompt)
            # time.sleep(10)
        checker = None
        if self.count > 1 or self.call_gpt4 ==1:
            try:
                # app.logger.info(f"the prompt we are sending is {prompt}")
                start= time.time()
                # response = requests.post(
                #     GPT_API,
                #     json={
                #     "model": "gpt35-turbo-1106",
                #     "data": [{"role":"user","content":prompt}],
                #     "max_token":1000,
                #     "request_id":str(thread_local_data.get_request_id())
                #     })
                # app.logger.info(f"gpt 3.5 response format is {response.json()}")
                # app.logger.info(f"gpt 3.5 response format type is {type(response.json())}")
                # app.logger.info("finish in {}".format(time.time()-start))
                response_from_groq = llm.invoke(prompt)
                # app.logger.info("groq response in streaming way")
                # app.logger.info("\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n")
                # for chunk in llm.stream(prompt):
                #     print(chunk.content, end="", flush=True)
                # app.logger.info("\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n")
                # app.logger.info(f" response from groq api {response}")
                # app.logger.info(f" response from groq api {type(response)}")
                response = json.loads(response_from_groq.content)
                app.logger.info(f" response from groq api after {response}")
                app.logger.info(f" response from groq api after {type(response)}")
                
                app.logger.info("finish in groq {}".format(time.time()-start))
                checker = 0
            except Exception as e:
                app.logger.info(f"In except the exception is {e}")
                start= time.time()
                response = requests.post(
                    GPT_API,
                    json={
                    "model": "gpt35-turbo-1106",
                    "data": [{"role":"user","content":prompt}],
                    "max_token":1000,
                    "request_id":str(thread_local_data.get_request_id())
                    })
                app.logger.info(f"gpt 3.5 response format is {response.json()}")
                app.logger.info(f"gpt 3.5 response format type is {type(response.json())}")
                app.logger.info("finish in {}".format(time.time()-start))
                checker = 1
        else:
            try:
                # app.logger.info(f"the prompt we are sending is {prompt}")
                start=time.time()
                
                # response = requests.post(
                #     GPT_API,
                #     json={
                #     "model": "gpt-4",
                #     "data": [{"role":"user","content":prompt}],
                #     "max_token":1000,
                #     "request_id":str(thread_local_data.get_request_id())
                #     }
                # )
                # app.logger.info(f"gpt 4 response format is {response.json()}")
                # app.logger.info(f"gpt 4 response format type is {type(response.json())}")
                # app.logger.info("finish in {}".format(time.time()-start))
                response_from_groq = llm.invoke(prompt)
                # app.logger.info("groq response in streaming way")
                # app.logger.info("\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n")
                # for chunk in llm.stream(prompt):
                #     print(chunk.content, end="", flush=True)
                # app.logger.info("\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n")

                # app.logger.info(f" response from groq api {response}")
                # app.logger.info(f" response from groq api type {type(response)}")
                response = json.loads(response_from_groq.content)
                app.logger.info(f" response from groq api after {response}")
                app.logger.info(f" response from groq api after {type(response)}")
                app.logger.info("finish in groq {}".format(time.time()-start))
                checker = 0
            except Exception as e:
                app.logger.info(f"In except the exception is {e}")
                start=time.time()
            
                response = requests.post(
                    GPT_API,
                    json={
                    "model": "gpt-4",
                    "data": [{"role":"user","content":prompt}],
                    "max_token":1000,
                    "request_id":str(thread_local_data.get_request_id())
                    }
                )
                app.logger.info(f"gpt 4 response format is {response.json()}")
                app.logger.info(f"gpt 4 response format type is {type(response.json())}")
                app.logger.info("finish in {}".format(time.time()-start))
                checker = 1


        # response.raise_for_status()
        # app.logger.info(f"hellpppppppppppppppp-->{response.json()['text']}")
        if checker == 0:
            try:
                app.logger.info(f"full response that came from the gpt{response}")
                text = str(response)
                app.logger.info(f"text got from gpt {text}")
                try:
                    text = text.strip('`').replace('json\n','').strip()
                except:
                    pass
                intents = json.loads(text)
                app.logger.info(f"the intents are: {intents}")

                curr_intent = intents["action"]
                app.logger.info(f"curr_intent is: {curr_intent}")
                if self.previous_intent == curr_intent:
                    self.call_gpt4 = 1
                self.previous_intent = curr_intent
                thread_local_data.update_recognize_intents(intents["action"])
            except Exception as e:
                app.logger.info(f"Exception occur while intent calcualtion and calling exception {e}")
                # thread_local_data.update_recognize_intents("Final Answer")
            # time.sleep(10)

            end_time = time.time()
            elapsed_time = end_time - start_time
            app.logger.info(f"time taken for this call is {elapsed_time}")
            num_tokens = len(encoding.encode(str(response).replace('\n', ' ').replace('\t', '')))
            app.logger.info(f"current num_tokens: {num_tokens}")
            thread_local_data.update_res_token_count(num_tokens)
            end_result = str(response).replace('\n', ' ').replace('\t', '')
            app.logger.info(f"the end response is {end_result}")
            return response_from_groq.content.replace('\n', ' ').replace('\t', '')
        if checker == 1:
            try:
                text = str(response.json()["text"])
                try:
                    text = text.strip('`').replace('json\n','').strip()
                except:
                    pass
                intents = json.loads(text)
                
                curr_intent = intents["action"]
                if self.previous_intent == curr_intent:
                    self.call_gpt4 = 1
                self.previous_intent = curr_intent
                thread_local_data.update_recognize_intents(intents["action"])
            except Exception as e:
                app.logger.info(f"Exception occur while intent calcualtion and calling exception {e}")
                # thread_local_data.update_recognize_intents("Final Answer")
                # time.sleep(10)

            end_time = time.time()
            elapsed_time = end_time - start_time
            app.logger.info(f"time taken for this call is {elapsed_time}")
            num_tokens = len(encoding.encode(response.json()["text"].replace('\n', ' ').replace('\t', '')))
            thread_local_data.update_res_token_count(num_tokens)
            return response.json()["text"].replace('\n', ' ').replace('\t', '')

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {

        }




class CustomAgentExecutor(AgentExecutor):

    def prep_outputs(self, inputs: Dict[str, str],
                    outputs: Dict[str, str],
                    metadata: Optional[Dict[str, Any]] = None,
                    return_only_outputs: bool = False) -> Dict[str, str]:
        # pdb.set_trace()
        self._validate_outputs(outputs)
        req_id = thread_local_data.get_request_id()
        prom_id = thread_local_data.get_prompt_id()
        metadata = {'request_Id': req_id, 'prompt_id': prom_id}
        app.logger.info(f"before: memory object is not none and metadata is {metadata}, {return_only_outputs}")
        if self.memory is not None:
            app.logger.info(f"memory object is not none and metadata is {metadata}")
            self.memory.save_context(inputs, outputs, metadata)
        app.logger.info(f"After: memory object is not none and metadata is {metadata}")
        if return_only_outputs:
            return outputs
        else:
            return {**inputs, **outputs}

    def prep_inputs(self, inputs: Union[Dict[str, Any], Any]) -> Dict[str, str]:
        """Validate and prepare chain inputs, including adding inputs from memory.

        Args:
            inputs: Dictionary of raw inputs, or single input if chain expects
                only one param. Should contain all inputs specified in
                `Chain.input_keys` except for inputs that will be set by the chain's
                 memory.

        Returns:
            A dictionary of all inputs, including those added by the chain's memory.
        """
        if not isinstance(inputs, dict):


            _input_keys = set(self.input_keys)
            if self.memory is not None:
                # If there are multiple input keys, but some get set by memory so that
                # only one is not set, we can still figure out which key it is.
                _input_keys = _input_keys.difference(self.memory.memory_variables)
            if len(_input_keys) != 1:
                raise ValueError(
                    f"A single string input was passed in, but this chain expects "
                    f"multiple inputs ({_input_keys}). When a chain expects "
                    f"multiple inputs, please call it by passing in a dictionary, "
                    "eg `chain({'foo': 1, 'bar': 2})`"
                )
            inputs = {list(_input_keys)[0]: inputs}
        if self.memory is not None:


            external_context = self.memory.load_memory_variables(inputs)

            inputs = dict(inputs, **external_context)
            filtered_messages = []
            for msg in inputs['chat_history']:
                try:
                    if msg.additional_kwargs['metadata']['prompt_id'] == thread_local_data.get_prompt_id():
                    # If it does, append the message content to the filtered_messages list
                        filtered_messages.append(msg)
                except:
                    pass

            inputs['chat_history'] = filtered_messages[-8:]

            # time.sleep(4)
        self._validate_inputs(inputs)
        return inputs




#helper functions
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
    unwanted_actions=['Topic Cofirmation','Langchain','Assessment Ended','Casual Conversation', 'Topic confirmation', 'Topic not found', 'Topic Confirmation', 'Topic Listing', 'Probe', 'Question Answering', 'Fallback']
    action_url = f"{ACTION_API}?user_id={user_id}"

    payload = {}
    headers = {}

    response = requests.request(
        "GET", action_url, headers=headers, data=payload)

    if response.status_code == 200:


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
    else:
        post_dict= {'user_id':user_id, 'status': TaskStatus.ERROR.value,'task_name':TaskNames.GET_ACTION_USER_DETAILS.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':'Exception happend at get action api end'}
        try:
            client.publish('com.hertzai.longrunning.log', post_dict)
        except Exception as e:
            logging.error("Error while publish at com.hertzai.longrunning.log topic")

    url = STUDENT_API
    payload = json.dumps({
        "user_id": user_id
    })
    headers = {
        'Content-Type': 'application/json'
    }
    response = requests.request("POST", url, headers=headers, data=payload)
    if response.status_code == 200:
        user_data = response.json()

        user_details = f'''Below are the information about the user.
        user_name: {user_data["name"]} (Call the user by this name only when required and not always),gender: {user_data["gender"]}, who_pays_for_course: {user_data["who_pays_for_course"]}(Entity Responsible for Paying the Course Fees), preferred_language: {user_data["preferred_language"]}(User's Preferred Language), date_of_birth: {user_data["dob"]}, english_proficiency: {user_data["english_proficiency"]}(User's English Proficiency Level), created_date: {user_data["created_date"]}(user creation date), standard: {user_data["standard"]}(User's Standard in which user studying)
        '''
    else:
        post_dict= {'user_id':user_id, 'status': TaskStatus.ERROR.value,'task_name':TaskNames.GET_ACTION_USER_DETAILS.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':'Exception happend at get user detail api end'}
        try:
            client.publish('com.hertzai.longrunning.log', post_dict)
        except Exception as e:
            logging.error("Error while publish at com.hertzai.longrunning.log topic")
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

        try:
            messages = memory.chat_memory.search(prompt,metadata=metadata)
        except:
            post_dict= {'user_id':'', 'status': TaskStatus.ERROR.value,'task_name':TaskNames.GET_TIME_BASED_HISTORY.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':'Exception happend at zep api end memory object found none'}
            try:
                client.publish('com.hertzai.longrunning.log', post_dict)
            except Exception as e:
                logging.error("Error while publish at com.hertzai.longrunning.log topic")
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
        try:
            messages = memory.chat_memory.search(prompt)
        except:
            post_dict= {'user_id':'', 'status': TaskStatus.ERROR.value,'task_name':TaskNames.GET_TIME_BASED_HISTORY.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':'Exception happend at zep api end memory object found none'}
            try:
                client.publish('com.hertzai.longrunning.log', post_dict)
            except Exception as e:
                logging.error("Error while publish at com.hertzai.longrunning.log topic")

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
        app.logger.info(" The string to parse: {string}")
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
        post_dict= {'user_id':'', 'task_type':'async', 'status': TaskStatus.EXECUTING.value ,'task_name': TaskNames.ANIMATE_CHARACTER.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.ANIMATE_CHARACTER.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id()}
        try:
            client.publish('com.hertzai.longrunning.log', post_dict)
        except Exception as e:
            logging.error("Error while publish at com.hertzai.longrunning.log topic")
        prompt = string
        student_id_url = STUDENT_API

        payload = json.dumps({
        "user_id": thread_local_data.get_user_id()
        })
        headers = {
        'Content-Type': 'application/json'
        }

        response = requests.request("POST", student_id_url, headers=headers, data=payload)
        if response.status_code == 200:
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
            "weights_dir": f"/home/azureuser/dreambooth/diffusers/examples/dreambooth/{folder_name}_result",
            "prompt": prompt
        })
        headers = {
            'Content-Type': 'application/json'
        }

        response = requests.request("POST", inference_url, headers=headers, data=payload, timeout=180)
        if response.status_code == 200:
            return response.json()["image_url"]
        else:
            post_dict= {'user_id':thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value,'task_name':TaskNames.ANIMATE_CHARACTER.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.ANIMATE_CHARACTER.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':f'Exception happend at dreamooth api end for re {thread_local_data.get_request_id()}'}
            try:
                client.publish('com.hertzai.longrunning.log', post_dict)
            except Exception as e:
                logging.error("Error while publish at com.hertzai.longrunning.log topic")

    except:
        post_dict= {'user_id':thread_local_data.get_user_id(), 'status': TaskStatus.TIMEOUT.value,'task_name':TaskNames.ANIMATE_CHARACTER.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.ANIMATE_CHARACTER.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':f'Exception happend at dreamooth api end for req_id {thread_local_data.get_request_id()} timed out'}
        try:
            client.publish('com.hertzai.longrunning.log', post_dict)
        except Exception as e:
            logging.error("Error while publish at com.hertzai.longrunning.log topic")
        return "something went wrong"



def parse_text_to_image(inp):
    '''
        stable diffusion
    '''
    try:

        post_dict= {'user_id':'', 'task_type':'async', 'status': TaskStatus.EXECUTING.value ,'task_name': TaskNames.STABLE_DIFF.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.STABLE_DIFF.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id()}
        try:
            client.publish('com.hertzai.longrunning.log', post_dict)
        except Exception as e:
            logging.error("Error while publish at com.hertzai.longrunning.log topic")

        url = f'{STABLE_DIFF_API}?prompt={inp}'
        payload = {}

        headers = {}
        response = requests.request("POST", url, headers=headers, data=payload, timeout=240)
        if response.status_code == 200:
            return response.json()["img_url"]
        else:
            post_dict= {'user_id':thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value,'task_name':TaskNames.STABLE_DIFF.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.STABLE_DIFF.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':f'Exception happend at stable diff for req_id: {thread_local_data.get_request_id()}'}
            try:
                client.publish('com.hertzai.longrunning.log', post_dict)
            except Exception as e:
                logging.error("Error while publish at com.hertzai.longrunning.log topic")
    except Exception as e:
        post_dict= {'user_id':thread_local_data.get_user_id(), 'status': TaskStatus.TIMEOUT.value,'task_name':TaskNames.ANIMATE_CHARACTER.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.ANIMATE_CHARACTER.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':f'Exception happend at stable diff for req_id: {thread_local_data.get_request_id()} timed out'}
        try:
            client.publish('com.hertzai.longrunning.log', post_dict)
        except Exception as e:
            logging.error("Error while publish at com.hertzai.longrunning.log topic")
        return f"{e} Not able to generating image at this moment please try later"

def parse_image_to_text(inp):
    '''
        LlaVA implemetation
    '''

    try:
        post_dict= {'user_id':'', 'task_type':'async', 'status': TaskStatus.EXECUTING.value ,'task_name': TaskNames.LLAVA.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.LLAVA.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id()}
        try:
            client.publish('com.hertzai.longrunning.log', post_dict)
        except Exception as e:
            logging.error("Error while publish at com.hertzai.longrunning.log topic")
        inp_list = inp.split(',')
        url = f'{LLAVA_API}'
        payload = {
            'url': inp_list[0],
            'prompt': inp_list[1]
        }
        files=[]
        headers={}

        response = requests.request("POST", url, headers=headers, data=payload, files=files, timeout=300)
        if response.status_code == 200:
            return response.text
        else:
            post_dict= {'user_id':thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value,'task_name':TaskNames.LLAVA.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.LLAVA.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':f'Exception happend at LLAVA for req_id: {thread_local_data.get_request_id()}'}
            try:
                client.publish('com.hertzai.longrunning.log', post_dict)
            except Exception as e:
                logging.error("Error while publish at com.hertzai.longrunning.log topic")
    except Exception as e:
        post_dict= {'user_id':thread_local_data.get_user_id(), 'status': TaskStatus.TIMEOUT.value,'task_name':TaskNames.LLAVA.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.LLAVA.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':f'Exception happend at LLAVA for req_id: {thread_local_data.get_request_id()} timed out'}
        try:
            client.publish('com.hertzai.longrunning.log', post_dict)
        except Exception as e:
            logging.error("Error while publish at com.hertzai.longrunning.log topic")
        return f'{e} Not able to generating answer at this moment please try later'


async def call_crwalab_api(input_url, input_str_list, user_id , request_id):
    try:
        app.logger.info("enter in call_crawlab_api function")
        app.logger.info(f"the input url is {input_url} and input_str_list is {input_str_list}")
        payload = {
            'link': input_str_list,
            'user_id': user_id,
            'request_id': request_id,
            'depth':'1',

        }
        app.logger.info("in crawlab api")
        app.logger.info(f"this is crawlab payload: - {payload}")

        headers = {}
        async with aiohttp.ClientSession() as session:
            async with session.post(CRAWLAB_API, headers=headers, data=payload) as response:
                response_text = await response.text()
                app.logger.info(f" this is response text : - {response_text}")
                return response_text
    except Exception as e:
        app.logger.info(f"we are in except of call_crawlab_api the error is {e}")
        url = RAG_API
        app.logger.info("going to except in Rag api")
        payload = {'url': input_url}
        files=[]
        headers = {}

        response = requests.request("POST", url, headers=headers, data=payload, files=files)
        return f'your url got uploaded and data extraction is being processes. Here is some brief information about url you hava provided {response.text}'

def start_async_tasks(coroutine):
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(coroutine)
        finally:
            loop.close()
    Thread(target=run).start()

def parse_link_for_crwalab(inp):
    '''

        Use this function when user give url for any webpage or pdf

    '''
    inp_list = inp.split(',')
    app.logger.info(inp_list)
    input_url = inp_list[0]
    app.logger.info(input_url)
    link_type = inp_list[1].strip(' ')
    app.logger.info(link_type)
    app.logger.info("\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n")
    user_id= thread_local_data.get_user_id()
    request_id= thread_local_data.get_request_id()

    try:
        post_dict= {'user_id':'', 'task_type':'async', 'status': TaskStatus.EXECUTING.value ,'task_name': TaskNames.CRAWLAB.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.CRAWLAB.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id()}
        try:
            client.publish('com.hertzai.longrunning.log', post_dict)
        except Exception as e:
            logging.error("Error while publish at com.hertzai.longrunning.log topic")
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
            except:
                app.logger.info("Got exception in book parsing api {e}")
                post_dict= {'user_id':thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value,'task_name':TaskNames.CRAWLAB.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.CRAWLAB.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':f'Exception happend at CRWALAB for req_id: {thread_local_data.get_request_id()} for pdf upload'}
                try:
                    client.publish('com.hertzai.longrunning.log', post_dict)
                except:
                    logging.error("Error while publish at com.hertzai.longrunning.log topic")
                return "sorry I am not able to process your request at this moment"

        elif link_type == 'website':
            input_url_list = [input_url]
            app.logger.info(f"link type is {link_type}")
            input_str_list = repr(input_url_list)
            try:

                url = RAG_API

                payload = {'url': input_url}
                files=[]
                headers = {}

                response = requests.request("POST", url, headers=headers, data=payload, files=files)

                app.logger.info(response.text)
                app.logger.info(f"RAG_API response {response.text}")
                app.logger.info("completed rag")
                try:
                    app.logger.info("going for crawlab api")
                    start_async_tasks(call_crwalab_api(input_url, input_str_list, user_id, request_id))
                    app.logger.info("done for crawlab api")
                    return response.text
                except Exception as e:
                    app.logger.info(f"Got exception in crawlab api {e}")
                    post_dict= {'user_id':thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value,'task_name':TaskNames.CRAWLAB.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.CRAWLAB.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':f'Exception happend at CRWALAB for req_id: {thread_local_data.get_request_id()} for weblink upload'}
                    try:
                        client.publish('com.hertzai.longrunning.log', post_dict)
                    except Exception as e:
                        logging.error("Error while publish at com.hertzai.longrunning.log topic")
                    return f"sorry I am not able to process your request at this moment but here is some brief information about url you hava provided {response.text}"

                return f"your url got uploaded and data extraction is being processes. Here is some brief information about url you hava provided {response.text}"
            except Exception as e:
                app.logger.info(f"Got exception in crawlab api {e}")
                post_dict= {'user_id':thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value,'task_name':TaskNames.CRAWLAB.value, 'uid':thread_local_data.get_request_id(), 'task_id': f"{TaskNames.CRAWLAB.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason':f'Exception happend at CRWALAB for req_id: {thread_local_data.get_request_id()} for weblink upload'}
                try:
                    client.publish('com.hertzai.longrunning.log', post_dict)
                except Exception as e:
                    logging.error("Error while publish at com.hertzai.longrunning.log topic")
                return "sorry I am not able to process your request at this moment"

        else:
            return "Sorry I am unable to process your request with this url type"
    except Exception as e:
        pass


def parse_user_id(inp: str):
    url = 'https://azurekong.hertzai.com:8443/db/getstudent_by_user_id'


    headers = {
        'Content-Type': 'application/json'
    }

    try:
        prov_user_id = re.findall('\d',inp)[0]
    except:
        pass
    finally:
        prov_user_id = ""

    payload = json.dumps({
        "user_id": thread_local_data.get_user_id()
    })

    response = requests.request("POST", url, headers=headers, data=payload)
    if prov_user_id=="" or int(prov_user_id) != thread_local_data.get_user_id():

        return f"you might interested in finding your user detail here are details {response.text}"

    else:
        return response.text
from bs4 import BeautifulSoup

async def fetch(session, url):
    try:
        async with session.get(url) as response:
            start_time = time.time()
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            end_time = time.time()
            elapsed_time = end_time - start_time
            app.logger.info(f"time taken to crawl {url} is {elapsed_time}")
            return soup.get_text()
    except Exception as e:
        app.logger.error(f"An error occurred while fetching {url}: {e}")
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
        text =  asyncio.run(async_main(top_2_search_res_link))
        # Removing punctuation and extra characters
        app.logger.info(text)
        cleaned_text = re.sub(r'[^\w\s]', '', text[0]+" "+text[1])  # Remove punctuation
        cleaned_text = re.sub(r'\n+', '\n', cleaned_text).strip()  # Remove extra newlines and leading/trailing whitespaces
    except RuntimeError as e:
        app.logger.error(f"Runtime error occurred: {e}")

    final_res.append({'text': cleaned_text, 'source':top_2_search_res_link})
    app.logger.info(f"res:-->{final_res}")

    if len(final_res) == 0:
        return search.results(query, 4)

    return final_res



class CustomConvoOutputParser(AgentOutputParser):
    """Output parser for the conversational agent."""

    def get_format_instructions(self) -> str:
        return FORMAT_INSTRUCTIONS

    def parse(self, text: str) -> Union[AgentAction, AgentFinish]:
        try:
            response = parse_json_markdown(text)
            action, action_input = response["action"], response["action_input"]
            if action == "Final Answer" or action == "Final_Answer":
                return AgentFinish({"output": action_input}, text)
            else:
                return AgentAction(action, action_input, text)
        except Exception as e:
            # str = ""
            app.logger.info(text)
            app.logger.info(f"Caught Exception while parsing output {e}")
            pattern = r"final\s*[_]*answer"
            try:
                if re.search(pattern, text, re.IGNORECASE):
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
                else:
                    app.logger.info(text)
                    start_index = text.index('{')
                    try:
                        end_index = text.rindex('}') + 1
                    except:
                        text += '"}'
                        end_index = text.rindex('}') + 1
                    try:
                        json_string = text[start_index:end_index]
                        response = parse_json_markdown(json_string)
                        action, action_input = response["action"], response["action_input"]
                    except Exception as innerException:
                        app.logger.info("Caught inner Exception: {innerException}")
                        return AgentFinish({"output": text}, text)
                    return AgentAction(action, action_input, text)
                    # raise OutputParserException(f"Could not parse LLM output: {text}") from e
            except Exception as e:
                app.logger.info(f"Encounter an except {e} for ai msg")
                # Check for the 'AI:' pattern in the response
                ai_pattern = r"AI: (.+)"
                ai_match = re.search(ai_pattern, text, re.IGNORECASE)
                if ai_match:
                    final_answer = ai_match.group(1).strip()
                    return AgentFinish({"output": final_answer}, text)

    @property
    def _type(self) -> str:
        return "conversational_chat"




# main function
def get_ans(casual_conv, req_tool, user_id, query, custom_prompt):
    start_time = time.time()
    user_details, actions = get_action_user_details(user_id=user_id)
    app.logger.info("time taken by get_action_user_details %s seconds", time.time() - start_time)

    llm = CustomGPT()
    app.logger.info(f"query------> {query}")
    memory_start_time = time.time()
    memory=get_memory(user_id=user_id)
    app.logger.info("time taken by get_memory %s seconds", time.time() - memory_start_time)



    tools_start_time = time.time()
    tools = get_tools(req_tool=req_tool, is_first=True)
    app.logger.info("time taken by get_tools %s seconds", time.time() - tools_start_time)

    app.logger.info(f'tools {type(tools)}')






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
        Before you respond, consider the context in which you are utilized. {custom_prompt}
        You are designed to answer questions, provide revisions, conduct assessments, teach various topics, create personalised curriculum and assist with research for both students and working professionals.
        Your expertise draws from various knowledge sources like books, websites, and white papers. Your responses will be conveyed to the user through a video, using an avatar and text-to-speech technology, and can be translated into various languages.
        Consider the user's location, time and context of previous dialogues with time to create a proper prompt for tools and follow up in-context questions.
        <CONTEXT_END>
        These are all the actions that the user has performed up to now:
        <PREVIOUS_USER_ACTION_START>
        {actions}

        Conversation History:
        <HISTORY_START>
        """
    
    if not casual_conv:
        suffix = """
            <HISTORY_END>
            Only if this above conversation history is not sufficient to fulfill the user's request then use below FULL_HISTORY tool. Important: If results can be accomplished with above information skip tools section and move to format instructions.

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

            always create parsable output.

            Here is the User and AI conversation in reverse chronological order:

            USER'S INPUT:
            -------------
            <USER_INPUT_START>
            Latest USER'S INPUT For which you need to respond (consult recent history only when needed for more context): {{{{input}}}}
            <USER_INPUT_END>
            """
    else:
        suffix = """
            <HISTORY_END>
            Only if this above conversation history is not sufficient to fulfill the user's request then use below FULL_HISTORY tool. Important: If results can be accomplished with above information skip tools section and move to format instructions.

            TOOLS

            ------

            Assistant can use tools to look up information that may be helpful in answering the user's
            question. The tools you can use are:

            
            <FORMAT_INSTRUCTION_START>
            {format_instructions}
            <FORMAT_INSTRUCTION_END>

            always create parsable output.

            Here is the User and AI conversation in reverse chronological order:

            USER'S INPUT:
            -------------
            <USER_INPUT_START>
            Latest USER'S INPUT For which you need to respond (consult recent history only when needed for more context): {{{{input}}}}
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
        human_message=suffix,
        input_variables=["input", "agent_scratchpad", "chat_history"]
    )
    # prompt.input_variables


    #chat Agent
    llm_chain = LLMChain(
        llm=llm,
        prompt=prompt,
        # memory=memory
    )

    custom_parser = CustomConvoOutputParser()

    agent = ConversationalChatAgent(
        llm_chain=llm_chain,
        verbose=True,
        output_parser=custom_parser,
        template_tool_response=TEMPLATE_TOOL_RESPONSE
        )


    prom_id = thread_local_data.get_prompt_id()
    metadata = {"where": {
        "jsonpath": '$[*] ? (@.prompt_id == {})'.format(prom_id)}}
    agent_chain = CustomAgentExecutor(
        agent=agent,
        tools=tools,
        verbose=True,
        memory=memory,
        metadata=metadata
    )
    agent_chain_start_time = time.time()
    ans = agent_chain.run({'input':query})
    app.logger.info("time taken by chain agent run %s seconds", time.time() - agent_chain_start_time)
    end_time = time.time()
    elapse_time = end_time-start_time
    app.logger.info(f"total time taken by get_ans function %s seconds", elapse_time)
    return ans


Hevolve = "You are Hevolve, a highly intelligent educational AI developed by HertzAI."



@app.route('/chat', methods=['POST'])
def chat():
    # print("hii")

    start_time = time.time()
    data = request.get_json()
    user_id = data.get('user_id', None)
    request_id = data.get('request_id', None)
    req_tool = data.get('tools', None)
    prompt_id = data.get('prompt_id', None)
    casual_conv = data.get('casual_conv', None)
    app.logger.info(f"casual_conv type {casual_conv}")

    # return ""
    thread_local_data.set_request_id(request_id=request_id)



    if prompt_id:
        try:
            res = requests.get(
                            f'{DB_URL}/getprompt/?prompt_id={prompt_id}').json()
                        # use config for url
            custom_prompt = res[0]['prompt']
            if res[0]['prompt']=='Learn Language':
                app.logger.info('found Learn languague getting user preffered language')
                lang = requests.post('{}/getstudent_by_user_id'.format(DB_URL),
                        data=json.dumps({"user_id": user_id})).json()
                language = lang['preferred_language'][:2]
                app.logger.info(f'user preffered language is {language}')  
                custom_prompt = custom_prompt+f' The Language selected is {language}'
                app.logger.info(f"custom prompt is: {custom_prompt}")


        except:
            print(f'failed to get prompt from id:- {prompt_id}')
            custom_prompt = Hevolve
    else:
        custom_prompt = Hevolve  # use Hevolve from config/template
        prompt_id = 0
    app.logger.info(f'{custom_prompt}-->{prompt_id}')

    post_dict= {'user_id':user_id, 'status':'INITIALIZED','task_name':"CHAT", 'uid':request_id, 'task_id': f"CHAT_{str(request_id)}", 'request_id': request_id}
    try:
        client.publish('com.hertzai.longrunning.log', post_dict)
    except Exception as e:
        app.logger.error("Error while publish at com.hertzai.longrunning.log topic")

    thread_local_data.set_user_id(user_id=user_id)
    thread_local_data.set_req_token_count(value=0)
    thread_local_data.set_res_token_count(value=0)
    thread_local_data.set_recognize_intents()
    thread_local_data.set_global_intent(global_intent=req_tool)
    thread_local_data.set_prompt_id(prompt_id)

    prompt = data.get('prompt', None)
    app.logger.info("the time taken before get ans in main api is %s seconds", time.time() - start_time)
    ans_start_time = time.time()
    ans= get_ans(casual_conv,req_tool, user_id=user_id, query=prompt, custom_prompt=custom_prompt)
    app.logger.info("the time taken by get ans in main api is %s seconds", time.time() - ans_start_time)
    if ans != "":
        post_dict= {'user_id':user_id, 'status':'FINISHED','task_name':"CHAT", 'uid':request_id, 'task_id': f"CHAT_{str(request_id)}", 'request_id': request_id}
        try:
            client.publish('com.hertzai.longrunning.log', post_dict)
        except Exception as e:
            app.logger.error("Error while publish at com.hertzai.longrunning.log topic")
    else:
        post_dict= {'user_id':user_id, 'status':'ERROR','task_name':"CHAT", 'uid':request_id, 'task_id': f"CHAT_{str(request_id)}", 'request_id': request_id, 'failure_reason':'Got null response from GPT'}
        try:
            client.publish('com.hertzai.longrunning.log', post_dict)
        except Exception as e:
            app.logger.error("Error while publish at com.hertzai.longrunning.log topic")

    end_time = time.time()
    elapsed_time = end_time - start_time
    app.logger.info(f"time taken for this full call is {elapsed_time}")

    return jsonify({'response': ans, 'intent':thread_local_data.get_recognize_intents(), 'req_token_count': thread_local_data.get_req_token_count(), 'res_token_count':thread_local_data.get_res_token_count(), 'history_request_id': thread_local_data.get_reqid_list()})

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
            metadata={'prompt_id':0}
        )
        memory.chat_memory.add_message(
            AIMessage(content=ai_msg),
            metadata={'prompt_id':0}
        )
        return jsonify({'response':"Messages are saved!!!"}), 200
    else:
        return jsonify({'response':"Memory object not found"}), 400


@app.route('/status', methods=['GET'])
def status():
    return jsonify({'response':'Working...'})




if __name__ == '__main__':
    serve(app, host='0.0.0.0', port=5000)
