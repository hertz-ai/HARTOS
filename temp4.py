from langchain.prompts import StringPromptTemplate
from langchain.memory import VectorStoreRetrieverMemory
from langchain.agents import ConversationalAgent, ZeroShotAgent
from langchain.output_parsers import StructuredOutputParser, ResponseSchema, PydanticOutputParser
from langchain.prompts import PromptTemplate, ChatPromptTemplate, HumanMessagePromptTemplate
from langchain.llms import OpenAI
from langchain.chat_models import ChatOpenAI
from pydantic import BaseModel, Field, validator
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.vectorstores import Chroma
from langchain.embeddings import OpenAIEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter, CharacterTextSplitter, TextSplitter
from langchain.llms import OpenAI
from langchain.chains import VectorDBQA
from langchain.document_loaders import TextLoader
from langchain.chains.conversation.memory import ConversationBufferMemory, ConversationBufferWindowMemory
from langchain.chains import RetrievalQA
from langchain.agents import load_tools
from langchain.agents import initialize_agent
from langchain.agents import AgentType
from langchain.utilities import GoogleSearchAPIWrapper
from langchain.utilities import WikipediaAPIWrapper
from langchain.agents import Tool, AgentExecutor, LLMSingleActionAgent, AgentOutputParser
from langchain.prompts import StringPromptTemplate
from langchain import OpenAI, SerpAPIWrapper, LLMChain
from typing import List, Union, Any, Optional, Type
from langchain.schema import AgentAction, AgentFinish
import re
from langchain import PromptTemplate
from langchain.tools import BaseTool
from langchain.callbacks.manager import AsyncCallbackManagerForToolRun, CallbackManagerForToolRun
from langchain.utilities import GoogleSerperAPIWrapper
from langchain import LLMMathChain, OpenAI, SerpAPIWrapper, SQLDatabase, SQLDatabaseChain
from langchain.agents import initialize_agent, Tool
from langchain.agents import AgentType
from langchain.chat_models import ChatOpenAI
from langchain import OpenAI
from langchain.chains import ConversationChain
from langchain.memory import VectorStoreRetrieverMemory
import os
from langchain.utilities import GoogleSerperAPIWrapper
from langchain.utilities import GoogleSearchAPIWrapper
from langchain.agents import Agent
import requests
import json
from langchain.agents import Tool
from langchain.prompts import StringPromptTemplate
from langchain.docstore.document import Document
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple, Type
from langchain.vectorstores.base import VectorStore
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.vectorstores import Chroma
from langchain.text_splitter import RecursiveCharacterTextSplitter, CharacterTextSplitter, TextSplitter
from langchain import OpenAI, VectorDBQA
from langchain.chains import RetrievalQA
from langchain.document_loaders import DirectoryLoader, TextLoader
from langchain.prompts import PromptTemplate
from langchain.chains.question_answering import load_qa_chain
from langchain.embeddings import HuggingFaceEmbeddings
from vicuna_config import VicunaLLM, CustomOutputParser
import sentence_transformers
import os
import nltk
import config
import logging
from pydantic import Field
from typing import Any, Optional, Dict
import chromadb
from chromadb.api.types import Documents, Embeddings
from uuid import uuid4
import pandas as pd
from langchain.tools import Tool
from langchain.utilities import GoogleSearchAPIWrapper
from flask import Flask, request, jsonify
from langchain.agents import initialize_agent
from langchain.chains.conversation.memory import ConversationBufferWindowMemory
from langchain.agents import load_tools
from langchain.agents import initialize_agent
from langchain.agents import AgentType
from langchain.utilities import GoogleSearchAPIWrapper
from langchain.utilities import WikipediaAPIWrapper
from langchain.agents import Tool, AgentExecutor, LLMSingleActionAgent, AgentOutputParser
from langchain.prompts import StringPromptTemplate
from langchain import OpenAI, SerpAPIWrapper, LLMChain
from typing import List, Union, Any, Optional, Type
from langchain.schema import AgentAction, AgentFinish
import re
from langchain import PromptTemplate
from langchain.tools import BaseTool
from langchain.callbacks.manager import AsyncCallbackManagerForToolRun, CallbackManagerForToolRun
from langchain.utilities import GoogleSerperAPIWrapper
from langchain.memory import VectorStoreRetrieverMemory
from langchain import LLMMathChain
from langchain.chat_models import ChatOpenAI
# from llm_client import AlpacaLLM

