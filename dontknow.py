from langchain.agents import ZeroShotAgent, Tool, AgentExecutor
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
from langchain.chains.conversation.memory import ConversationSummaryMemory


os.environ["OPENAI_API_KEY"] = "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"
os.environ["GOOGLE_CSE_ID"] = "9589161c491c4493e"
os.environ["GOOGLE_API_KEY"] = "AIzaSyCTEiyRiS8mfZlUp3Lc1JwmmyK4sZI_8Lo"
os.environ["NEWS_API_KEY"] = "291350f6b8fd4df982f343888a4cabd5"
os.environ["SERPAPI_API_KEY"] = "15916f6b8a0a976ab7f92ed1c4e3bc9bb40c73b40404ad2bbf219c5091394cb0"
search = GoogleSearchAPIWrapper(k=4)





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




template = """This is a conversation between a human and a bot:

{chat_history}

Write a summary of the conversation for {input}:
"""

prompt = PromptTemplate(input_variables=["input", "chat_history"], template=template)
memory = ConversationBufferMemory(memory_key="chat_history")
readonlymemory = ReadOnlySharedMemory(memory=memory)
summry_chain = LLMChain(
    llm=OpenAI(),
    prompt=prompt,
    verbose=True,
    memory=readonlymemory,  # use the read-only memory to prevent the tool from modifying the memory
)


llm_math = LLMMathChain(llm=OpenAI())

math_tool = Tool(
    name='Calculator',
    func=llm_math.run,
    description='Useful for when you need to answer questions about math.'
)

search = GoogleSearchAPIWrapper()
prompt2 = PromptTemplate(
    input_variables=["query"],
    template= "{query}"
)
llm_chain = LLMChain(llm=OpenAI(), prompt=prompt2,memory=ConversationSummaryMemory(llm=OpenAI()))

# initialize the LLM tool
llm_tool = Tool(
    name='Language Model',
    func=llm_chain.run,
    description='use this tool for general purpose queries and logic'
)


tools = [
    Tool(
        name="Search",
        func=search.run,
        description="useful for when you need to answer questions about current events",
    ),
    Tool(
        name="Summary",
        func=summry_chain.run,
        description="useful for when you summarize a conversation. The input to this tool should be a string, representing who will read this summary.",
    ),

]
tools.append(math_tool)
tools.append(llm_tool)
tools.append(PythonREPLTool())
class CustomOutputParser(AgentOutputParser):

    def parse(self, llm_output: str) -> Union[AgentAction, AgentFinish]:
        # Check if agent should finish
        if "Final Answer:" in llm_output:
            return AgentFinish(
                # Return values is generally always a dictionary with a single `output` key
                # It is not recommended to try anything else at the moment :)
                return_values={"output": llm_output.split("Final Answer:")[-1].strip()},
                log=llm_output,
            )
        # Parse out the action and action input
        regex = r"Action\s*\d*\s*:(.*?)\nAction\s*\d*\s*Input\s*\d*\s*:[\s]*(.*)"
        match = re.search(regex, llm_output, re.DOTALL)
        if not match:
            return AgentFinish(
                # Return values is generally always a dictionary with a single `output` key
                # It is not recommended to try anything else at the moment :)
                return_values={"output": llm_output.split("Final Answer:")[-1].strip()},
                log=llm_output,
            )
        action = match.group(1).strip()
        action_input = match.group(2)
        # Return the action and action input
        return AgentAction(tool=action, tool_input=action_input.strip(" ").strip('"'), log=llm_output)


output_parser = CustomOutputParser()


def get_ans(user_id, query):
    user_details, actions = get_action_user_details(user_id=user_id)

    prefix = f"""Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

        Instructions:
        You will have to act like the world's best teacher who has knowledge in every field, and you will have to think of the consequences of the particular response you will give.
        Your response should be meaningful, should not exceed more than 200 words, and should be as fast as possible.
        You are a highly knowledgeable teacher with a vast amount of information at your disposal.
        You also have access to a tool similar to Google Search that allows you to retrieve information from the web in real-time.
        As a teacher, your goal is to assist students by answering their questions and providing accurate and up-to-date information.
        The aim is to maintain a natural and conversational tone throughout the interaction. When providing responses, make sure to address the user by their name only if there is a necessity.
        When generating responses, prioritize delivering helpful information while using the user's name sparingly to enhance personalization when appropriate.

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

    {chat_history}
    Question: {input}
    {agent_scratchpad}"""

    prompt = ZeroShotAgent.create_prompt(
        tools,
        prefix=prefix,
        suffix=suffix,
        input_variables=["input", "chat_history", "agent_scratchpad"],
    )

    llm_chain = LLMChain(llm=OpenAI(temperature=0), prompt=prompt)
    agent = ZeroShotAgent(llm_chain=llm_chain, tools=tools, verbose=True, output_parser=output_parser)
    agent_chain = AgentExecutor.from_agent_and_tools(
        agent=agent, tools=tools, verbose=True, memory=memory
    )

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
