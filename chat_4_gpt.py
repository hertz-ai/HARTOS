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
# import os
os.environ["OPENAI_API_KEY"] = "***REMOVED***"
os.environ["GOOGLE_CSE_ID"] = "9589161c491c4493e"
os.environ["GOOGLE_API_KEY"] = "***REMOVED***"
os.environ["NEWS_API_KEY"] = "***REMOVED***"
os.environ["SERPAPI_API_KEY"] = "***REMOVED***"
search = GoogleSearchAPIWrapper(k=4)


# Initialize logging with the specified configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_FILE),
        logging.StreamHandler(),
    ],
)
LOGGER = logging.getLogger(__name__)

# client: Any  #: :meta private:
DEFAULT_K = 4  # Number of Documents to return.

# chroma class


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
        k: int = DEFAULT_K,
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


'''
list_of_document = ["Operating the Climate Control System  Your Googlecar has a climate control system that allows you to adjust the temperature and airflow in the car. To operate the climate control system, use the buttons and knobs located on the center console.  Temperature: The temperature knob controls the temperature inside the car. Turn the knob clockwise to increase the temperature or counterclockwise to decrease the temperature. Airflow: The airflow knob controls the amount of airflow inside the car. Turn the knob clockwise to increase the airflow or counterclockwise to decrease the airflow. Fan speed: The fan speed knob controls the speed of the fan. Turn the knob clockwise to increase the fan speed or counterclockwise to decrease the fan speed. Mode: The mode button allows you to select the desired mode. The available modes are: Auto: The car will automatically adjust the temperature and airflow to maintain a comfortable level. Cool: The car will blow cool air into the car. Heat: The car will blow warm air into the car. Defrost: The car will blow warm air onto the windshield to defrost it."
                    "Your Googlecar has a large touchscreen display that provides access to a variety of features, including navigation, entertainment, and climate control. To use the touchscreen display, simply touch the desired icon.  For example, you can touch the \"Navigation\" icon to get directions to your destination or touch the \"Music\" icon to play your favorite songs."
                    "Shifting Gears  Your Googlecar has an automatic transmission. To shift gears, simply move the shift lever to the desired position.  Park: This position is used when you are parked. The wheels are locked and the car cannot move. Reverse: This position is used to back up. Neutral: This position is used when you are stopped at a light or in traffic. The car is not in gear and will not move unless you press the gas pedal. Drive: This position is used to drive forward. Low: This position is used for driving in snow or other slippery conditions."]
'''

# embedder
eb = HuggingFaceEmbeddings()
# chroma instance
db = Chroma(embedding_function=eb)
from langchain.chat_models import ChatOpenAI

# defining LLM
llm = ChatOpenAI(temperature=0, max_tokens=512,
                 model_name="gpt-3.5-turbo")