# os variables
os.environ["OPENAI_API_KEY"] = "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"
os.environ["GOOGLE_CSE_ID"] = "9589161c491c4493e"
os.environ["GOOGLE_API_KEY"] = "AIzaSyCTEiyRiS8mfZlUp3Lc1JwmmyK4sZI_8Lo"
os.environ["NEWS_API_KEY"] = "291350f6b8fd4df982f343888a4cabd5"
os.environ["SERPAPI_API_KEY"] = "15916f6b8a0a976ab7f92ed1c4e3bc9bb40c73b40404ad2bbf219c5091394cb0"

#global variables
eb = HuggingFaceEmbeddings()
news_api_key = os.environ["NEWS_API_KEY"]
TOOLS_LIST = ["llm-math"]
MAX_TOKENS = 512
llm = ChatOpenAI(temperature=0, max_tokens=MAX_TOKENS,
                 model_name="gpt-3.5-turbo")
search1 = GoogleSearchAPIWrapper(k=4)
search = GoogleSerperAPIWrapper(
    serper_api_key="15916f6b8a0a976ab7f92ed1c4e3bc9bb40c73b40404ad2bbf219c5091394cb0")

# Set up the base template
template = """Answer the following questions as best you can, but speaking as a pirate might speak. You have access to the following tools:

{tools}

Use this user detail when user asks for his personal information
{user_details}

This are all action that user have taken in his learning journey. Don't treat it as history
{actions}

Previous conversation history, Use history when u need to answer question from previous conversation or user asking about previous conversation history:
{chat_history}
Use the following format:


Question: the input question you must answer
Thought: you should always think about what to do
Action: the action to take, should be one of [{tool_names}]
Action Input: the input to the action
Observation: the result of the action
... (this Thought/Action/Action Input/Observation can repeat N times)
Thought: I now know the final answer
Final Answer: the final answer to the original input question

Begin! Remember to speak as a pirate when giving your final answer. Use lots of "Arg"s

Question: {input}
{agent_scratchpad}
"""


def _results_to_docs(results: Any) -> List[Document]:
    return [doc for doc, _ in _results_to_docs_and_scores(results)]


def _results_to_docs_and_scores(results: Any) -> List[Tuple[Document, float]]:
    list_of_doc = []
    for doc, meta, distance in zip(results["documents"][0], results["metadatas"][0], results["distances"][0]):
        if distance <= 0.5:
            list_of_doc.append(
                (Document(page_content=doc, metadata=meta or {}), distance))
    return list_of_doc


