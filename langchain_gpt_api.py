# Fix Windows encoding for non-ASCII characters (Telugu, emojis, etc.)
import sys
import io
if sys.platform == 'win32':
    # Force UTF-8 encoding for stdout/stderr to prevent crashes with non-ASCII characters
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from bs4 import BeautifulSoup
from enum import Enum

# Use langchain-classic for pydantic v2 compatibility
from langchain.llms import OpenAI
from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
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
from langchain.chains import LLMMathChain
from langchain.chains.conversation.memory import ConversationSummaryMemory, ConversationBufferWindowMemory

# ChatOpenAI - use langchain
from langchain.chat_models import ChatOpenAI

# ChatGroq - optional import (version compatibility issues)
try:
    from langchain_groq import ChatGroq
except (ImportError, ModuleNotFoundError):
    ChatGroq = None

# LLM base class
from langchain.llms.base import LLM
from langchain.memory import ConversationBufferMemory, ReadOnlySharedMemory, ZepMemory
from langchain.schema import AgentAction, AgentFinish, OutputParserException, HumanMessage, AIMessage, SystemMessage
from langchain.tools import OpenAPISpec, APIOperation, StructuredTool
from langchain.utilities import GoogleSearchAPIWrapper
try:
    from langchain.requests import Requests
except (ImportError, AttributeError):
    # Requests might not be in langchain
    Requests = None
from flask import Flask, jsonify, request
import json
import os
import re
import logging
import requests
import pytz
from core.http_pool import pooled_get, pooled_post
from datetime import datetime, timezone
from typing import List, Union, Optional, Mapping, Any, Dict

# Conversational chat imports from langchain-classic
try:
    from langchain.agents.conversational_chat.output_parser import ConvoOutputParser
    from langchain.agents.conversational_chat.prompt import FORMAT_INSTRUCTIONS
    from langchain.output_parsers.json import parse_json_markdown
except (ImportError, AttributeError) as e:
    # These might not exist in langchain-classic
    ConvoOutputParser = None
    FORMAT_INSTRUCTIONS = None
    parse_json_markdown = None

# Tools imports - try langchain_community first
try:
    from langchain_community.tools import RequestsGetTool
except ImportError:
    try:
        from langchain.tools.requests.tool import RequestsGetTool
    except (ImportError, AttributeError):
        RequestsGetTool = None

try:
    from langchain_community.utilities import TextRequestsWrapper
except ImportError:
    try:
        from langchain.utilities.requests import TextRequestsWrapper
    except (ImportError, AttributeError):
        TextRequestsWrapper = None

import tiktoken
from pytz import timezone
from datetime import datetime, timedelta
from waitress import serve
from logging.handlers import RotatingFileHandler

# Pydantic v2 imports (we have pydantic 2.12.5)
try:
    from pydantic import BaseModel, Field, field_validator as root_validator
except ImportError:
    from pydantic import BaseModel, Field, root_validator
from threadlocal import thread_local_data
import crossbarhttp
from PIL import Image
import numpy as np
# Cohere rerank - make optional to avoid pydantic v2 incompatibility with old langchain
try:
    from langchain_community.retrievers.document_compressors import cohere_rerank
except (ImportError, ModuleNotFoundError):
    cohere_rerank = None  # Not available
import asyncio
import aiohttp
import redis
import pickle
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
try:
    import autogen
except ImportError:
    autogen = None  # Optional dependency
from typing import Dict, Tuple
load_dotenv()

#autogen requirements

try:
    from create_recipe import recipe, time_based_execution as time_execution, visual_execution
    from reuse_recipe import chat_agent, crossbar_multiagent, time_based_execution, visual_based_execution
except ImportError as e:
    print(f"Could not import recipe modules: {e}")
    recipe = None
    time_execution = None
    visual_execution = None
    chat_agent = None
    crossbar_multiagent = None
    time_based_execution = None
    visual_based_execution = None

try:
    from autobahn.asyncio.component import Component, run
except ImportError:
    Component = None
    run = None

import threading

try:
    from helper import retrieve_json
except ImportError:
    retrieve_json = None

# Google A2A integration (from gpt4.1)
try:
    from integrations.google_a2a import initialize_a2a_server, get_a2a_server, register_all_agents
except ImportError:
    initialize_a2a_server = None
    get_a2a_server = None
    register_all_agents = None
# os.environ['LANGCHAIN_TRACING_V2'] = 'true'
# os.environ['LANGCHAIN_ENDPOINT'] = 'https://api.smith.langchain.com'
# os.environ['LANGCHAIN_API_KEY'] = os.getenv("LANGCHAIN_API_KEY")
# os.environ["LANGCHAIN_PROJECT"] = os.getenv("LANGCHAIN_PROJECT")
groq_api_key = os.environ['GROQ_API_KEY']


# ============================================================================
# Custom Qwen3-VL LangChain Wrapper
# ============================================================================
class ChatQwen3VL(LLM):
    """
    Custom LangChain LLM wrapper for local Qwen3-VL API server.

    Compatible with LangChain's LLM interface while calling the local
    crawl4ai server at http://localhost:8000/v1/chat/completions.

    Features:
    - OpenAI-compatible API interface
    - Multimodal support (text + images)
    - Zero API costs (local server)
    - Drop-in replacement for ChatOpenAI
    """

    base_url: str = "http://localhost:8000/v1"
    model_name: str = "Qwen3-VL-4B-Instruct"
    temperature: float = 0.7
    max_tokens: int = 1500

    @property
    def _llm_type(self) -> str:
        return "qwen3-vl"

    def _call(self, prompt: str, stop: list = None) -> str:
        """
        Call the Qwen3-VL API with the given prompt.

        Args:
            prompt: The input text prompt
            stop: Optional stop sequences

        Returns:
            The generated response text
        """
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }

        if stop:
            payload["stop"] = stop

        try:
            response = pooled_post(
                f"{self.base_url}/chat/completions",
                json=payload,
                timeout=60
            )
            response.raise_for_status()

            data = response.json()
            return data["choices"][0]["message"]["content"]

        except Exception as e:
            app.logger.error(f"[Qwen3-VL] Error calling API: {e}")
            raise

    @property
    def _identifying_params(self) -> dict:
        """Return identifying parameters."""
        return {
            "model_name": self.model_name,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }


# Flag to switch between OpenAI and Qwen3-VL
USE_QWEN3VL = True  # Set to False to use OpenAI instead

def get_llm(model_name="gpt-3.5-turbo", temperature=0.7, max_tokens=1500):
    """
    Get LLM instance based on configuration.

    Returns ChatQwen3VL if USE_QWEN3VL is True, otherwise ChatOpenAI.
    """
    if USE_QWEN3VL:
        return ChatQwen3VL(
            model_name="Qwen3-VL-2B-Instruct",
            temperature=temperature,
            max_tokens=max_tokens
        )
    else:
        return ChatOpenAI(
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens
        )
# ============================================================================


class RequestLogRecord(logging.LogRecord):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Safely get the req_id from thread-local storage
        self.req_id = thread_local_data.get_request_id()


# logging info
# Use the custom log record factory
logging.setLogRecordFactory(RequestLogRecord)
logging.basicConfig(level=logging.INFO)
stream_handler = logging.StreamHandler(sys.stdout)
handler = RotatingFileHandler('langchain.log', maxBytes=100000, backupCount=0)

# Set the logging level for the file handler
handler.setLevel(logging.ERROR)

# Create a logging format
req_id = thread_local_data.get_request_id()
formatter = logging.Formatter(
    '%(asctime)s - %(name)s- [RequestID: %(req_id)s] - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

app = Flask(__name__)

app.logger.addHandler(stream_handler)
app.logger.addHandler(handler)
app.logger.propagate = False

# Security: Audit logging — redact API keys, JWTs, passwords from all logs
try:
    from security.audit_log import apply_sensitive_filter_to_all
    apply_sensitive_filter_to_all()
except Exception:
    pass  # Degrade gracefully — logs will still work, just unredacted

# Test logging
app.logger.info('Logger initialized')

# Security: Apply middleware (headers, CORS, CSRF, host validation, API auth)
try:
    from security.middleware import apply_security_middleware
    apply_security_middleware(app)
    app.logger.info("Security middleware applied")
except Exception as e:
    app.logger.warning(f"Security middleware not applied: {e}")

# ============================================================================
# HevolveSocial - Agent Social Network
# ============================================================================
try:
    from integrations.social import social_bp, init_social
    app.register_blueprint(social_bp)
    init_social(app)
    app.logger.info("HevolveSocial registered at /api/social")
except Exception as e:
    app.logger.warning(f"HevolveSocial init skipped (non-critical): {e}")

# ============================================================================
# Google A2A Protocol Initialization
# ============================================================================
# Initialize A2A server for cross-platform agent communication
try:
    app.logger.info("Initializing Google A2A Protocol server...")
    a2a_server = initialize_a2a_server(app, base_url="http://localhost:6777")
    app.logger.info("Google A2A Protocol server initialized successfully")

    # Register all agents with A2A
    register_all_agents()
    app.logger.info("All agents registered with Google A2A Protocol")
except Exception as e:
    app.logger.warning(f"Google A2A Protocol initialization error (non-critical): {e}")

# openAPI spec
try:
    spec = OpenAPISpec.from_file(
        "./openapi.yaml"
    )
except Exception as e:
    app.logger.warning(f"Could not load OpenAPI spec: {e}")
    spec = None


with open("config.json", 'r') as f:
    config = json.load(f)


# global variables
encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")

# api and keys
# app.logger.log(config['OPENAI_API_KEY'])
os.environ["OPENAI_API_KEY"] = config['OPENAI_API_KEY']
os.environ["GOOGLE_CSE_ID"] = config['GOOGLE_CSE_ID']
os.environ["GOOGLE_API_KEY"] = config['GOOGLE_API_KEY']
os.environ["NEWS_API_KEY"] = config['NEWS_API_KEY']
os.environ["SERPAPI_API_KEY"] = config['SERPAPI_API_KEY']
ZEP_API_URL = config['ZEP_API_URL']
ZEP_API_KEY = config['ZEP_API_KEY']
GPT_API = config['GPT_API']
STUDENT_API = config['STUDENT_API']
ACTION_API = config['ACTION_API']
FAV_TEACHER_API = config['FAV_TEACHER_API']
DREAMBOOTH_API = config['DREAMBOOTH_API']
STABLE_DIFF_API = config['STABLE_DIFF_API']
LLAVA_API = config['LLAVA_API']
BOOKPARSING_API = config['BOOKPARSING_API']
CRAWLAB_API = config['CRAWLAB_API']
RAG_API = config['RAG_API']
DB_URL = config['DB_URL']
# task scheduling and logging


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
try:
    search = GoogleSearchAPIWrapper(k=4)
except Exception as e:
    app.logger.warning(f"Could not initialize Google Search: {e}")
    search = None

# constants
# llm = ChatOpenAI(model_name="gpt-3.5-turbo-16k")
# llm = ChatOpenAI(temperature=0, model="gpt-4")
# llm = CustomGPT()
# The above code is creating an instance of the `LLMMathChain` class with an `open_ai_llm` attribute
# initialized with a `ChatOpenAI` object using the model name "gpt-3.5-turbo".

# llm_math = LLMMathChain(ChatOpenAI(model_name="gpt-3.5-turbo"))
# Old: llm_math = LLMMathChain(llm=ChatOpenAI(model_name="gpt-3.5-turbo"))
# New: Using get_llm() to automatically use Qwen3-VL or OpenAI based on USE_QWEN3VL flag
llm_math = LLMMathChain(llm=get_llm(model_name="gpt-3.5-turbo"))
# llm_math = LLMMathChain(llm= ChatGroq(groq_api_key=groq_api_key,
#                model_name = "mixtral-8x7b-32768"))

# llm = ChatGroq(groq_api_key=groq_api_key,
#                model_name="llama-3.1-8b-instant", temperature=0.3)

# app.logger.info(llm.invoke("hi how are you?"))

if spec is not None:
    try:
        chain = get_openapi_chain(spec)
    except Exception as e:
        app.logger.warning(f"Could not create OpenAPI chain: {e}")
        chain = None
else:
    chain = None


client = crossbarhttp.Client('http://aws_rasa.hertzai.com:8088/publish')

# Create thread pool executor for async Crossbar publishing
crossbar_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='crossbar_publish')

