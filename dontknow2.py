from langchain.agents import ZeroShotAgent, Tool, AgentExecutor, ConversationalAgent, ConversationalChatAgent
from langchain.memory import ConversationBufferMemory, ReadOnlySharedMemory
from langchain import OpenAI, LLMChain, PromptTemplate
from langchain.utilities import GoogleSearchAPIWrapper
import requests
import json
from flask import Flask, jsonify, request
import os
from langchain.utilities import GoogleSearchAPIWrapper
import re
from langchain.agents import Tool, AgentExecutor, LLMSingleActionAgent, AgentOutputParser
from typing import List, Union
from langchain.schema import AgentAction, AgentFinish, OutputParserException
from langchain.chains import LLMMathChain
from langchain.tools.python.tool import PythonREPLTool
from langchain.chains.conversation.memory import ConversationSummaryMemory, ConversationBufferWindowMemory
from langchain.tools import OpenAPISpec, APIOperation
from langchain.chains import OpenAPIEndpointChain
from langchain.requests import Requests
from langchain.llms import OpenAI, OpenAIChat
from langchain.chains.openai_functions.openapi import get_openapi_chain
import logging
from langchain.chat_models import ChatOpenAI
from typing import Any
from langchain.agents import initialize_agent
from langchain.agents import AgentType
from langchain.memory import ZepMemory
from langchain.schema import HumanMessage, AIMessage, SystemMessage
from langchain.agents.conversational_chat.output_parser import ConvoOutputParser





logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


os.environ["OPENAI_API_KEY"] = "***REMOVED***"
os.environ["GOOGLE_CSE_ID"] = "9589161c491c4493e"
os.environ["GOOGLE_API_KEY"] = "***REMOVED***"
os.environ["NEWS_API_KEY"] = "***REMOVED***"
os.environ["SERPAPI_API_KEY"] = "***REMOVED***"
search = GoogleSearchAPIWrapper(k=4)

ZEP_API_URL = "http://4.224.46.164:8000"





def get_action_user_details(user_id):


    action_url = f"http://aws_hevolve.hertzai.com:6006/action_by_user_id?user_id={user_id}"

    payload = {}
    headers = {}

    response = requests.request(
        "GET", action_url, headers=headers, data=payload)

    data = response.json()
    action_texts = [obj["action"] for obj in data]
    actions = ", ".join(action_texts)

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
    user_name: {user_data["name"]} (Call the user by this name),gender: {user_data["gender"]},who_pays_for_course: {user_data["who_pays_for_course"]}(Entity Responsible for Paying the Course Fees),preferred_language: {user_data["preferred_language"]}(User's Preferred Language),date_of_birth: {user_data["dob"]},english_proficiency: {user_data["english_proficiency"]}(User's English Proficiency Level),created_date: {user_data["created_date"]}(user creation date),standard: {user_data["standard"]}(User's Standard in which user studying)
   '''
    return user_details, actions


def get_memory(user_id:int):
    session_id = "user_"+str(user_id)
    memory = ZepMemory(
        session_id=session_id,
        url=ZEP_API_URL,
        memory_key="chat_history",
        return_messages=True
    )
    return memory



template = """This is a conversation between a human and a bot:

{chat_history}

Write a summary of the conversation for {input}:
"""
llm = OpenAI()





llm_math = LLMMathChain(llm=llm)

search = GoogleSearchAPIWrapper()
prompt2 = PromptTemplate(
    input_variables=["input"],
    template= "{input}"
)
llm_chain = LLMChain(llm=ChatOpenAI(temperature=0, model='gpt-3.5-turbo'), prompt=prompt2)

# openapi spec chain
spec = OpenAPISpec.from_file(
"./openapi.yaml"
)

chain = get_openapi_chain(spec)


class CustomOutputParser(AgentOutputParser):
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
            raise OutputParserException(f"Could not parse LLM output: {text}") from e

    @property
    def _type(self) -> str:
        return "conversational_chat"

output_parser = CustomOutputParser()






def get_ans(user_id, query):
    user_details, actions = get_action_user_details(user_id=user_id)

    prompt = PromptTemplate(input_variables=["input", "chat_history"], template=template)
    memory=get_memory(user_id=user_id)
    readonlymemory = ReadOnlySharedMemory(memory=memory)
    summry_chain = LLMChain(
        llm=OpenAI(),
        prompt=prompt,
        verbose=True,
        memory=readonlymemory,  # use the read-only memory to prevent the tool from modifying the memory
    )

    tools = [
        Tool(
            name='Calculator',
            func=llm_math.run,
            description='Useful for when you need to answer questions about math.'
        ),
        Tool(
            name="OpenAPI_Specification",
            func=chain.run,
            description="Use this when you need to search infomation from one of our api's that are available, use in this scenarious when user asking for image from text , get information students, available book, user details, list of topics available."
        ),
        Tool(
            name="Search",
            func=search.run,
            description="useful for when you need to answer questions about current events, current dates, weather.",
        ),
        
    ]
    tools.append(PythonREPLTool())

    prefix = f"""Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

        Instructions:
        You will have to act like the world's best teacher who has knowledge in every field, and you will have to think of the consequences of the particular response you will give.
        Your response should be meaningful and should be as fast as possible.
        You are a highly knowledgeable teacher with a vast amount of information at your disposal.
        You also have access to a tool similar to Google Search that allows you to retrieve information from the web in real-time.
        As a teacher, your goal is to assist students by answering their questions and providing accurate and up-to-date information.


        {user_details}

        Things to consider before you respond:
        Context in which you are used:
        You are Hevolve, a highly intelligent educational AI, developed by HertzAI, designed to answer questions, provide revisions, assessments,
        teach various topics and help with research for students and working professionals from various knowledge sources like books, websites, white papers.
        Your responses will be played to the user as a video using an avatar and text to speech in various languages.

        This all are action performed by user till date
        {actions}
        You have access to the following tools:"""
    suffix = """Begin!"
    Relevant pieces of previous conversation. Must use if user is asking about his previous conversations:
    {chat_history}
    (You do not need to use these pieces of information if not relevant)
    Question: {input}
    {agent_scratchpad}"""

    prompt = ConversationalAgent.create_prompt(
        tools,
        prefix=prefix,
        suffix=suffix,
        input_variables=["input", "chat_history", "agent_scratchpad"],
    )

    #llm_chain_2 = LLMChain(llm=ChatOpenAI(temperature=0, model="gpt-3.5-turbo-0613"), prompt=prompt)
    #agent = ConversationalChatAgent(llm_chain=llm_chain_2, tools=tools, verbose=True, return_intermediate_steps=False)
    #agent_chain = AgentExecutor.from_agent_and_tools(
    #     agent=agent, tools=tools, verbose=True, memory=memory
    #)

    agent_kwargs={"system_message":prefix, "input_variables":["input", "chat_history", "agent_scratchpad"]}

    agent_chain = initialize_agent(tools, ChatOpenAI(), agent=AgentType.CHAT_CONVERSATIONAL_REACT_DESCRIPTION, verbose=True, memory=memory, agent_kwargs=agent_kwargs)
    #llm = ChatOpenAI(temperature=0, model="gpt-3.5-turbo-0613")
    #agent_chain = initialize_agent(tools, llm, agent=AgentType.OPENAI_FUNCTIONS, verbose=True, memory=memory)
    ans = agent_chain.run(input=query)
    return ans

app = Flask(__name__)


@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()

    user_id = data.get('user_id', None)

    prompt = data.get('prompt', None)
    ans = get_ans(user_id=user_id, query=prompt)

    return jsonify({'response': ans})


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