class Chroma(VectorStore):
    def __init__(self, embedding_function):
        model_name: str = 'sentence-transformers/all-mpnet-base-v2'
        """Model name to use."""
        cache_folder: Optional[str] = None
        """Path to store models.
    Can be also set by SENTENCE_TRANSFORMERS_HOME environment variable."""
        model_kwargs: Dict[str, Any] = {'device': 'cuda'}
        """Key word arguments to pass to the model."""
        encode_kwargs: Dict[str, Any] = Field(default_factory=dict)
        """Key word arguments to pass when calling the `encode` method of the model."""

        self.client = sentence_transformers.SentenceTransformer(
            model_name, cache_folder=cache_folder, **model_kwargs
        )

        self.client_settings = chromadb.config.Settings(
            chroma_db_impl="duckdb+parquet",
            persist_directory='./temp',
        )
        self.chroma_client = chromadb.Client(self.client_settings)
        self._embedding_function = embedding_function
        self._collection = None

    _LANGCHAIN_DEFAULT_COLLECTION_NAME = "langchain"

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[List[dict]] = None,
        ids: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> List[str]:
        """Run more texts through the embeddings and add to the vectorstore.

        Args:
            texts (Iterable[str]): Texts to add to the vectorstore.
            metadatas (Optional[List[dict]], optional): Optional list of metadatas.
            ids (Optional[List[str]], optional): Optional list of IDs.

        Returns:
            List[str]: List of IDs of the added texts.
        """
        # TODO: Handle the case where the user doesn't provide ids on the Collection
        # if ids is None:
        # TODO: Handle the case where the user doesn't provide ids on the Collection
        if ids is None:
            ids = [str(uuid4()) for _ in texts]
        embeddings = None
        if self._embedding_function is not None:
            embeddings = self._embedding_function.embed_documents(list(texts))
        self._collection.add(
            metadatas=metadatas, embeddings=embeddings, documents=texts, ids=ids
        )
        return ids

    def similarity_search(
        self,
        query: str,
        k: int = 10,
        filter: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> List[Document]:
        """Run similarity search with Chroma.

        Args:
            query (str): Query text to search for.
            k (int): Number of results to return. Defaults to 4.
            filter (Optional[Dict[str, str]]): Filter by metadata. Defaults to None.

        Returns:
            List[Document]: List of documents most similar to the query text.
        """
        print("kwargs -------> ", kwargs)
        docs_and_scores = self.similarity_search_with_score(
            query, k, filter=kwargs['metadatas'], search_kwargs={'collection_name': kwargs['collection_name']})
        print("hi im here", docs_and_scores)
        return [doc for doc in docs_and_scores]

    def similarity_search_with_score(
        self,
        query: str,
        k: int = 4,
        filter: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> List[Tuple[Document, float]]:
        """Run similarity search with Chroma with distance.

        Args:
            query (str): Query text to search for.
            k (int): Number of results to return. Defaults to 4.
            filter (Optional[Dict[str, str]]): Filter by metadata. Defaults to None.

        Returns:
            List    [Tuple[Document, float]]: List of documents most similar to the query
                text with distance in float.
        """
        print("kwargs in search_similarity----> ", kwargs)

        collection = self.chroma_client.get_collection(
            name=kwargs['search_kwargs']['collection_name'])
        count = collection.count()
        print("count in collection --->", count)

        query_embeddings = self._embedding_function.embed_query(query)
        passage = collection.query(
            query_embeddings=[query_embeddings], where=filter, n_results=min(5, count))
        print("Hi I am passage", passage)
        x = _results_to_docs(passage)
        print(x)
        return x

    @classmethod
    def from_texts(
        cls: Type[Chroma],
        texts: List[str],
        embedding: Optional[Embeddings] = None,
        metadatas: Optional[List[dict]] = None,
        ids: Optional[List[str]] = None,
        collection_name: str = _LANGCHAIN_DEFAULT_COLLECTION_NAME,
        persist_directory: Optional[str] = None,
        client_settings: Optional[chromadb.config.Settings] = None,
        client: Optional[chromadb.Client] = None,
        **kwargs: Any,
    ) -> Chroma:
        """Create a Chroma vectorstore from a raw documents.

        If a persist_directory is specified, the collection will be persisted there.
        Otherwise, the data will be ephemeral in-memory.

        Args:
            texts (List[str]): List of texts to add to the collection.
            collection_name (str): Name of the collection to create.
            persist_directory (Optional[str]): Directory to persist the collection.
            embedding (Optional[Embeddings]): Embedding function. Defaults to None.
            metadatas (Optional[List[dict]]): List of metadatas. Defaults to None.
            ids (Optional[List[str]]): List of document IDs. Defaults to None.
            client_settings (Optional[chromadb.config.Settings]): Chroma client settings

        Returns:
            Chroma: Chroma vectorstore.
        """
        chroma_collection = cls(
            collection_name=collection_name,
            embedding_function=embedding,
            persist_directory=persist_directory,
            client_settings=client_settings,
            client=client,
        )
        chroma_collection.add_texts(texts=texts, metadatas=metadatas, ids=ids)
        return chroma_collection

    def embed_function(self, texts: Documents) -> Embeddings:

        texts = list(map(lambda x: x.replace("\n", " "), texts))
        embeddings = self._embedding_function.embed_documents(texts)
        return embeddings.tolist()

    def create_chroma_db(self, name):
        db = self.chroma_client.create_collection(
            name=name, embedding_function=self.embed_function)
        return db

    def store_embedding(self, documents, db, metas={}):
        if self._embedding_function is not None:
            embeddings = self._embedding_function.embed_documents(documents)
        print("embedding leng", len(embeddings[0]))
        for i, d in enumerate(documents):
            db.add(
                documents=d,
                embeddings=embeddings[i],
                ids=str(uuid4()),
                metadatas=metas
            )

    def get_relevant_passage(self, query, db):
        passage = db.query(query_texts=[query], n_results=1, where={
                           'user_id': 1, 'conv_id': 1})['documents'][0][0]
        return passage

    def show_database(self, db):
        return pd.DataFrame(db.peek(10))


# Set up a prompt template



def get_actions_user_detail(user_id: int):
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


class CustomOutputParser(AgentOutputParser):

    def parse(self, llm_output: str) -> Union[AgentAction, AgentFinish]:
        # Check if agent should finish
        if "Final Answer:" in llm_output:
            return AgentFinish(
                # Return values is generally always a dictionary with a single `output` key
                # It is not recommended to try anything else at the moment :)
                return_values={"output": llm_output.split(
                    "Final Answer:")[-1].strip()},
                log=llm_output,
            )
        # Parse out the action and action input
        regex = r"Action\s*\d*\s*:(.*?)\nAction\s*\d*\s*Input\s*\d*\s*:[\s]*(.*)"
        match = re.search(regex, llm_output, re.DOTALL)
        if not match:
            return AgentFinish(
                # Return values is generally always a dictionary with a single `output` key
                # It is not recommended to try anything else at the moment :)
                return_values={"output": llm_output.split(
                    "Final Answer:")[-1].strip()},
                log=llm_output,
            )
        action = match.group(1).strip()
        action_input = match.group(2)
        # Return the action and action input
        return AgentAction(tool=action, tool_input=action_input.strip(" ").strip('"'), log=llm_output)





# chroma instance
db = Chroma(embedding_function=eb)


def answer(question: str, user_id: int, conv_id: int, first_req: bool = False, list_of_document: list = [], persist_directory: str = config.PERSIST_DIR) -> str:

    collection_name = 'user{}_conv{}'.format(user_id, conv_id)

    metas = {'user_id': user_id, 'conv_id': conv_id}
    try:
        database = db.chroma_client.get_collection(name=collection_name)
        print(database)
    except:
        database = None
    if database == None:
        database = db.create_chroma_db(name=collection_name)
    else:
        database = db.chroma_client.get_collection(name=collection_name)

    if first_req:
        db.store_embedding(list_of_document, database, metas=metas)

    db._collection = database

    # print("hello",db.show_database(database))

    qa = RetrievalQA.from_chain_type(
        llm=llm, chain_type="stuff",
        retriever=db.as_retriever(
            search_kwargs={"score_threshold": 1,
                           "metadatas": metas, "collection_name": collection_name}
        )
    )


    tools = load_tools(TOOLS_LIST, llm=llm, news_api_key=news_api_key)

    tool = [

        Tool(
            name='Knowledge Base',
            func=qa.run,
            description=(
                " Useful for when you need to answer question based on previous chat history between you and human. Extract history from this tool and answer "
            )
        ),

        Tool(
            name="Google Search",
            description="Search Google for recent results, current events.",
            func=search1.run,
        ),
        Tool(
            name="current events",
            func=search1.run,
            description="useful for when you need to ask with search",
        )
    ]
    tools += tool

    user_details, actions = get_actions_user_detail(user_id=user_id)
    class CustomPromptTemplate(StringPromptTemplate):
        # The template to use
        template: str
        # The list of tools available
        tools: List[Tool]
        actions: str
        user_details: str

        def format(self, **kwargs) -> str:
            # Get the intermediate steps (AgentAction, Observation tuples)
            # Format them in a particular way
            intermediate_steps = kwargs.pop("intermediate_steps")
            thoughts = ""
            for action, observation in intermediate_steps:
                thoughts += action.log
                thoughts += f"\nObservation: {observation}\nThought: "
            # Set the agent_scratchpad variable to that value
            kwargs["agent_scratchpad"] = thoughts
            # Create a tools variable from the list of tools provided
            kwargs["tools"] = "\n".join(
                [f"{tool.name}: {tool.description}" for tool in self.tools])
            # Create a list of tool names for the tools provided
            kwargs["tool_names"] = ", ".join([tool.name for tool in self.tools])
            kwargs["actions"] = actions
            kwargs['user_details'] = user_details
            return self.template.format(**kwargs)

    # history =
    prompt = CustomPromptTemplate(
        template=template,
        tools=tools,
        actions=actions,
        user_details=user_details,
        # This omits the `agent_scratchpad`, `tools`, and `tool_names` variables because those are generated dynamically
        # This includes the `intermediate_steps` variable because that is needed
        input_variables=["input", "intermediate_steps", "chat_history"]
    )



    output_parser = CustomOutputParser()


    # LLM chain consisting of the LLM and a prompt
    llm_chain = LLMChain(llm=llm, prompt=prompt)

    tool_names = [tool.name for tool in tools]
    agent = LLMSingleActionAgent(
        llm_chain=llm_chain,
        output_parser=output_parser,
        stop=["\nObservation:"],
        allowed_tools=tool_names
    )

    conversational_memory = ConversationBufferMemory(memory_key="chat_history")

    agent_executor = AgentExecutor.from_agent_and_tools(
        agent=agent, tools=tools, verbose=True, memory=conversational_memory)

    ans = agent_executor.run({'input': "question"})
    return ans


i = 0
global user_id
user_id = 0
global conv_id
conv_id = 0
global list_of_document
list_of_document = []

# flask app
app = Flask(__name__)


@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()

    user_id = data.get('user_id', None)
    conv_id = data.get('conv_id', None)
    list_of_document = list(data.get('conv_list', None))
    print(f'{list_of_document}-->{type(list_of_document)}')

    first_req_flag = data.get('first_req_flag', False)
    prompt = data.get('prompt', None)
    if first_req_flag:
        ans = answer(user_id=user_id, conv_id=conv_id,
                  list_of_document=list_of_document, first_req=first_req_flag, question=prompt)
    else:
        ans = answer(user_id=user_id, conv_id=conv_id, question=prompt)

    return jsonify({'response': ans})


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5050)