template = """
Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

Instructions:
You will have to act like the world's best teacher who has knowledge in every field, and you will have to think of the consequences of the particular response you will give.
Your response should be meaningful, should not exceed more than 200 words, and should be as fast as possible.
You are a highly knowledgeable teacher with a vast amount of information at your disposal.
You also have access to a tool similar to Google Search that allows you to retrieve information from the web in real-time.
As a teacher, your goal is to assist students by answering their questions and providing accurate and up-to-date information.
The aim is to maintain a natural and conversational tone throughout the interaction. When providing responses, make sure to address the user by their name only if there is a necessity.
When generating responses, prioritize delivering helpful information while using the user's name sparingly to enhance personalization when appropriate.

this are user details:
{user_details}

Things to consider before you respond:
Context in which you are used:
You are Hevolve, a highly intelligent educational AI, developed by HertzAI, designed to answer questions, provide revisions, assessments,
teach various topics and help with research for students and working professionals from various knowledge sources like books, websites, white papers.
Your responses will be played to the user as a video using an avatar and text to speech in various languages.

You have access to the following tools:
{tools}

Use history to find relevant conversations for the current query.

Use actions to get what all actions the user has taken before, keep all these actions into account while answering the query. Also, it could be used as additional history.

Given the chat history so far, generate an engaging message to continue the conversation with the user. Incorporate interesting facts, jokes, or personalized references to previous discussions to keep the interaction lively and captivating. The main aim is to encourage the user to actively participate and explore further topics. Your message should be engaging, friendly, and foster a sense of curiosity.

Strictly use the following format:

Question: the input question you must answer
Thought: you should always think about what to do
Action: the action to take, should be one of [Google Search]
Action Input: the input to the action, should be a question.
Observation: the result of the action
... (this Thought/Action/Action Input/Observation can repeat N times)
Thought: I now know the final answer
Final Answer: the final answer to the original input question

Use the provided examples as a reference,
you should follow a similar format and structure when answering queries.
To perform actions like Google searches, retrieve relevant information,
and present the observations and final answers in a step-by-step manner. This approach helps provide thorough
and accurate responses to user queries, enhancing the overall conversational experience.
While generating responses, emphasize maintaining a logical flow and breaking down complex queries into manageable steps.

For examples:
Question: How old is the CEO of Microsoft's wife?
Thought: First, I need to find who is the CEO of Microsoft.
Action: Google Search
Action Input: Who is the CEO of Microsoft?
Observation: Satya Nadella is the CEO of Microsoft.
Thought: Now, I should find out Satya Nadella's wife.
Action: Google Search
Action Input: Who is Satya Nadella's wife?
Observation: Satya Nadella's wife's name is Anupama Nadella.
Thought: Then, I need to check Anupama Nadella's age.
Action: Google Search
Action Input: How old is Anupama Nadella?
Observation: Anupama Nadella's age is 50.
Thought: I now know the final answer.
Final Answer: Anupama Nadella is 50 years old.

Example 2:
Question: What was my last question to you?
Thought: First I need to check what all questions I have in the Knowledge Base.
Action: Knowledge Base
Action Input: What is the last question or query in the Knowledge Base.
Observation: Who is the current Prime Minister of India?
Thought: Now, this is the last question I found out from the Knowledge Base.
Final Answer: Your last question to me based on our previous conversation is: "Who is the current Prime Minister of India?"

Actions
{actions}

History
{history}

Input:
{input}

Response:
{agent_scratchpad}
"""


# Set up a prompt template
class CustomPromptTemplate(StringPromptTemplate):
    # The template to use
    template: str
    # The list of tools available
    tools: List[Tool]

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
        kwargs["tools"] = "\n".join([f"{tool.name}: {tool.description}" for tool in self.tools])
        # Create a list of tool names for the tools provided
        kwargs["tool_names"] = ", ".join([tool.name for tool in self.tools])
        return self.template.format(**kwargs)




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

# search = GoogleSearchAPIWrapper(k=1)