def publish_async(topic, message, timeout=2.0):
    """
    Publish to Crossbar in a background thread without blocking the main request.

    Args:
        topic: Crossbar topic to publish to
        message: Message payload (dict)
        timeout: Maximum time to wait for publish (default: 2.0 seconds)
    """
    def _publish():
        import socket
        try:
            # Set socket timeout to prevent long waits
            original_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(timeout)

            client.publish(topic, message)
            app.logger.debug(f"Successfully published to {topic}")
        except Exception as e:
            app.logger.error(f"Error publishing to {topic}: {e}")
        finally:
            # Restore original timeout
            if original_timeout is not None:
                socket.setdefaulttimeout(original_timeout)

    # Submit to executor without waiting for result
    crossbar_executor.submit(_publish)


# create prompt
def create_prompt(tools):
    user_details, actions = get_action_user_details(
        user_id=thread_local_data.get_user_id())
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
        Consider the user's location, time and context of previous dialogues with time to create a proper prompt for tools and follow up in-context questions.You have ability to see using Visual_Context_Camera tool.
        If your response contains abbreviated words, please separate them with spaces, like T T S.
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
        ]

        # Only add OpenAPI tool if chain is initialized
        if chain is not None:
            tool.append(Tool(
                name="OpenAPI_Specification",
                func=chain.run,
                description="Use this feature only when the user's request specifically pertains to one of the following scenarios:\
                Image Creation: When a request involves generating an image using text, this feature should be engaged. The entire text prompt must be used as it is unless otherwise requested to enhance further detail of prompt for the image generation process. If additional enahancement is needed , enrich the prompt to image generation with greater detail for learning.\
                Student Information: If a request is made for information regarding students, this functionality should be utilized to retrieve the necessary details.\
                Query Available Books: When the user is inquiring about available books, this feature should be used to locate and provide information about the required texts.\
                Any CRUD operation which is not a READ or anything related to curriculum should not use this tool,  It is vital to ensure that the intent precisely falls within one of the above  categories before engaging this functionality.\
                Don't use this to create a custom curriculum for user",
            ))

        tool += [
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
                description='''Use this tool exclusively for animating the selected character or teacher as requested by the user. The user should specify their animation request in a query, such as 'Show me in a spacesuit' or 'Animate yourself as a cartoon standing in front of the Taj Mahal.' This tool handles requests involving animating a pre-selected character and should not be used for general image generation tasks. For example, use it for 'Show me a picture of yourself dancing in the rain' but not for 'Generate an image of a sunset.' input'''
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
            ),
            Tool(
                name="Visual_Context_Camera",
                func=parse_visual_context,
                description="To see user or if there is a need to look at user camera feed for vision and understanding scene, visual question answering, seeing user, recognise visual objects and activity then this should be utilised. Input to this tool function should be the user query/input. Only if last 16 seconds Visual Context information is present & is enough, then use that to craft a better creative, better, cohesive, correlated , summarised natural response, format this tool response togather with Previous 15 minutes Visual Context information if you are seeing the scene via videocall from the other end. If there are more than 1 person try to give an identity to each across frames to track the subjects through time by framing the tool input accordingly."
            )

        ]
        tools += tool
        return tools

    else:
        tools_dict = {1: 'google_search', 2: 'Calculator', 3: 'OpenAPI_Specification', 4: 'FULL_HISTORY', 5: 'Text to image',
                      6: 'Image_Inference_Tool', 7: 'Data_Extraction_From_URL', 8: 'User_details_tool', 9: 'Visual_Context_Camera'}
        tool_desc = {
            'google_search': '''Search Google for recent results and retrieve URLs that are suitable for web crawling. Ensure that the search responses include the source URL from which the data was extracted. Always present this URL in the response as an HTML anchor tag. This approach ensures clear attribution and easy navigation to the original source for each piece of extracted information. Give urls for the source''',
            'Calculator': '''Useful for when you need to answer questions about math.''',
            'OpenAPI_Specification': '''Use this feature only when the user's request specifically pertains to one of the following scenarios:\
                Image Creation: When a request involves generating an image using text, this feature should be engaged. The entire text prompt must be used as it is unless otherwise requested to enhance further detail of prompt for the image generation process. If additional enahancement is needed , enrich the prompt to image generation with greater detail for learning.\
                Student Information: If a request is made for information regarding students, this functionality should be utilized to retrieve the necessary details.\
                Query Available Books: When the user is inquiring about available books, this feature should be used to locate and provide information about the required texts.\
                Any CRUD operation which is not a READ or anything related to curriculum should not use this tool,  It is vital to ensure that the intent precisely falls within one of the above  categories before engaging this functionality.\
                Don't use this to create a custom curriculum for user''',
            'FULL_HISTORY': '''Utilize this tool exclusively when the information required predates the current day & pertains to the ongoing user query or when there is a need to recall certain things we spoke earlier. The necessary input for this tool comprises a list of values separated by commas.
                The list should encompass a user-generated query, designated by user input text, a commencement date denoted as start_date, and an end date labeled as end_date. The start_date denotes the initiation date for the user information search and should consistently adhere to the ISO 8601 format. Meanwhile, the end_date, also conforming to the ISO 8601 format, signifies the conclusion date for the search.
                In cases where the end_date is indeterminable, the current datetime should be employed. For example, if the objective is to retrieve a user's dialogue spanning from the preceding day up to the present day (assuming today's date is 2023-07-13T10:19:56.732291Z), the input would resemble: 'what zep can do, 2023-07-12T10:19:56.00000Z, 2023-07-13T10:19:56.732291Z'. If query has any form of date or time by user, then start end datetime can be exact rather than till today for more accurate results. Remove any references to time based words (e.g. yesterday, today, datetimes, last year) since the date range you provide already accounts for that. e.g. if user has asked what did we discuss the day before yesterday then the text argument should just be empty since it does not have any named entity for fuzzy search followed by start and end datetime.
                Strive to apply this tool judiciously for scenarios in which retrospective user information is imperative. If Full history tool response is present, forget other histories, the inputs should be meticulously arranged to facilitate the extraction of accurate and pertinent data within the specified timeframe. Never use this tool for what is the response to my last comment?
                Remember whatever user query is regarding search history understand what user is asking about and rephrase it properly then send to tool. Before framing the final tool response from this tool consult corresponding created_at date time to give more accurate response''',
            'Text to image': '''Based on user query generate visual representation of text. Extract prompt from user query and use it as input for function''',
            'Image_Inference_Tool': '''When a user provides a query containing an image download URL and a related question about that image, utilize this tool for support. Your objective is to extract both the image URL and the user's inquiry or prompt pertaining to that image from their query, and then convert these elements into comma seperated string. The format should be as follows: "image_url, user_query".''',
            'Data_Extraction_From_URL': '''Your task is to extract a URL and its type (either 'pdf' or 'website') from a user's query. Upon receiving a query that contains a URL and a specified URL type, you are to use a tool designed for this purpose. The objective is to accurately identify both the URL and its type from the query. Once identified, these elements should be formatted into a comma-separated string, adhering to the format: "url, url_type".''',
            'User_details_tool': '''If a request is made for information regarding students or users, this functionality should be utilized to retrieve the necessary details. input for this api should Always be current user_id. Except current user id you should say you cannot have access other user's details.''',
            'Visual_Context_Camera': '''This tool captures the user's visual context during a video call, providing real-time captions. Use it for visual question answering, scene understanding, recognizing objects, activities, & monitoring the user. Input will be the user's input/query. If the last 16 seconds of visual context are available and sufficient, it crafts a creative, cohesive response. If not, inform the user of the glitch accessing the current camera feed and guess using the Last_5_Minutes_Visual_Context. Ensure responses are natural, avoiding lists of captions, and format them as if you are seeing the user scene via video call. Analyze the current tool response and previous visual context captions to recognize user activities and infer actions from multiple frames. If the user requests continuous narration without active input, adapt the response to include past, present, and future tenses for dynamic and contextually aware commentary.'''
        }
        tools_func = {
            'google_search': top5_results,
            'Calculator': llm_math.run,
            'FULL_HISTORY': parsing_string,
            'Text to image': parse_text_to_image,
            'Image_Inference_Tool': parse_image_to_text,
            'Data_Extraction_From_URL': parse_link_for_crwalab,
            'User_details_tool': parse_user_id,
            'Visual_Context_Camera': parse_visual_context
        }

        # Only add OpenAPI_Specification to tools_func if chain is initialized
        if chain is not None:
            tools_func['OpenAPI_Specification'] = chain.run
        if req_tool == "google_search":
            req_tool = "Google Search Snippets"
        if req_tool is not None and req_tool in tools_dict.values():
            tool_description = tool_desc[req_tool]
            tool_func = tools_func[req_tool]
            req_tool_from_user = [
                Tool(
                    name=req_tool,
                    func=tool_func,
                    description=tool_description
                )
            ]
            tools = load_tools(["google-search"])
            tools += req_tool_from_user

        else:
            tool_description = ""
            tool_func = ""
            tools = load_tools(["google-search"])
            # tools += req_tool_from_user

        tool = [

            Tool(
                name='Calculator',
                func=llm_math.run,
                description='Useful for when you need to answer questions about math.'
            ),
        ]

        # Only add OpenAPI tool if chain is initialized
        if chain is not None:
            tool.append(Tool(
                name="OpenAPI_Specification",
                func=chain.run,
                description="Use the specialized feature for image generation, student information retrieval, and querying available books, while avoiding its use for non-READ CRUD operations or custom curriculum creation.",
            ))

        tool += [
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
            Tool(
                name="Animate_Character",
                func=parse_character_animation,
                description='''Use this tool exclusively for animating the selected AI character or teacher as requested by the user; it is not intended for general requests or for animating random images or individuals other than AI teacher avatars. The user should specify their animation request in a query, e.g. 'Show me yourself in a spacesuit' or 'Animate yourself as a person riding a bike.' Once the request is made, the tool will generate the animation and return an URL link to the user that directs them to the animated image. This tool should not be used for general image generation tasks that don't pertain to animating the user's chosen character or teacher. For example, if a user queries 'Show me dancing in the rain,' and they have previously selected a specific character or teacher, the tool should be used to generate this animated scenario. However, if the user's request is something like 'Generate an image of a sunset,' which does not directly involve animating the selected character or teacher, then this tool should not be used.'''
            ),
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
            ),
            Tool(
                name="Visual_Context_Camera",
                func=parse_visual_context,
                description="This tool captures the user's visual context during a video call, providing real-time captions. Use it for visual question answering, scene understanding, recognizing objects, activities, & monitoring the user. Input will be the user's input/query. If the last 16 seconds of visual context are available and sufficient, it crafts a creative, cohesive response. If not, inform the user of the glitch accessing the current camera feed and guess using the Last_5_Minutes_Visual_Context. Ensure responses are natural, avoiding lists of captions, and format them as if you are seeing the user scene via video call. Analyze the current tool response and previous visual context captions to recognize user activities and infer actions from multiple frames. If the user requests continuous narration without active input, adapt the response to include past, present, and future tenses for dynamic and contextually aware commentary."
            )

        ]
        final_tool = []
        for new_tool in tool:
            if new_tool not in tools:
                final_tool.append(new_tool)

        tools += final_tool

        tool_strings = "\n".join(
            f"\n> {tool.name}: {tool.description}" for tool in tools)
        return tool_strings

