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
from langchain.tools import StructuredTool
from datetime import datetime, timezone
from langchain.llms.base import LLM
from typing import Optional, List, Mapping, Any
from langchain.agents import load_tools
from langchain.agents import initialize_agent
from langchain.agents import AgentType
import requests
import pytz
from langchain.experimental.plan_and_execute import PlanAndExecute, load_agent_executor, load_chat_planner


class CustomGPT(LLM):
    @property
    def _llm_type(self) -> str:
        return "custom"

    def _call(self, prompt: str, stop: Optional[List[str]] = None) -> str:
        response = requests.post(
            "http://aws_rasa.hertzai.com:5459/gpt-4",
            json={
              "model": "gpt-3.5-turbo-16k",
              "data": [{"role":"user","content":prompt}]
            }
        )
        response.raise_for_status()
        return response.json()["text"]

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {

        }




logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


os.environ["OPENAI_API_KEY"] = "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"
os.environ["GOOGLE_CSE_ID"] = "9589161c491c4493e"
os.environ["GOOGLE_API_KEY"] = "AIzaSyCTEiyRiS8mfZlUp3Lc1JwmmyK4sZI_8Lo"
os.environ["NEWS_API_KEY"] = "291350f6b8fd4df982f343888a4cabd5"
os.environ["SERPAPI_API_KEY"] = "15916f6b8a0a976ab7f92ed1c4e3bc9bb40c73b40404ad2bbf219c5091394cb0"
search = GoogleSearchAPIWrapper(k=4)

ZEP_API_URL = "http://4.224.46.164:8000"





def get_action_user_details(user_id):


    action_url = f"http://aws_hevolve.hertzai.com:6006/action_by_user_id?user_id={user_id}"

    payload = {}
    headers = {}

    response = requests.request(
        "GET", action_url, headers=headers, data=payload)

    unwanted_actions=['Casual Conversation', 'Topic confirmation', 'Topic not found', 'Topic confirmation', 'Topic listing', 'Probe', 'Question Answering', 'Fallback']
    data = response.json()
    action_texts = [obj["action"] for obj in data if obj["action"] not in unwanted_actions]
    if len(action_texts)==0:
        action_texts=['user has not performed any actions yet']
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
llm = ChatOpenAI(temperature=0, model='gpt-3.5-turbo')





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

spec2 = OpenAPISpec.from_file(
    "./openapi2.yaml"
)

chain = get_openapi_chain(spec)

#chain2 = get_openapi_chain(spec2)


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



def get_time_based_history(prompt:str, session_id:str, start_date:str, end_date:str):
    ZEP_API_URL = "http://4.224.46.164:8000"
    # print(type(start_date))

    memory = ZepMemory(
        session_id=session_id,
        url=ZEP_API_URL,
        memory_key="chat_history",
    )


    # messages = [message.message["content"] for message in messages if message.dist>0.8 and message.message["role"]!="system" and message.message["role"]!="ai"]
    try:
        messages = memory.chat_memory.search(prompt)

        print("messages----->", messages)

        filtered_messages = [[message.message['content'] for message in messages if message.message["role"]!="system" and datetime.fromisoformat(start_date.replace('Z', '+00:00')).replace(tzinfo=timezone.utc) <= datetime.fromisoformat(message.message['created_at'].replace('Z', '+00:00')).replace(tzinfo=timezone.utc) <= datetime.fromisoformat(end_date.replace('Z', '+00:00')).replace(tzinfo=timezone.utc) and message.dist>0.8 ]]
        #filtered_messages = [message.message['content'] for message in messages if message.message["role"] != "system" and
        #                 datetime.strptime(start_date, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc) <=
        #                 datetime.strptime(message.message['created_at'], '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc) <=
        #                 datetime.strptime(end_date, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc) and
        #                 message.dist > 0.8 ]
        #print("filter_messages ----->",filtered_messages)
        final_res = {'res':filter_messages}
        
        return json.dumps(final_res)
    except:
        #return [message.message['content'] for message in messages]
        return json.dumps({'res':memory.chat_memory.zep_summary})


def parsing_string(string):
    prompt, session_id, start_date, end_date = [s.strip() for s in string.split(",")]
    return get_time_based_history(prompt, session_id, start_date, end_date)