# Define answer generation function
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
    # else:
    #     database = db.chroma_client.get_collection(name=collection_name)

    if first_req:
        db.store_embedding(list_of_document, database, metas=metas)

    db._collection = database

    # print("hello",db.show_database(database))
    LOGGER.info(f"Start answering based on prompt: {question}.")

    # Create a prompt template using a template from the config module and input variables
    # representing the context and question.
    # prompt_template = PromptTemplate(
    #     template=config.prompt_template, input_variables=["context", "question"])

    # Log a message indicating the number of chunks to be conside nred when answering the user's query.
    LOGGER.info(
        f"The top {config.k} chunks are considered to answer the user's query.")

    # conversational memory
    #use constome search
    ret = db.as_retriever(
            search_kwargs={"metadatas": metas, "collection_name": collection_name})
    conversational_memory = VectorStoreRetrieverMemory(
        retriever=ret,
        memory_key='history',
        # k=5,
        input_key="input",
        # output_key='output',
        return_docs=False
    )

    # Create a RetrivalQA object using a vector store, a QA chain, and a number of chunks to consider.
    qa = RetrievalQA.from_chain_type(
        llm=llm, chain_type="stuff",
        retriever=ret
    )

    # Once we get chain we are ready to generate Agent for this we need to convert this retrieval chain into a tool. We do that like so:

    # use below code when you want to use chain as standalone
    # # Call the RetrivalQA object to generate an answer to the prompt.
    # result = qa({"query": prompt})

    news_api_key = os.environ["NEWS_API_KEY"]

    #TOOLS_LIST = ["llm-math"]

    #tools = load_tools(TOOLS_LIST, llm=llm, news_api_key=news_api_key)

    search1 = GoogleSearchAPIWrapper(k=4)
    # search = GoogleSerperAPIWrapper(serper_api_key="***REMOVED***")
    llm_math_chain = LLMMathChain.from_llm(llm=llm, verbose=True)
    tools = [

        Tool(
            name='Knowledge Base',
            func=qa.run,
            description=(
                " Useful for when you need to answer question based on previous chat history between you and human. Extract history from this tool and answer "
            )
        )
        ,

        Tool(
        name="Calculator",
        func=llm_math_chain.run,
        description="useful for when you need to answer questions about math or calculations"
        ),
        # python refl

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

    prompt = CustomPromptTemplate(
        template=template,
        tools=tools,
        # This omits the `agent_scratchpad`, `tools`, and `tool_names` variables because those are generated dynamically
        # This includes the `intermediate_steps` variable because that is needed
        input_variables=["input", "intermediate_steps","history","actions","user_details"]
    )


    output_parser = CustomOutputParser()

    llm_chain = LLMChain(llm=llm, prompt=prompt)

    agent = LLMSingleActionAgent(
        llm_chain=llm_chain,
        output_parser=output_parser,
        stop=["\nObservation:"],
        allowed_tools=tools,
    )

    agent_executor = AgentExecutor.from_agent_and_tools(
        agent=agent,
        tools=tools,
        verbose=True,
        memory=conversational_memory
    )

    # user action details

    import requests

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

    # Initializing agen
    answer = agent_executor(
            {'input': question, 'actions': actions, "user_details": user_details})


    # _input = prompt.format_prompt(query=question)
    # answer = agent(question.to_string())['output']
    print("ans-->",answer["output"])
    temp_list = [question, answer["output"]]
    conversational_memory.save_context({"input":question},{"output": answer["output"]})
    db.store_embedding(temp_list, database, metas=metas)
    db.chroma_client.persist()

    # Log a message indicating the answer that was generated
    LOGGER.info(f"The returned answer is: {answer}")

    # Log a message indicating that the function has finished and return the answer.
    LOGGER.info(f"Answering module over.")
    return answer


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

    # action_list = data.get('action_list', None)


@app.route('/saveaction', methods=['POST'])
def api():
    data = request.get_json()  # get data sent as JSON
    user_id = data.get('user_id', None)
    conv_id = data.get('conv_id', None)
    action = data.get('action', None)
    collection_name = 'user{}_conv{}'.format(user_id, conv_id)
    metas = {'user_id': user_id, 'conv_id': conv_id}
    try:
        try:
            database = db.chroma_client.get_collection(name=collection_name)
            print(database)
        except:
            database = None
        if database == None:
            database = db.create_chroma_db(name=collection_name)
        else:
            database = db.chroma_client.get_collection(name=collection_name)

        db.store_embedding([action], database, metas=metas)
        db.chroma_client.persist()
        return jsonify({'response': f'saved into database in {collection_name} collection'}), 200

    except Exception as e:
        print(f"Something went wrong. Error: {e}")
        return jsonify({'response': 'Something went wrong'})


@app.route('/get_collection_data', methods=['POST'])
def getcollection():
    data = request.get_json()
    collection_name = data.get('collection_name', None)
    try:
        database = db.chroma_client.get_collection(name=collection_name)
        print(database)
    except:
        database = None

    if database == None:
        return jsonify({"response": "Collection NOT FOUND!"})
    else:
        database = db.chroma_client.get_collection(name=collection_name)
    collection_data = db.show_database(database)
    collection_data = collection_data.to_dict()
    return jsonify({'response': f"following are the top 10 records in collection {collection_name}\n data:{collection_data}"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5050)
