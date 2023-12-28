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

## logging info
logging.basicConfig(level=logging.DEBUG)
handler = RotatingFileHandler('flask_app.log', maxBytes=100000, backupCount=3)

# Set the logging level for the file handler
handler.setLevel(logging.DEBUG)

# Create a logging format
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)

app = Flask(__name__)

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
        if self.count >= 5 or self.call_gpt4 ==1:
            response = requests.post(
                GPT_API,
                json={
                "model": "gpt-4",
                "data": [{"role":"user","content":prompt}],
                "max_token":1000
                }
            )
        else:
            response = requests.post(
                GPT_API,
                json={
                "model": "gpt-4",
                "data": [{"role":"user","content":prompt}],
                "max_token":1000
                }
            )

        response.raise_for_status()
        app.logger.info(f"hellpppppppppppppppp-->{response.json()['text']}")
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
            time.sleep
            if '"Final Answer"' in text or '"Final_Answer"' in text:
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
                json_string = text[start_index:end_index]
                response = parse_json_markdown(json_string)
                action, action_input = response["action"], response["action_input"]
                return AgentAction(action, action_input, text)
                # raise OutputParserException(f"Could not parse LLM output: {text}") from e

    @property
    def _type(self) -> str:
        return "conversational_chat"


class CustomAgentExecutor(AgentExecutor):

    def prep_outputs(self, inputs: Dict[str, str],
                    outputs: Dict[str, str],
                    metadata: Optional[Dict[str, Any]] = None,
                    return_only_outputs: bool = False) -> Dict[str, str]:
        # pdb.set_trace()
        self._validate_outputs(outputs)
        req_id = thread_local_data.get_request_id()
        metadata = {'request_Id':req_id}
        app.logger.info(f"before: memory object is not none and metadata is {metadata}, {return_only_outputs}")
        if self.memory is not None:
            app.logger.info(f"memory object is not none and metadata is {metadata}")
            self.memory.save_context(inputs, outputs, metadata)
        app.logger.info(f"After: memory object is not none and metadata is {metadata}")
        if return_only_outputs:
            return outputs
        else:
            return {**inputs, **outputs}