# custom GPT


SUPPORTED_LANG_DICT = {
    "ar": "Arabic",
    "bg": "Bulgarian",
    "zh": "Chinese",
    "zh-cn": "Chinese (Simplified)",
    "nl": "Dutch",
    "fi": "Finnish",
    "fr": "French",
    "de": "German",
    "el": "Greek",
    "he": "Hebrew",
    "hu": "Hungarian",
    "is": "Icelandic",
    "id": "Indonesian",
    "ko": "Korean",
    "lv": "Latvian",
    "ms": "Malay",
    "fa": "Persian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "es": "Spanish",
    "sw": "Swahili",
    "sv": "Swedish",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "vi": "Vietnamese",
    "cy": "Welsh",
    "hi": "Hindi",
    "bn": "Bengali",
    "ta": "Tamil",
    "pa": "Punjabi",
    "gu": "Gujarati",
    "kn": "Kannada",
    "te": "Telugu",
    "mr": "Marathi",
    "ml": "Malayalam",
    "en": "English"
}


class CustomGPT(LLM):
    casual_conv: bool

    count: int = 0
    previous_intent: Optional[str] = None
    call_gpt4: Optional[int] = 0
    total_tokens: int = 0

    @property
    def _llm_type(self) -> str:
        return "custom"

    def _call(self, prompt: str, stop: Optional[List[str]] = None) -> str:
        start_time = time.time()
        self.count += 1
        # self.total_tokens = 0
        app.logger.info(f'calling for {self.count} times')

        app.logger.info(f"len---->{len(prompt.split(' '))}")
        # encoding = tiktoken.get_encoding("gpt-3.5-turbo")
        num_tokens = len(encoding.encode(prompt))
        thread_local_data.update_req_token_count(num_tokens)
        app.logger.info(f"len---->{num_tokens}")

        app.logger.info(f"first time calling {len(prompt)}")

        if self.count > 1 and thread_local_data.get_global_intent() != self.previous_intent:
            tools = get_tools(thread_local_data.get_global_intent())
            start_index = prompt.find("<TOOLS_START>")
            end_index = prompt.find("<TOOLS_END>") + len("<TOOLS_END>")
            prompt = prompt[:start_index] + tools + prompt[end_index:]
            app.logger.info(f"second time calling {len(prompt)}")

            # prompt = create_prompt(tools)
            app.logger.info(prompt)
            # time.sleep(10)

        checker = None
        # structured_llm = llm.with_structured_output(
        #     method="json_mode",
        #     include_raw=True
        # )
        if (self.count > 1 or self.call_gpt4 == 1):

            # try:
            #     # app.logger.info(f"the prompt we are sending is {prompt}")

            #     start = time.time()
            #     response = pooled_post(
            #         GPT_API,
            #         json={

            #             "model": "gpt-4.1-mini",
            #             "data": [{"role": "user", "content": prompt}],
            #             "max_token": 2000,
            #             "request_id": str(thread_local_data.get_request_id())
            #         })
            #     app.logger.info(
            #         f"gpt 3.5 response format is {response.json()}")
            #     app.logger.info(
            #         f"gpt 3.5 response format type is {type(response.json())}")
            #     app.logger.info(
            #         " gpt 3.5 finish in {}".format(time.time()-start))
            #     checker = 1

            # except:
            #     app.logger.info("gpt 3.5 fails on line number 483!!")
            try:
                if self.casual_conv:
                    app.logger.info(f"casual conv!")
                    start = time.time()
                    response = pooled_post(
                        GPT_API,
                        json={
                            "model": "llama",
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 200,  # Reduced from 1000 for faster responses
                            "temperature": 0.7
                        })
                    app.logger.info(
                        f"gpt 3.5 response format is {response.json()}")
                    app.logger.info(
                        f"gpt 3.5 response format type is {type(response.json())}")
                    app.logger.info(
                        " gpt 3.5 finish in {}".format(time.time()-start))
                    checker = 1

                else:
                    app.logger.info("Non casual conv")
                    start = time.time()
                    response = pooled_post(
                        GPT_API,
                        json={
                            "model": "llama",
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 200,  # Reduced from 1000 for faster responses
                            "temperature": 0.7
                        })
                    app.logger.info(
                        f"gpt 3.5 response format is {response.json()}")
                    app.logger.info(
                        f"gpt 3.5 response format type is {type(response.json())}")
                    app.logger.info(
                        " gpt 3.5 finish in {}".format(time.time()-start))
                    checker = 1

                    # # `response_from_groq`.

                    # response_from_groq = structured_llm.invoke(prompt)
                    # # app.logger.info("groq response in streaming way")
                    # # app.logger.info("\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n")
                    # # response_from_groq = ""
                    # # for chunk in llm.stream(prompt):
                    # #     app.logger.info(f"chunk in stream {chunk}")
                    # #     app.logger.info(f"chunk content in straming way {chunk.content}")
                    # #     response_from_groq +=chunk.content

                    # # app.logger.info("\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n")
                    # # app.logger.info(f" response from groq api {response_from_groq}")
                    # # app.logger.info(f" response from groq api {type(response_from_groq)}")

                    # app.logger.info(
                    #     "finish in groq {}".format(time.time()-start))
                    # response = response_from_groq['raw'].content
                    # response_from_groq = response_from_groq['raw'].content
                    # # response = json.loads(response_from_groq.content)
                    # # response = json.dumps(response)
                    # app.logger.info(
                    #     f" response from groq api after {response}")
                    # app.logger.info(
                    #     f" response from groq api after {type(response)}")
                    # checker = 0
            except Exception as e:
                app.logger.info(f"In except the exception is {e}")
                start = time.time()
                response = pooled_post(
                    GPT_API,
                    json={
                        "model": "llama",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1000
                    })
                app.logger.info(
                    f"gpt 3.5 response format is {response.json()}")
                app.logger.info(
                    f"gpt 3.5 response format type is {type(response.json())}")
                app.logger.info("finish in {}".format(time.time()-start))
                checker = 1
        else:
            try:
                # app.logger.info(f"the prompt we are sending is {prompt}")
                if self.casual_conv:

                    app.logger.info(
                        f"the casual conv line 519 casual conv {self.casual_conv} type of casual conv {type(self.casual_conv)}")
                    start = time.time()

                    response = pooled_post(
                        GPT_API,
                        json={
                            "model": "llama",
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 1000
                        }
                    )
                    app.logger.info(
                        f"gpt 3.5 response format is {response.json()}")
                    app.logger.info(
                        f"gpt 3.5 response format type is {type(response.json())}")
                    app.logger.info(
                        "gpt 3.5 finish in {}".format(time.time()-start))
                    checker = 1
                else:
                    try:
                        app.logger.info("non casual conv")
                        start = time.time()
                        response = pooled_post(
                            GPT_API,
                            json={
                                "model": "llama",
                                "messages": [{"role": "user", "content": prompt}],
                                "max_tokens": 1000
                            }
                        )
                        app.logger.info(
                            f"gpt 3.5 response format is {response.json()}")
                        app.logger.info(
                            f"gpt 3.5 response format type is {type(response.json())}")
                        app.logger.info(
                            "gpt 3.5 finish in {}".format(time.time()-start))
                        checker = 1

                        # response_from_groq = structured_llm.invoke(prompt)


                        # app.logger.info(
                        #     "finish in groq {}".format(time.time()-start))
                        # app.logger.info(
                        #     f"this is response from groq {response_from_groq}")
                        # response = response_from_groq['raw'].content
                        # response_from_groq = response_from_groq['raw'].content
                        # app.logger.info(
                        #     f" response from groq api after {response}")
                        # app.logger.info(
                        #     f" response from groq api after {type(response)}")
                        # checker = 0
                    except Exception as e:
                        app.logger.info(f" the error is {e}")
            except Exception as e:
                app.logger.info(f"In except the exception is {e}")
                start = time.time()

                response = pooled_post(
                    GPT_API,
                    json={
                        "model": "llama",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1000
                    }
                )
                app.logger.info(f"gpt 4 response format is {response.json()}")
                app.logger.info(
                    f"gpt 4 response format type is {type(response.json())}")
                app.logger.info("finish in {}".format(time.time()-start))
                checker = 1

        if checker == 0:
            try:
                app.logger.info(
                    f"full response that came from the gpt{response}")
                text = str(response)
                app.logger.info(f"text got from gpt {text}")
                try:
                    text = text.strip('`').replace('json\n', '').strip()
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
                app.logger.info(
                    f"Exception occur while intent calcualtion and calling exception {e}")
                # thread_local_data.update_recognize_intents("Final Answer")
            # time.sleep(10)

            end_time = time.time()
            elapsed_time = end_time - start_time
            app.logger.info(f"time taken for this call is {elapsed_time}")
            num_tokens = len(encoding.encode(
                str(response).replace('\n', ' ').replace('\t', '')))
            app.logger.info(f"current num_tokens: {num_tokens}")
            thread_local_data.update_res_token_count(num_tokens)
            end_result = str(response).replace('\n', ' ').replace('\t', '')
            app.logger.info(f"the end response is {end_result}")

            return 'response_from_groq'.replace('\n', ' ').replace('\t', '')
            # return response_from_groq.content.replace('\n', ' ').replace('\t', '')
        if checker == 1:
            try:
                # Extract text from OpenAI-compatible response format
                text = str(response.json()["choices"][0]["message"]["content"])
                try:
                    text = text.strip('`').replace('json\n', '').strip()
                except:
                    pass
                intents = json.loads(text)

                curr_intent = intents["action"]
                if self.previous_intent == curr_intent:
                    self.call_gpt4 = 1
                self.previous_intent = curr_intent
                thread_local_data.update_recognize_intents(intents["action"])
            except Exception as e:
                app.logger.info(
                    f"Exception occur while intent calcualtion and calling exception {e}")
                # thread_local_data.update_recognize_intents("Final Answer")
                # time.sleep(10)

            end_time = time.time()
            elapsed_time = end_time - start_time
            app.logger.info(f"time taken for this call is {elapsed_time}")
            response_text = response.json()["choices"][0]["message"]["content"]
            num_tokens = len(encoding.encode(
                response_text.replace('\n', ' ').replace('\t', '')))
            thread_local_data.update_res_token_count(num_tokens)
            return response_text.replace('\n', ' ').replace('\t', '')

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {

        }


