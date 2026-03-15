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
from hart_intelligence_entry import CustomConvoOutputParser, CustomChain

with open("config.json", 'r') as f:
    config = json.load(f)

app = Flask(__name__)


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


user_id = 96
recognized_intent = []
req_total_tokens = 0
res_total_tokens = 0
encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")

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
        app.logger.info(f'calling for {self.count} times')

        app.logger.info(f"len---->{len(prompt.split(' '))}")
        #encoding = tiktoken.get_encoding("gpt-3.5-turbo")
        num_tokens = len(encoding.encode(prompt))
        req_total_tokens += num_tokens
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
        global recognized_intent
        try:
            intents = json.loads(response.json()["text"])
            curr_intent = intents["action"]
            if self.previous_intent == curr_intent:
                self.call_gpt4 = 1
            self.previous_intent = curr_intent
            recognized_intent.append(intents["action"])
        except:
            recognized_intent=["Final Answer"]
        # time.sleep(10)

        end_time = time.time()
        elapsed_time = end_time - start_time
        app.logger.info(f"time taken for this call is {elapsed_time}")
        num_tokens = len(encoding.encode(response.json()["text"].replace('\n', ' ').replace('\t', '')))
        res_total_tokens += num_tokens
        return response.json()["text"].replace('\n', ' ').replace('\t', '')

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {

        }


session_id = "user_"+str(user_id)
memory = ZepMemory(
    session_id=session_id,
    url=ZEP_API_URL,
    memory_key="chat_history",
    api_key=ZEP_API_KEY,
    return_messages=True,
    input_key="input"
)

llm = CustomGPT()

query= "hi"

parser = CustomConvoOutputParser()

prompt_template = "You are highly intelligent bot answer {input}"


llm_chain = CustomChain(llm=llm, prompt=PromptTemplate.from_template(prompt_template), memory=memory)

agent = ConversationalChatAgent(
    llm_chain=llm_chain,
    output_parser=parser
)

agent_chain = AgentExecutor.from_agent_and_tools(
    agent=agent,
    tools=[],
    verbose=True
)

ans = agent_chain.run(input=query)

print("Done")