#def parse_date_time():



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
            name='Language Model',
            func=llm_chain.run,
            description= 'Useful when you need to answer from internal knowledge of LLM'
        ),
        Tool(
            name='Calculator',
            func=llm_math.run,
            description='Useful for when you need to answer questions about math.'
        ),
        Tool(
            name="OpenAPI_Specification",
            func=chain.run,
            description=f"Use this when you need to search infomation from one of our api's that are available, use in this scenarious when user asking image generation using text , information students, available book, ."
        ),
        Tool(
            name="Search",
            func=search.run,
            description="useful for when you need to answer questions about current events, current dates, weather.",
        ),
        Tool(
            name="Historical Conversations",
            func=parsing_string,
            description=f"""Use this tool if and only if the information requested is from prior to today regarding current user. The input required by this tool is a comma-separated list.
            The list should include a prompt generated from user input text, a session_id is user_{user_id}, a start_date, and an end_date.
            The start_date refers to the date from which the user information search begins and should always be in the ISO 8601 format. The end_date, also in the ISO 8601 format, represents the date at which the search ends.
            If you can't determine the end_date, use the current datetime time.
            For instance, if you want to search for a user's conversation from yesterday till today (assuming today's date is 2023-07-13T10:19:56.732291Z), your input would be 'what zep can do, user_123, 2023-07-12T10:19:56.732291Z, 2023-07-13T10:19:56.732291Z'."""
        )
    ]
    #tools.append(PythonREPLTool())

    today_date = datetime.now(pytz.timezone('Asia/Kolkata'))

    prefix = f"""Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

        Instructions:
        You will have to act like the world's best teacher who has knowledge in every field, and you will have to think of the consequences of the particular response you will give.
        Your response should be meaningful and should be as fast as possible.
        You are a highly knowledgeable teacher with a vast amount of information at your disposal.
        You also have access to a tool similar to Google Search that allows you to retrieve information from the web in real-time.
        As a teacher, your goal is to assist students by answering their questions and providing accurate and up-to-date information.
        Create a personality for yourself and don;t ever refer to user as human and yourself as just AI.

        {user_details}

        Things to consider before you respond:
        Context in which you are used:
        You are Hevolve, a highly intelligent educational AI, developed by HertzAI, designed to answer questions, provide revisions, assessments,
        teach various topics and help with research for students and working professionals from various knowledge sources like books, websites, white papers.
        Your responses will be played to the user as a video using an avatar and text to speech in various languages.

        These are all the actions performed by user till now:
        {actions}

        Today's date time is
        {today_date}

        Last 20 conversations or today's dialogue with the user along with timestamp of the conversations (recent_history except current dialogue) is:
	Always return json response
        """
    suffix = """
        Only if this above history is not sufficient to fulfill the user's request then use tools.

        TOOLS

        ------

        Assistant can use tools to look up information that may be helpful in answering the user's original question. The tools you can use are:


        {{tools}}

        {format_instructions}
	always create parsable output
        USER'S INPUT
        --------------------
        Here is the user's input (remember to respond with a markdown code snippet of a json blob with a single action, and NOTHING else):

        {{{{input}}}}"""


    prompt = ConversationalChatAgent.create_prompt(
        tools
    )

    #planner Agent
    # model = ChatOpenAI(temperature=0)
    # planner = load_chat_planner(model)
    # executor = load_agent_executor(model, tools, verbose=True)
    # agent = PlanAndExecute(planner=planner, executor=executor, verbose=True)
    # ans = agent.run(query)
    
    #chat Agent
    llm_chain_2 = LLMChain(llm=ChatOpenAI(model_name="gpt-3.5-turbo", temperature=0.7), prompt=prompt)


    agent = ConversationalChatAgent(llm_chain=llm_chain_2, tools=tools, verbose=True, return_intermediate_steps=False)
    agent_chain = AgentExecutor.from_agent_and_tools(
        agent=agent, tools=tools, verbose=True, memory=memory
    )
    ans = agent_chain.run(input=query)


    # agent_kwargs={"system_message":prefix, "human_message":suffix ,"input_variables":["input", "chat_history", "agent_scratchpad"] }

    # agent_chain = initialize_agent(tools, CustomGPT(), agent=AgentType.CHAT_CONVERSATIONAL_REACT_DESCRIPTION, verbose=True,  memory=memory)
    # ans = agent_chain.run(input=query)



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