# llm = CustomGPT(casual_conv=True)

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
        app.logger.info(
            f"before: memory object is not none and metadata is {metadata}, {return_only_outputs}")
        if self.memory is not None:
            app.logger.info(
                f"memory object is not none and metadata is {metadata}")
            try:
                self.memory.save_context(inputs, outputs, metadata)
                app.logger.info(
                    f"After: memory saved successfully with metadata {metadata}")
            except Exception as e:
                app.logger.error(f"Failed to save memory (Zep server may be down): {e}")
                # Continue without crashing - memory save is not critical
        else:
            app.logger.info(
                f"Memory object is None, skipping save")
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
                _input_keys = _input_keys.difference(
                    self.memory.memory_variables)
            if len(_input_keys) != 1:
                raise ValueError(
                    f"A single string input was passed in, but this chain expects "
                    f"multiple inputs ({_input_keys}). When a chain expects "
                    f"multiple inputs, please call it by passing in a dictionary, "
                    "eg `chain({'foo': 1, 'bar': 2})`"
                )
            inputs = {list(_input_keys)[0]: inputs}
        if self.memory is not None:
            try:
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
            except Exception as e:
                # Handle empty Zep session or API errors gracefully
                app.logger.warning(f"Could not load memory from Zep (likely empty session): {e}")
                inputs['chat_history'] = []

            # time.sleep(4)
        self._validate_inputs(inputs)
        return inputs


# helper functions
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


def get_action_user_details(user_id):
    '''
        This function help to extract action that user have perfomed till time
    '''
    # Initialize default values
    user_details = "No user details available."
    actions = "user has not performed any actions yet."

    unwanted_actions = ['Topic Cofirmation', 'Langchain', 'Assessment Ended', 'Casual Conversation', 'Topic confirmation',
                        'Topic not found', 'Topic Confirmation', 'Topic Listing', 'Probe', 'Question Answering', 'Fallback']
    action_url = f"{ACTION_API}?user_id={user_id}"

    # Todo: get, and populate timezone from client
    time_zone = "Asia/Kolkata"

    india_tz = pytz.timezone(time_zone)

    payload = {}
    headers = {}

    try:
        response = requests.request(
            "GET", action_url, headers=headers, data=payload, timeout=5.0)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        app.logger.error(f"Failed to get actions from {action_url}: {e}")
        post_dict = {'user_id': user_id, 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.GET_ACTION_USER_DETAILS.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Connection timeout/error at get action api: {e}'}
        publish_async('com.hertzai.longrunning.log', post_dict)
        # Continue with defaults instead of crashing
        response = None

    if response and response.status_code == 200:

        data = response.json()

        # Filter out unwanted actions
        filtered_data = [obj for obj in data if obj["action"]
                         not in unwanted_actions and obj["zeroshot_label"]
                         not in ['Video Reasoning']]

        filtered_data_video = [
            obj for obj in data if obj["zeroshot_label"] == 'Video Reasoning']
        # Dictionary to store the first and last occurrence dates for each action
        action_occurrences = {}

        # Iterate over the filtered data
        for obj in filtered_data:
            action = obj["action"]
            date = parse_date(obj["created_date"])
            gpt3_label = obj["gpt3_label"]

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
            first_action_text = f"{action} on {first_date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}"
            action_texts.append(first_action_text)
            if first_date != last_date:
                last_action_text = f"{action} on {last_date.astimezone(india_tz).strftime('%Y-%m-%dT%H:%M:%S')}"
                action_texts.append(last_action_text)

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
            action_texts.append('<Last_5_Minutes_Visual_Context_Start>')
            action_texts.extend(video_context_texts)
            action_texts.append('<Last_5_Minutes_Visual_Context_End>')
            action_texts.append(
                'If a person is identified in Visual_Context section that\'s most probably the user (me) & most likely not taking any selfie.')

        if len(action_texts) == 0:
            action_texts = ['user has not performed any actions yet.']

        actions = ", ".join(action_texts)
        # Get the current time

        # Format the time in the desired format
        formatted_time = datetime.now(pytz.utc).astimezone(
            india_tz).strftime('%Y-%m-%d %H:%M:%S')

        actions = actions + ". List of actions ends. <PREVIOUS_USER_ACTION_END> \n " + "Today's datetime in "+time_zone + "is: " + formatted_time + \
            " in this format:'%Y-%m-%dT%H:%M:%S' \n Whenever user is asking about current date or current time at particular location then use this datetime format by asking what user's location is. Use the previous sentence datetime info to answer current time based questions coupled with google_search for current time or full_history for historical conversation based answers. Take a deep breath and think step by step.\n"
        # user detail api
    else:
        post_dict = {'user_id': user_id, 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.GET_ACTION_USER_DETAILS.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': 'Exception happend at get action api end'}
        publish_async('com.hertzai.longrunning.log', post_dict)

    url = STUDENT_API
    payload = json.dumps({
        "user_id": user_id
    })
    headers = {
        'Content-Type': 'application/json'
    }

    try:
        response = requests.request("POST", url, headers=headers, data=payload, timeout=5.0)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        app.logger.error(f"Failed to get user details from {url}: {e}")
        post_dict = {'user_id': user_id, 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.GET_ACTION_USER_DETAILS.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Connection timeout/error at get user detail api: {e}'}
        publish_async('com.hertzai.longrunning.log', post_dict)
        return user_details, actions  # Return defaults

    if response.status_code == 200:
        user_data = response.json()

        user_details = f'''Below are the information about the user.
        user_name: {user_data["name"]} (Call the user by this name only when required and not always),gender: {user_data["gender"]}, who_pays_for_course: {user_data["who_pays_for_course"]}(Entity Responsible for Paying the Course Fees), preferred_language: {user_data["preferred_language"]}(User's Preferred Language), date_of_birth: {user_data["dob"]}, english_proficiency: {user_data["english_proficiency"]}(User's English Proficiency Level), created_date: {user_data["created_date"]}(user creation date), standard: {user_data["standard"]}(User's Standard in which user studying)
        '''
    else:
        post_dict = {'user_id': user_id, 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.GET_ACTION_USER_DETAILS.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': 'Exception happend at get user detail api end'}
        publish_async('com.hertzai.longrunning.log', post_dict)
    return user_details, actions


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
    messages = []  # Initialize to prevent UnboundLocalError

    memory = ZepMemory(
        session_id=session_id,
        url=ZEP_API_URL,
        api_key=ZEP_API_KEY,
        memory_key="chat_history",
    )

    try:

        metadata = {
            "start_date": start_date,
            "end_date":  end_date
        }

        try:
            messages = memory.chat_memory.search(prompt, metadata=metadata)
            app.logger.info(f'GOT THE messages from search {messages}')
        except Exception as e:
            app.logger.error(
                    f"Error while data search in zep response: {e}")
            post_dict = {'user_id': '', 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.GET_TIME_BASED_HISTORY.value, 'uid': thread_local_data.get_request_id(
            ), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': 'Exception happend at zep api end memory object found none'}
            publish_async('com.hertzai.longrunning.log', post_dict)
            messages = []  # Set empty list if search fails
        try:
            extracted_metadata = [message.message['metadata']
                                  for message in messages]
            list_req_ids = [data.get('request_Id', None)
                            for data in extracted_metadata]
            app.logger.info(f'GOT THE EXTRACTED METADATA AS {extracted_metadata}')
            thread_local_data.set_reqid_list(list_req_ids)
        except Exception as e:
            app.logger.error(f"Error while getting req ids {e}")

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
        app.logger.info(f"final-->{final_res}")
        end_time = time.time()
        elapsed_time = end_time - start_time
        return json.dumps(final_res)
    except Exception as e:
        app.logger.info(f"Exception {e}")
        try:
            messages = memory.chat_memory.search(prompt)
        except Exception as search_error:
            app.logger.error(f"Fallback search also failed: {search_error}")
            post_dict = {'user_id': '', 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.GET_TIME_BASED_HISTORY.value, 'uid': thread_local_data.get_request_id(
            ), 'task_id': f"{TaskNames.GET_ACTION_USER_DETAILS.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': 'Exception happend at zep api end memory object found none'}
            publish_async('com.hertzai.longrunning.log', post_dict)
            messages = []  # Set empty list if fallback search also fails

        # app.logger.info(f"final messages in except-->{messages}")
        try:
            extracted_metadata = [message.message['metadata']
                                  for message in messages]
            list_req_ids = [data.get('request_Id', None)
                            for data in extracted_metadata]
            thread_local_data.set_reqid_list(list_req_ids)
        except Exception as e:
            app.logger.info(f"Error while getting req ids {e}")
        end_time = time.time()
        elapsed_time = end_time - start_time
        app.logger.info(f"time taken for zep is {elapsed_time}")
        return json.dumps({'res': [message.message['content'] for message in messages] if messages else []})


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
        post_dict = {'user_id': '', 'task_type': 'async', 'status': TaskStatus.EXECUTING.value, 'task_name': TaskNames.ANIMATE_CHARACTER.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.ANIMATE_CHARACTER.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id()}
        publish_async('com.hertzai.longrunning.log', post_dict)
        prompt = string
        student_id_url = STUDENT_API

        payload = json.dumps({
            "user_id": thread_local_data.get_user_id()
        })
        headers = {
            'Content-Type': 'application/json'
        }

        response = requests.request(
            "POST", student_id_url, headers=headers, data=payload)
        if response.status_code == 200:
            favorite_teacher_id = response.json()["favorite_teacher_id"]

        get_image_by_id_url = f"{FAV_TEACHER_API}/{favorite_teacher_id}"

        payload = {}
        headers = {}

        response = requests.request(
            "GET", get_image_by_id_url, headers=headers, data=payload)

        image_name = response.json()["image_name"]

        image_url = response.json()["image_url"]
        image_response = pooled_get(image_url)
        image_content = image_response.content

        image_name = image_name.replace("vtoonify_", "", 1)
        folder_name = image_name.split(".")[0]
        inference_url = f"{DREAMBOOTH_API}/generate_images"
        payload = {'prompt': prompt}
        headers = {}
        logging.info("done till here")
        files = [
            # Use the correct content type
            ('image', ('image.jpeg', image_content, 'image/jpeg'))
        ]
        url = "http://20.197.30.74:8000/generate_image/"
        response = pooled_post(url, headers=headers,
                                 data=payload, files=files)
        if response.status_code == 200:
            return response.json()["url"]
        else:
            post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.ANIMATE_CHARACTER.value, 'uid': thread_local_data.get_request_id(
            ), 'task_id': f"{TaskNames.ANIMATE_CHARACTER.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at dreamooth api end for re {thread_local_data.get_request_id()}'}
            publish_async('com.hertzai.longrunning.log', post_dict)

    except Exception as e:
        # logging.info(f"exception {e}")
        time.sleep(30)
        post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.TIMEOUT.value, 'task_name': TaskNames.ANIMATE_CHARACTER.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.ANIMATE_CHARACTER.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at dreamooth api end for req_id {thread_local_data.get_request_id()} timed out'}
        publish_async('com.hertzai.longrunning.log', post_dict)
        return "something went wrong"


def parse_text_to_image(inp):
    '''
        stable diffusion
    '''
    try:

        post_dict = {'user_id': '', 'task_type': 'async', 'status': TaskStatus.EXECUTING.value, 'task_name': TaskNames.STABLE_DIFF.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.STABLE_DIFF.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id()}
        publish_async('com.hertzai.longrunning.log', post_dict)

        url = f'{STABLE_DIFF_API}?prompt={inp}'
        payload = {}

        headers = {}
        response = requests.request(
            "POST", url, headers=headers, data=payload, timeout=240)
        if response.status_code == 200:
            return response.json()["img_url"]
        else:
            post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.STABLE_DIFF.value, 'uid': thread_local_data.get_request_id(
            ), 'task_id': f"{TaskNames.STABLE_DIFF.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at stable diff for req_id: {thread_local_data.get_request_id()}'}
            publish_async('com.hertzai.longrunning.log', post_dict)
    except Exception as e:
        post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.TIMEOUT.value, 'task_name': TaskNames.ANIMATE_CHARACTER.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.ANIMATE_CHARACTER.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at stable diff for req_id: {thread_local_data.get_request_id()} timed out'}
        publish_async('com.hertzai.longrunning.log', post_dict)
        return f"{e} Not able to generating image at this moment please try later"


def parse_image_to_text(inp):
    '''
        LlaVA implemetation
    '''

    try:
        post_dict = {'user_id': '', 'task_type': 'async', 'status': TaskStatus.EXECUTING.value, 'task_name': TaskNames.LLAVA.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.LLAVA.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id()}
        publish_async('com.hertzai.longrunning.log', post_dict)
        inp_list = inp.split(',')
        url = f'{LLAVA_API}'
        payload = {
            'url': inp_list[0],
            'prompt': inp_list[1]
        }
        files = []
        headers = {}

        response = requests.request(
            "POST", url, headers=headers, data=payload, files=files, timeout=300)
        if response.status_code == 200:
            return response.text
        else:
            post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.LLAVA.value, 'uid': thread_local_data.get_request_id(
            ), 'task_id': f"{TaskNames.LLAVA.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at LLAVA for req_id: {thread_local_data.get_request_id()}'}
            publish_async('com.hertzai.longrunning.log', post_dict)
    except Exception as e:
        post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.TIMEOUT.value, 'task_name': TaskNames.LLAVA.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.LLAVA.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at LLAVA for req_id: {thread_local_data.get_request_id()} timed out'}
        publish_async('com.hertzai.longrunning.log', post_dict)
        return f'{e} Not able to generating answer at this moment please try later'


async def call_crwalab_api(input_url, input_str_list, user_id, request_id):
    try:
        app.logger.info("enter in call_crawlab_api function")
        app.logger.info(
            f"the input url is {input_url} and input_str_list is {input_str_list}")
        payload = {
            'link': input_str_list,
            'user_id': user_id,
            'request_id': request_id,
            'depth': '1',

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
        app.logger.info(
            f"we are in except of call_crawlab_api the error is {e}")
        url = RAG_API
        app.logger.info("going to except in Rag api")
        payload = {'url': input_url}
        files = []
        headers = {}

        response = requests.request(
            "POST", url, headers=headers, data=payload, files=files)
        return f'your url got uploaded and data extraction is being processes. Here is some brief information about url you hava provided {response.text}'


def start_async_tasks(coroutine):
    def run():
        from core.event_loop import get_or_create_event_loop
        loop = get_or_create_event_loop()
        loop.run_until_complete(coroutine)
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
    user_id = thread_local_data.get_user_id()
    request_id = thread_local_data.get_request_id()

    try:
        post_dict = {'user_id': '', 'task_type': 'async', 'status': TaskStatus.EXECUTING.value, 'task_name': TaskNames.CRAWLAB.value, 'uid': thread_local_data.get_request_id(
        ), 'task_id': f"{TaskNames.CRAWLAB.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id()}
        publish_async('com.hertzai.longrunning.log', post_dict)
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

                response = pooled_get(input_url)
                with open(pdf_save_path, 'wb') as file:
                    file.write(response.content)

                payload = {
                    'user_id': thread_local_data.get_user_id(),
                    'request_id': thread_local_data.get_request_id()
                }

                # Open the file and send it in the POST request
                with open(pdf_save_path, 'rb') as file:
                    files = [('file', (pdf_file_name, file, 'application/pdf'))]
                    response = pooled_post(
                        BOOKPARSING_API, data=payload, files=files)

                os.remove(pdf_save_path)

                return f"your request has been sent and pdf is getting uploading into our system {response.text}"
            except:
                app.logger.info("Got exception in book parsing api {e}")
                post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.CRAWLAB.value, 'uid': thread_local_data.get_request_id(
                ), 'task_id': f"{TaskNames.CRAWLAB.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at CRWALAB for req_id: {thread_local_data.get_request_id()} for pdf upload'}
                publish_async('com.hertzai.longrunning.log', post_dict)
                return "sorry I am not able to process your request at this moment"

        elif link_type == 'website':
            input_url_list = [input_url]
            app.logger.info(f"link type is {link_type}")
            input_str_list = repr(input_url_list)
            try:

                url = RAG_API
                payload = {'url': input_url}
                files = []
                headers = {}

                response = requests.request(
                    "POST", url, headers=headers, data=payload, files=files)

                app.logger.info(response.text)
                app.logger.info(f"RAG_API response {response.text}")
                app.logger.info("completed rag")
                try:
                    app.logger.info("going for crawlab api")
                    start_async_tasks(call_crwalab_api(
                        input_url, input_str_list, user_id, request_id))
                    app.logger.info("done for crawlab api")
                    return response.text
                except Exception as e:
                    app.logger.info(f"Got exception in crawlab api {e}")
                    post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.CRAWLAB.value, 'uid': thread_local_data.get_request_id(
                    ), 'task_id': f"{TaskNames.CRAWLAB.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at CRWALAB for req_id: {thread_local_data.get_request_id()} for weblink upload'}
                    publish_async('com.hertzai.longrunning.log', post_dict)
                    return f"sorry I am not able to process your request at this moment but here is some brief information about url you hava provided {response.text}"

                return f"your url got uploaded and data extraction is being processes. Here is some brief information about url you hava provided {response.text}"
            except Exception as e:
                app.logger.info(f"Got exception in crawlab api {e}")
                post_dict = {'user_id': thread_local_data.get_user_id(), 'status': TaskStatus.ERROR.value, 'task_name': TaskNames.CRAWLAB.value, 'uid': thread_local_data.get_request_id(
                ), 'task_id': f"{TaskNames.CRAWLAB.value}_{str(thread_local_data.get_request_id())}", 'request_id': thread_local_data.get_request_id(), 'failure_reason': f'Exception happend at CRWALAB for req_id: {thread_local_data.get_request_id()} for weblink upload'}
                publish_async('com.hertzai.longrunning.log', post_dict)
                return "sorry I am not able to process your request at this moment"

        else:
            return "Sorry I am unable to process your request with this url type"
    except Exception as e:
        pass


redis_client = redis.StrictRedis(
    host='azure_all_vms.hertzai.com', port=6369, db=0)


def get_frame(user_id):
    serialized_frame = redis_client.get(user_id)

    try:
        if serialized_frame is not None:
            from security.safe_deserialize import safe_load_frame
            frame_bgr = safe_load_frame(serialized_frame)
            app.logger.info(
                f"Frame for user_id {user_id} retrieved successfully.")
            frame = frame_bgr[:, :, ::-1]
            return frame
        else:
            app.logger.info(f"No frame found for user_id {user_id}.")
            return None
    except ModuleNotFoundError as e:
        app.logger.info("ModuleNotFoundError: %s", "Numpy errr", exc_info=True)
        app.logger.info("Numpy version: %s", np.__version__)
        app.logger.info("Numpy location: %s", np.__file__)
        raise e


def parse_visual_context(inp: str):
    user_id = thread_local_data.get_user_id()
    request_id = thread_local_data.get_request_id()
    app.logger.info('Using Vision to answer question')
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
            response = pooled_post(
                url, headers=headers, data=payload, files=files)
            app.logger.info(response.text)
            response = response.text

            return response
        except Exception as e:
            app.logger.error('Got error in visal QA')


def parse_user_id(inp: str):
    url = 'https://azurekong.hertzai.com:8443/db/getstudent_by_user_id'

    headers = {
        'Content-Type': 'application/json'
    }

    try:
        prov_user_id = re.findall('\d', inp)[0]
    except:
        pass
    finally:
        prov_user_id = ""

    payload = json.dumps({
        "user_id": thread_local_data.get_user_id()
    })

    response = requests.request("POST", url, headers=headers, data=payload)
    if prov_user_id == "" or int(prov_user_id) != thread_local_data.get_user_id():

        return f"you might interested in finding your user detail here are details {response.text}"

    else:
        return response.text


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
        text = asyncio.run(async_main(top_2_search_res_link))
        # Removing punctuation and extra characters
        app.logger.info(text)
        cleaned_text = re.sub(r'[^\w\s]', '', text[0] +
                              " "+text[1])  # Remove punctuation
        # Remove extra newlines and leading/trailing whitespaces
        cleaned_text = re.sub(r'\n+', '\n', cleaned_text).strip()
    except RuntimeError as e:
        app.logger.error(f"Runtime error occurred: {e}")

    final_res.append({'text': cleaned_text, 'source': top_2_search_res_link})
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
                    escape_chars = ['\n', '\t', '\r',
                                    '\"', "\'", '\\', "'''", '"""']
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
                        parsed_json = parse_json_markdown(json_string.replace('\n', '').replace('\t', '').replace('\r', '').replace(
                            '\"', '').replace("\'", '').replace('\\', '').replace("'''", '').replace('"""', '').replace('`', ''))
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
                        app.logger.info(
                            "Caught inner Exception: {innerException}")
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
                # Fallback: treat entire response as final answer
                app.logger.info("No parsable format found, using raw text as final answer")
                return AgentFinish({"output": text.strip()}, text)

    @property
    def _type(self) -> str:
        return "conversational_chat"


# Store user-specific agents and their chat history
# Only initialize if autogen is available
if autogen is not None:
    user_agents_creator: Dict[str, Tuple[autogen.AssistantAgent,
                                         autogen.UserProxyAgent]] = {}

    # Store agents for each user
    user_agents: Dict[str, Tuple[autogen.ConversableAgent,
                                 autogen.ConversableAgent]] = {}
else:
    user_agents_creator: Dict[str, Tuple] = {}
    user_agents: Dict[str, Tuple] = {}


# Define autogen-dependent functions only if autogen is available
if autogen is not None:
    def create_agents_for_user(user_id: str) -> Tuple[autogen.AssistantAgent, autogen.UserProxyAgent]:
        """Create new assistant and user proxy agents for a user with basic configuration."""
        config_list = [{
            "model": 'hertzai-4o',
            "api_type": "azure",
            "api_key": '4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf',
            "base_url": 'https://hertzai-gpt4.openai.azure.com/',
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
            is_termination_msg=lambda x: True if "TERMINATE" in x.get(
                "content") else False,
            system_message="""You are a custom agent bot creator. Your task is to interact with the user to gather all the necessary details to create an agent. Once you have collected all the required information, you will generate a complete agent configuration.

            The information you need to collect includes:

            Name: The name of the agent.
            Purpose: A brief description of the agent's purpose.
            Flow: The operational flow of the agent.
            Actions: The actions the agent will perform.
            Goal: The ultimate goal of the agent.
            Guidelines for Responses:

            If you are still gathering information, your response should be formatted as: { "status": "pending", "question": "The question you want to ask" }
            Once all the requirements are complete and you are ready to provide the agent configuration, your response should be formatted as: { "status": "completed", "name": "","Recipe":"", "purpose": "", "tools": "", "flow": { "actions": [] }, "goal": ""}
            the recipe should be containing all the steps
            necessary and name for it. Suggest well-documented, generalized python function(s)
            only if necessary if the task is achievable without python then dont give python code just give the prompt
            to perform similar tasks for coding steps in future. Make sure coding steps and
            non-coding steps are never mixed in one function. In the docstr of the function(s),
            clarify what non-coding steps are needed to use the language skill of the assistant.
            """
        )

        # Create the user proxy agent
        user_proxy = autogen.UserProxyAgent(
            name=f"user_proxy_{user_id}",
            human_input_mode="NEVER",
            is_termination_msg=lambda x: True if "TERMINATE" in x.get(
                "content") else False,
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
            context_str = "\n".join(
                [f"{msg['role']}: {msg['content']}" for msg in context])

            # Append context to the message if there's history
            enhanced_message = message
            if context:
                enhanced_message = f"Previous conversation:\n{context_str}\n\nCurrent message: {message}"

            # Send message and get response
            response = user_proxy.send(
                enhanced_message,
                assistant,
                request_reply=True
            )

            key = list(user_proxy.chat_messages.keys())[0]

            return user_proxy.chat_messages[key][-1]['content']

        except Exception as e:
            return f"Error getting response: {str(e)}"


    def create_agents(user_id: str,recipe:str) -> Tuple[autogen.ConversableAgent, autogen.ConversableAgent]:
        """Create new assistant and user agents for a given user_id"""

        llm_config = {
            "temperature": 0.7,
            "config_list": [{
            "model": 'hertzai-4o',
            "api_type": "azure",
            "api_key": '4xmi9X9pGCwRn2Pb0vldz6t6FQaAe29bUIkFjKRC7ytrVZ1Ni5cWJQQJ99BAACHYHv6XJ3w3AAABACOG99Zf',
            "base_url": 'https://hertzai-gpt4.openai.azure.com/',
            "api_version": "2024-02-15-preview"
        }],
        }
        conversation = True
        if conversation:
            recipe = recipe+'\n Note: Wait for user confirmation to proceed after every action.'

        # Create assistant agent
        assistant = autogen.ConversableAgent(
            name=f"assistant_{user_id}",
            llm_config=llm_config,
            is_termination_msg=lambda x: True if "TERMINATE" in x.get(
                "content") else False,
            system_message=recipe
        )

        # Create user agent
        user = autogen.ConversableAgent(
            name=f"user_{user_id}",
            is_termination_msg=lambda x: True if "TERMINATE" in x.get(
                "content") else False,
            llm_config=None,  # User agent doesn't need LLM
            human_input_mode="NEVER",  # We'll manually send messages
            max_consecutive_auto_reply=1  # Limit to 1 auto reply
        )

        return user, assistant

else:
    # Provide stub functions when autogen is not available
    def create_agents_for_user(user_id: str):
        raise ImportError("autogen package is not installed")

    def get_agent_response(assistant, user_proxy, message: str) -> str:
        raise ImportError("autogen package is not installed")

    def create_agents(user_id: str, recipe: str):
        raise ImportError("autogen package is not installed")


# main function
def get_ans(casual_conv, req_tool, user_id, query, custom_prompt, preferred_lang):
    start_time = time.time()
    user_details, actions = get_action_user_details(user_id=user_id)
    app.logger.info(
        "time taken by get_action_user_details %s seconds", time.time() - start_time)
    app.logger.info(casual_conv)
    llm = CustomGPT(casual_conv=casual_conv)
    app.logger.info(f"query------> {query}")
    memory_start_time = time.time()
    memory = get_memory(user_id=user_id)
    app.logger.info("time taken by get_memory %s seconds",
                    time.time() - memory_start_time)

    tools_start_time = time.time()
    tools = get_tools(req_tool=req_tool, is_first=True)
    app.logger.info("time taken by get_tools %s seconds",
                    time.time() - tools_start_time)

    app.logger.info(f'tools {type(tools)}')
    language = SUPPORTED_LANG_DICT.get(preferred_lang[:2], 'English')
    colloquial = True

    prefix = f"""You are Hevolve, an expert educational AI teacher with knowledge in every field.
        Answer questions accurately and respond as quickly as possible in {language}.
        Keep responses under 200 words. Be colloquial and natural - don't always greet or use the user's name.

        User details: {user_details}
        Context: {custom_prompt}

        You can answer questions, provide revisions, conduct assessments, teach topics, create curriculum, and assist with research.
        Your responses are conveyed via video with an avatar and text-to-speech.

        IMPORTANT: Always respond with valid JSON in a markdown code block with "action" and "action_input" fields.

        Previous user actions: {actions}

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

            always create parsable output."""+f'''
            <RESPONSE_INSTRUCTIONS_START>
            The response should be Colloquial in nature,
            The response language should be: {language}'''+"""
            <RESPONSE_INSTRUCTIONS_END>

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

            always create parsable output."""+f'''
            <RESPONSE_INSTRUCTIONS_START>
            The response should be Colloquial in nature,
            The response language should be: {language}'''+"""
            <RESPONSE_INSTRUCTIONS_END>

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

    # chat Agent

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
    ans = agent_chain.run({'input': query})
    app.logger.info("time taken by chain agent run %s seconds",
                    time.time() - agent_chain_start_time)
    end_time = time.time()
    elapse_time = end_time-start_time
    app.logger.info(
        f"total time taken by get_ans function %s seconds", elapse_time)
    return ans


Hevolve = "You are Hevolve, a highly intelligent educational AI developed by HertzAI."
PROBE_TEMPLATE = ("You are Hevolve, a highly intelligent educational AI developed by HertzAI. Weave the conversation "
                  "history along with the Last_5_Minutes_Visual_Context if present to create a clear, engaging, "
                  "coherent conversation flow that encourages the user to respond. Complete your response in 130 words"
                  "Your response should not be more than 130 words. Neither repeat the previous "
                  "responses nor be monotonous, be creative and talk about intriguing awe-inspiring facts, "
                  "or with some interesting age appropriate casual conversations which will make you the single point "
                  "of contact for everything in the world. Greet if & only if the context demands you to, "
                  "build a dialogue, use user\'s name only when necessary, Do not sound robotic. If the user is not "
                  "actively engaging or if visual context is present but user not visible or if user visible but not "
                  "looking at camera (based on visual and conversation history timestamps) call out their name loud "
                  "or try singing a song to bring back their attention using a SEEK_ATTENTION tool with input like a "
                  "song made of knowledge based on user's age, or calling their name loud e.g. tool input: "
                  "\'<seek_attend_loud>Hey <username>, are you there</seek_attend_loud>\' or  "
                  "\'<seek_attend_lyrics>Some awesome lyrics</seek_attend_lyrics>\' . Continue the Conversation from "
                  "where I or you left off."
                  )
INTERMEDIATE_CONTINUATION = "You are Hevolve, a highly intelligent educational AI developed by HertzAI. Continue your response from where you left off in the last conversation, considering the new input as a continuation of the last request. Ensure a smooth transition from the previous response and start this response as a continuation of the previous one.\n INSTRUCTIONS: Start your response with transitional words or phrases that can be used as a continuation of the previous response."

first_promts = []
review_agents = {"10077":True,10077:True}
conversation_agent = {"10077":False,10077:False}
@app.route('/chat', methods=['POST'])
def chat():

    start_time = time.time()
    data = request.get_json()
    user_id = data.get('user_id', None)
    preferred_lang = data.get('preferred_lang', 'en')
    request_id = data.get('request_id', None)
    req_tool = data.get('tools', None)
    file_id = data.get('file_id', None)
    prompt_id = data.get('prompt_id', None)
    create_agent = data.get('create_agent', None)
    casual_conv = data.get('casual_conv', True)
    probe = data.get('probe', None)
    intermediate = data.get('intermediate', None)
    app.logger.info(f"casual_conv type {casual_conv}")

    # return ""
    thread_local_data.set_request_id(request_id=request_id)
    prompt = data.get('prompt', None)

    # Security: Prompt injection detection
    if prompt:
        try:
            from security.prompt_guard import check_prompt_injection
            is_safe, reason = check_prompt_injection(prompt)
            if not is_safe:
                app.logger.warning(f"Prompt injection detected: {reason}")
                return jsonify({'error': f'Input rejected: {reason}'}), 400
        except Exception:
            pass  # Degrade gracefully

    if prompt_id:
        if os.path.exists(f'prompts/{prompt_id}.json'):
            app.logger.info('GATHER JSON EXISTS')
            if os.path.exists(f'prompts/{prompt_id}_0_recipe.json'):
                app.logger.info('0 Recipe JSON EXISTS')
                file_path = f'prompts/{prompt_id}.json'
                with open(file_path, 'r') as f:
                    data = json.load(f)
                    no_of_flow = len(data['flows'])-1
                    app.logger.info(f'GOT LEN OF FLOW AS {no_of_flow}')
                if os.path.exists(f'prompts/{prompt_id}_{no_of_flow}_recipe.json'):
                    create_agent = set_flags_to_enter_review_mode(no_of_flow, user_id) #returns false
                else:
                    app.logger.info(f'{no_of_flow} Recipe JSON doesnot EXISTS')
                    create_agent = True
                    review_agents[user_id] = True
                    conversation_agent[user_id] = False
            else:
                app.logger.info('0 Recipe JSON doesnot EXISTS')
                create_agent = True
                review_agents[user_id] = True
                conversation_agent[user_id] = False

        else:
            app.logger.info('GATHER JSON doesnot EXISTS')
            create_agent = True
            review_agents[user_id] = False
            conversation_agent[user_id] = True

    if create_agent:
        # Phase 1: Gather Requirements
        if user_id not in review_agents.keys() or review_agents[user_id] == False:
            review_agents[user_id] = False
            prompt = data.get('prompt', None)
            if prompt_id not in first_promts:
                first_promts.append(prompt_id)
                try:
                    res = pooled_get(
                        f'{DB_URL}/getprompt/?prompt_id={prompt_id}').json()
                    prompt = prompt+f" name:{res[0]['name']} goal:{res[0]['prompt']}"
                except:
                    app.logger.error(f'GOT DB ERROR FOR PROMPTID:{prompt_id}')
            if not user_id or not prompt:
                return jsonify({'response': 'Need user_id and text to create agent', 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': []})
            from gather_agentdetails import gather_info
            response = gather_info(user_id,prompt,prompt_id)
            new_response = response.replace('true','True').replace("false", "False")
            app.logger.info('AFTER GATHER INFO')
            try:
                try:
                    new_res = retrieve_json(new_response)
                    app.logger.info(f"new_res: {new_res}")
                except Exception as e:
                    app.logger.error(f'Got some error while will try with re match error:{e}')
                    json_match = re.search(r'{[\s\S]*}', response)
                    app.logger.info(f'Json match result: {json_match}')
                    if json_match:
                        app.logger.info(f'Inside json_match')
                        json_part = json_match.group(0)
                        app.logger.info(f'Before loads json_part:{json_part}')
                        new_res = json.loads(json_part)
                        app.logger.info(f'After loads new_res:{new_res}')
                    else:
                        raise 'No Json in response'
                app.logger.info('AFTER EVAL')
                if new_res['status'] == 'pending':
                    app.logger.info('PENDING STATUS')
                    ans = new_res['question'] if 'question' in new_res else new_res['review_details']
                    return jsonify({'response': ans, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Creation Mode'})
                else:
                    app.logger.info('COMPLETED STATUS')
                    new_res['prompt_id'] = prompt_id
                    new_res['creator_user_id'] = user_id
                    conversation_agent[user_id] = False
                    app.logger.info(
                        'Agent Created Successfully saving it and reusing it for further purpose')
                    name = f'prompts/{prompt_id}.json'
                    with open(name, "w") as json_file:
                        json.dump(new_res, json_file)
                    app.logger.info(f"Dictionary saved to {name}")
                    review_agents[user_id] = True
                    return jsonify({'response': 'Got Agent details successfully lets move on to review them one at a time', 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Review Mode'})
            except Exception as e:
                app.logger.error('GOT some error while eval and returning the response')
                app.logger.error(e)
                return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Creation Mode'})
        # Phase 2: Review Phase
        if review_agents[user_id] and not conversation_agent[user_id]:
            response = recipe(user_id,prompt,prompt_id,file_id,request_id)
            if response =='Agent Created Successfully':
                conversation_agent[user_id] = True
                # Bridge: auto-create social identity for this agent
                try:
                    _create_social_agent_from_prompt(user_id, prompt_id)
                except Exception as e:
                    app.logger.debug(f"Social agent bridge skipped: {e}")
                return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'completed'})
            return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Review Mode'})
        # Phase 3: Evaluation Phase
        if review_agents[user_id] and conversation_agent[user_id]:
            return evaluate_agent_after_creation_in_review(file_id, prompt, prompt_id, request_id, user_id)

    if prompt_id and os.path.exists(f'prompts/{prompt_id}.json'):

        with open(f'prompts/{prompt_id}.json', "r") as file:
            created_json = json.load(file)


        response = chat_agent(user_id,prompt,prompt_id,file_id,request_id)

        # if not user_id or not prompt:
        #     return jsonify({'response': 'Need user_id and text to use agent', 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': []})
        # last_response = ''
        #create and user use_recipe.py
        return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0, 'history_request_id': [],'Agent_status':'Reuse Mode'})

    if prompt_id:
        try:
            res = pooled_get(
                f'{DB_URL}/getprompt/?prompt_id={prompt_id}').json()
            # use config for url
            custom_prompt = res[0]['prompt']
            if res[0]['prompt'] == 'Learn Language':
                app.logger.info(
                    'found Learn languague getting user preffered language')
                lang = pooled_post('{}/getstudent_by_user_id'.format(DB_URL),
                                     data=json.dumps({"user_id": user_id})).json()
                language = lang['preferred_language'][:2]
                app.logger.info(f'user preffered language is {language}')
                custom_prompt = custom_prompt + \
                    f' The Language selected is {language}'
                app.logger.info(f"custom prompt is: {custom_prompt}")

        except:
            app.logger.error(f'failed to get prompt from id:- {prompt_id}')
            custom_prompt = Hevolve
    elif probe:
        custom_prompt = PROBE_TEMPLATE
        prompt_id = 0
    elif intermediate:
        custom_prompt = INTERMEDIATE_CONTINUATION
        prompt_id = 0
    else:
        custom_prompt = Hevolve  # use Hevolve from config/template
        prompt_id = 0
    app.logger.info(f'{custom_prompt}-->{prompt_id}')

    post_dict = {'user_id': user_id, 'status': 'INITIALIZED', 'task_name': "CHAT",
                 'uid': request_id, 'task_id': f"CHAT_{str(request_id)}", 'request_id': request_id}
    publish_async('com.hertzai.longrunning.log', post_dict)

    thread_local_data.set_user_id(user_id=user_id)
    thread_local_data.set_req_token_count(value=0)
    thread_local_data.set_res_token_count(value=0)
    thread_local_data.set_recognize_intents()
    thread_local_data.set_global_intent(global_intent=req_tool)
    thread_local_data.set_prompt_id(prompt_id)

    prompt = data.get('prompt', None)
    if probe:
        prompt = ''

    app.logger.info(
        "the time taken before get ans in main api is %s seconds", time.time() - start_time)
    ans_start_time = time.time()
    ans = get_ans(casual_conv, req_tool, user_id=user_id,
                  query=prompt, custom_prompt=custom_prompt, preferred_lang=preferred_lang)
    app.logger.info("the time taken by get ans in main api is %s seconds",
                    time.time() - ans_start_time)
    if req_tool == 'Image_Inference_Tool':
        action_response = pooled_post(f'{DB_URL}/create_action',)
        payload = json.dumps({
            "conv_id": None,
            "user_id": user_id,
            "action": f"{ans}",
            "zeroshot_label": "Image Inference",
            "gpt3_label": "Visual Context"
        })
        headers = {
            'Content-Type': 'application/json'
        }
        action_response = pooled_post(f'{DB_URL}/create_action', headers=headers, data=payload)
    if ans != "":
        post_dict = {'user_id': user_id, 'status': 'FINISHED', 'task_name': "CHAT",
                     'uid': request_id, 'task_id': f"CHAT_{str(request_id)}", 'request_id': request_id}
        publish_async('com.hertzai.longrunning.log', post_dict)
    else:
        post_dict = {'user_id': user_id, 'status': 'ERROR', 'task_name': "CHAT", 'uid': request_id,
                     'task_id': f"CHAT_{str(request_id)}", 'request_id': request_id, 'failure_reason': 'Got null response from GPT'}
        publish_async('com.hertzai.longrunning.log', post_dict)

    end_time = time.time()
    elapsed_time = end_time - start_time
    app.logger.info(f"time taken for this full call is {elapsed_time}")

    return jsonify({'response': ans, 'intent': thread_local_data.get_recognize_intents(), 'req_token_count': thread_local_data.get_req_token_count(), 'res_token_count': thread_local_data.get_res_token_count(), 'history_request_id': thread_local_data.get_reqid_list()})


def evaluate_agent_after_creation_in_review(file_id, prompt, prompt_id, request_id, user_id):
    response = chat_agent(user_id, prompt, prompt_id, file_id, request_id)
    return jsonify({'response': response, 'intent': ['FINAL_ANSWER'], 'req_token_count': 0, 'res_token_count': 0,
                    'history_request_id': [], 'Agent_status': 'Evaluation Mode'})


def set_flags_to_enter_review_mode(no_of_flow, user_id):
    app.logger.info(f'{no_of_flow} Recipe Json exist Going to reuse')
    create_agent = False
    review_agents[user_id] = True
    conversation_agent[user_id] = False
    return create_agent


@app.route('/time_agent',methods=['POST'])
def time_agent():
    app.logger.info('GOT REQUEST IN TIME AGENT API')
    data = request.get_json()
    task_description = data.get('task_description',None)
    user_id = data.get('user_id',None)
    request_from = data.get('request_from',"Reuse")
    prompt_id = data.get('prompt_id',None)
    action_entry_point = data.get('prompt_id',0)
    if not task_description or not user_id or not prompt_id:
        return jsonify({'error':'user_id or task_description or prompt_id is missing'}), 404
    app.logger.info(f'GOT user_id:{user_id} & prompt_id:{prompt_id} & task_description:{task_description}')
    if request_from == 'Reuse':
        res = time_based_execution(str(task_description),int(user_id),int(prompt_id),action_entry_point)
    else:
        res = time_execution(str(task_description),int(user_id),int(prompt_id),action_entry_point)
    return jsonify({'response':f'{res}'}), 200


@app.route('/visual_agent',methods=['POST'])
def visual_agent():
    app.logger.info('GOT REQUEST IN Visual AGENT API')
    data = request.get_json()
    task_description = data.get('task_description',None)
    user_id = data.get('user_id',None)
    request_from = data.get('request_from',"Reuse")
    prompt_id = data.get('prompt_id',None)
    if not task_description or not user_id or not prompt_id:
        return jsonify({'error':'user_id or task_description or prompt_id is missing'}), 404
    app.logger.info(f'GOT user_id:{user_id} & prompt_id:{prompt_id} & task_description:{task_description}')
    if request_from == 'Reuse':
        res = visual_based_execution(str(task_description),int(user_id),int(prompt_id))
    else:
        res = visual_execution(str(task_description),int(user_id),int(prompt_id))
    return jsonify({'response':f'{res}'}), 200

@app.route('/response_ack',methods=['POST'])
def response_ack():
    app.logger.info('GOT REQUEST IN response_ack')
    data = request.get_json()
    user_id = data.get('user_id',None)
    request_id = data.get('request_id',None)
    thread_local_data.set_request_id(request_id=request_id)
    prompt_id = data.get('prompt_id',None)

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
            metadata={'prompt_id': 0}
        )
        memory.chat_memory.add_message(
            AIMessage(content=ai_msg),
            metadata={'prompt_id': 0}
        )
        return jsonify({'response': "Messages are saved!!!"}), 200
    else:
        return jsonify({'response': "Memory object not found"}), 400


# ═══════════════════════════════════════════════════════════════
# Social Bridge: auto-create social user when agent is created via /chat
# ═══════════════════════════════════════════════════════════════

def _create_social_agent_from_prompt(user_id, prompt_id):
    """Read prompts/{prompt_id}.json and create a social User for this agent."""
    prompt_file = f'prompts/{prompt_id}.json'
    if not os.path.exists(prompt_file):
        return
    with open(prompt_file, 'r') as f:
        data = json.load(f)

    agent_display_name = data.get('name', f'Agent {prompt_id}')
    agent_name = data.get('agent_name', '')  # 3-word name from LLM
    goal = data.get('goal', '')

    from integrations.social.models import get_db
    from integrations.social.services import UserService
    db = get_db()
    try:
        # Use LLM-generated 3-word name if available, otherwise generate one
        if not agent_name:
            from integrations.social.agent_naming import generate_agent_name
            suggestions = generate_agent_name(db, count=1)
            agent_name = suggestions[0] if suggestions else f"agent-{prompt_id}"

        try:
            user = UserService.register_agent(
                db, agent_name, goal or agent_display_name,
                agent_id=str(prompt_id), owner_id=str(user_id),
                skip_name_validation=not bool(agent_name))
            user.display_name = agent_display_name
            db.flush()
        except ValueError:
            pass  # already exists

        db.commit()
        app.logger.info(f"Social agent created: {agent_name} for prompt {prompt_id}")
    except Exception as e:
        db.rollback()
        app.logger.debug(f"Social bridge error: {e}")
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# Local-first Prompt CRUD (syncs to cloud DB)
# ═══════════════════════════════════════════════════════════════

@app.route('/prompts', methods=['GET'])
def get_prompts():
    """List prompts for a user. Local-first, cloud DB fallback."""
    req_user_id = request.args.get('user_id', '')
    if not req_user_id:
        return jsonify({'error': 'user_id required'}), 400

    prompts = []

    # 1. Read from local prompts/*.json files
    prompts_dir = os.path.join(os.path.dirname(__file__), 'prompts')
    if os.path.isdir(prompts_dir):
        for fname in os.listdir(prompts_dir):
            if fname.endswith('.json') and '_' not in fname:
                try:
                    fpath = os.path.join(prompts_dir, fname)
                    with open(fpath, 'r') as f:
                        data = json.load(f)
                    pid = fname.replace('.json', '')
                    creator = str(data.get('creator_user_id', ''))
                    if creator == str(req_user_id) or not creator:
                        prompts.append({
                            'prompt_id': pid,
                            'name': data.get('name', ''),
                            'prompt': data.get('goal', ''),
                            'agent_name': data.get('agent_name', ''),
                            'is_active': data.get('status', '') == 'completed',
                            'user_id': creator or req_user_id,
                            'has_recipe': os.path.exists(
                                os.path.join(prompts_dir, f'{pid}_0_recipe.json')),
                            'flow_count': len(data.get('flows', [])),
                            'source': 'local',
                        })
                except Exception:
                    continue

    # 2. Fallback: try cloud DB if no local results
    if not prompts:
        try:
            res = pooled_get(
                f'{DB_URL}/getprompt_onlyuserid/?user_id={req_user_id}',
                timeout=5)
            if res.status_code == 200:
                cloud_data = res.json()
                for item in cloud_data:
                    item['source'] = 'cloud'
                    item['has_recipe'] = False
                prompts = cloud_data
        except Exception:
            pass

    return jsonify(prompts)


@app.route('/prompts', methods=['POST'])
def create_prompts():
    """Create/update prompts. Saves locally AND syncs to cloud DB."""
    data = request.get_json()
    listprompts = data.get('listprompts', [data] if 'name' in data else [])

    saved = []
    for item in listprompts:
        pid = item.get('prompt_id')
        if not pid:
            # Auto-assign prompt_id from existing files
            existing = [f.replace('.json', '') for f in os.listdir('prompts')
                        if f.endswith('.json') and '_' not in f and f[0].isdigit()]
            pid = max([int(x) for x in existing if x.isdigit()] or [0]) + 1
            item['prompt_id'] = pid

        # Save locally
        local_path = f'prompts/{pid}.json'
        local_data = {}
        if os.path.exists(local_path):
            with open(local_path, 'r') as f:
                local_data = json.load(f)

        local_data['name'] = item.get('name', local_data.get('name', ''))
        local_data['goal'] = item.get('prompt', item.get('goal', local_data.get('goal', '')))
        local_data['prompt_id'] = pid
        local_data['creator_user_id'] = item.get('user_id', local_data.get('creator_user_id'))
        if 'agent_name' in item:
            local_data['agent_name'] = item['agent_name']
        if 'status' not in local_data:
            local_data['status'] = 'pending'

        with open(local_path, 'w') as f:
            json.dump(local_data, f, indent=2)

        saved.append({'prompt_id': pid, 'name': local_data['name']})

    # Sync to cloud DB (non-blocking, best effort)
    try:
        pooled_post(
            f'{DB_URL}/createpromptlist',
            json={'listprompts': listprompts},
            timeout=5)
    except Exception as e:
        app.logger.debug(f"Cloud sync failed (non-fatal): {e}")

    return jsonify({'success': True, 'saved': saved})


@app.route('/status', methods=['GET'])
def status():
    return jsonify({'response': 'Working...'})

@app.route('/zeroshot/', methods=['POST'])
def zeroshot():
    """
    Zero-shot classification endpoint using GPT-4.1-mini model.

    Request JSON format:
    {
        "input_text": "text to classify",
        "labels": ["label1", "label2", "label3"],
        "multi_label": false  // optional, default is false
    }

    Response format:
    {
        "sequence": "input text",
        "labels": ["label1", "label2", "label3"],
        "scores": [0.85, 0.10, 0.05]
    }
    """
    try:
        # Get request data
        data = request.get_json(force=True)
        input_text = data.get('input_text', '')
        labels = data.get('labels', [])
        multi_label = data.get('multi_label', False)

        # Validate inputs
        if not input_text:
            return jsonify({"error": "input_text is required"}), 400
        if not labels or len(labels) == 0:
            return jsonify({"error": "labels list is required and cannot be empty"}), 400

        # Create prompt for zero-shot classification
        if multi_label:
            prompt = f"""You are a text classification system. Given the following text and a list of labels, determine which labels apply to the text. Multiple labels can apply.

Text: "{input_text}"

Available labels: {', '.join(labels)}

For each label, provide a confidence score between 0 and 1 indicating how well it applies to the text. The scores don't need to sum to 1.

Respond ONLY with a JSON object in this exact format:
{{"scores": {{"label1": score1, "label2": score2, ...}}}}

Example response format:
{{"scores": {{"sports": 0.85, "entertainment": 0.60, "politics": 0.15}}}}"""
        else:
            prompt = f"""You are a text classification system. Given the following text and a list of labels, classify the text into ONE of the provided labels.

Text: "{input_text}"

Available labels: {', '.join(labels)}

Provide a confidence score between 0 and 1 for each label. The scores should sum to approximately 1.0, with the highest score indicating the most likely label.

Respond ONLY with a JSON object in this exact format:
{{"scores": {{"label1": score1, "label2": score2, ...}}}}

Example response format:
{{"scores": {{"sports": 0.75, "entertainment": 0.15, "politics": 0.10}}}}"""

        app.logger.info(f"Zero-shot classification request - Text: {input_text[:100]}..., Labels: {labels}")

        # Call Llama API
        response = pooled_post(
            GPT_API,
            json={
                "model": "llama",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500
            }
        )

        app.logger.info(f"GPT API response status: {response.status_code}")

        if response.status_code != 200:
            return jsonify({"error": "GPT API request failed", "details": response.text}), 500

        # Parse GPT response
        gpt_result = response.json()
        app.logger.info(f"GPT response: {gpt_result}")

        # Extract the response content
        if isinstance(gpt_result, dict) and 'text' in gpt_result:
            response_text = gpt_result['text']
        elif isinstance(gpt_result, dict) and 'response' in gpt_result:
            response_text = gpt_result['response']
        elif isinstance(gpt_result, dict) and 'choices' in gpt_result:
            response_text = gpt_result['choices'][0]['message']['content']
        else:
            response_text = str(gpt_result)

        app.logger.info(f"Response text: {response_text}")

        # Parse JSON from response
        try:
            # Try to find JSON in the response
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                parsed_response = json.loads(json_match.group())
            else:
                parsed_response = json.loads(response_text)

            scores_dict = parsed_response.get('scores', {})

            # Convert to list format sorted by scores (descending)
            sorted_items = sorted(scores_dict.items(), key=lambda x: x[1], reverse=True)
            sorted_labels = [item[0] for item in sorted_items]
            sorted_scores = [item[1] for item in sorted_items]

            # Build response in format similar to transformers zero-shot-classification
            result = {
                "sequence": input_text,
                "labels": sorted_labels,
                "scores": sorted_scores
            }

            app.logger.info(f"Final result: {result}")
            return jsonify(result)

        except json.JSONDecodeError as e:
            app.logger.error(f"JSON parsing error: {e}, Response: {response_text}")
            # Fallback: return equal probabilities if parsing fails
            equal_score = 1.0 / len(labels)
            return jsonify({
                "sequence": input_text,
                "labels": labels,
                "scores": [equal_score] * len(labels),
                "warning": "Could not parse model response, returning equal probabilities"
            })

    except Exception as e:
        app.logger.error(f"Error in /zeroshot/ endpoint: {str(e)}")
        return jsonify({"error": str(e)}), 500

def main():
    """
    Main entry point for hevolve-server CLI command.
    Starts the Flask server using waitress.
    """
    serve(app, host='0.0.0.0', port=6778, threads=50)


if __name__ == '__main__':
    main()
    # app.debug = True
    # flask_thread = threading.Thread(target=lambda: serve(app, host='0.0.0.0', port=6777))
    # flask_thread.daemon = True
    # flask_thread.start()
    # from crossbar_server import component
    # # Run the WAMP client
    # run([component])

