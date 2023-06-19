from langchain.docstore.document import Document
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple, Type
from langchain.vectorstores.base import VectorStore
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.vectorstores import Chroma
from langchain.text_splitter import RecursiveCharacterTextSplitter, CharacterTextSplitter, TextSplitter
from langchain import OpenAI, VectorDBQA
from langchain.document_loaders import DirectoryLoader, TextLoader
from langchain.prompts import PromptTemplate
from langchain.chains.question_answering import load_qa_chain
from langchain.embeddings import HuggingFaceEmbeddings
from vicuna_config import VicunaLLM
from langchain.chains import RetrievalQA
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

'''
os.environ["GOOGLE_CSE_ID"] = "4690150357"
os.environ["GOOGLE_API_KEY"] = "AIzaSyB77VP_jzGuim9yzBk8-as5hJga9zYwv_Q"

search = GoogleSearchAPIWrapper(k=1)

tool = Tool(
        name = "Google Search",
        description="Search Google and return the first result",
        func=search.run
        )

'''

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
    for doc, meta, distance in zip(results["documents"][0],results["metadatas"][0],results["distances"][0]):
        if distance <= 0.5:
            list_of_doc.append((Document(page_content=doc, metadata=meta or {}), distance))
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
        #if ids is None:
        # TODO: Handle the case where the user doesn't provide ids on the Collection
        if ids is None:
            ids = [str(uuid.uuid1()) for _ in texts]
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
        print("kwargs -------> ",kwargs)
        docs_and_scores = self.similarity_search_with_score(
            query, k, filter=kwargs['metadatas'], search_kwargs={'collection_name':kwargs['collection_name']})
        print("hi im here",docs_and_scores)
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

        collection = self.chroma_client.get_collection(name=kwargs['search_kwargs']['collection_name'])
        count = collection.count()
        print("count in collection --->", count)

        query_embeddings = self._embedding_function.embed_query(query)
        passage = collection.query(query_embeddings=[query_embeddings], where=filter, n_results=min(5, count))
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
        print("embedding leng",len(embeddings[0]))
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


eb = HuggingFaceEmbeddings()
db = Chroma(embedding_function=eb)

# Define answer generation function
def answer(prompt: str, user_id: int, conv_id: int, first_req:bool = False, list_of_document: list = [], persist_directory: str = config.PERSIST_DIR) -> str:

    collection_name = 'user{}_conv{}'.format(user_id,conv_id)
    metas = {'user_id':user_id, 'conv_id':conv_id}
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

    # print("hello",db.show_database(database))
    LOGGER.info(f"Start answering based on prompt: {prompt}.")

    # Create a prompt template using a template from the config module and input variables
    # representing the context and question.
    prompt_template = PromptTemplate(
        template=config.prompt_template, input_variables=["context", "question"])

    # Load a QA chain using an OpenAI object, a chain type, and a prompt template.
    doc_chain = load_qa_chain(
        llm=VicunaLLM(),
        chain_type="stuff",
        prompt=prompt_template,
    )

    # Log a message indicating the number of chunks to be conside nred when answering the user's query.
    LOGGER.info(
        f"The top {config.k} chunks are considered to answer the user's query.")

    # Create a VectorDBQA object using a vector store, a QA chain, and a number of chunks to consider.
    qa = RetrievalQA.from_chain_type(llm=VicunaLLM(), chain_type="stuff", chain_type_kwargs={'prompt': prompt_template}, retriever=db.as_retriever(
        search_kwargs={"score_threshold": 1.2, "metadatas": metas, "collection_name": collection_name}))
    # Call the VectorDBQA object to generate an answer to the prompt.
    result = qa({"query": prompt})
    answer = result["result"]
    temp_list = [prompt, answer]
    db.store_embedding(temp_list, database, metas=metas)
    db.chroma_client.persist()

    # Log a message indicating the answer that was generated
    LOGGER.info(f"The returned answer is: {answer}")

    # Log a message indicating that the function has finished and return the answer.
    LOGGER.info(f"Answering module over.")
    return answer

i = 0
global user_id
user_id=0
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
        ans = answer(user_id=user_id,conv_id=conv_id, list_of_document=list_of_document,first_req=first_req_flag, prompt=prompt)
    else:
        ans = answer(user_id=user_id,conv_id=conv_id, prompt=prompt)
    
    return jsonify({'response':ans})


    # action_list = data.get('action_list', None)

@app.route('/saveaction', methods=['POST'])
def api():
    data = request.get_json()  # get data sent as JSON
    user_id = data.get('user_id', None)
    conv_id = data.get('conv_id', None)
    action = data.get('action', None)
    collection_name = 'user{}_conv{}'.format(user_id,conv_id)
    metas = {'user_id':user_id, 'conv_id':conv_id}
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

        return jsonify({'response':f'saved into database in {collection_name} collection'}), 200
    
    except Exception as e:
        print(f"Something went wrong. Error: {e}")
        return jsonify({'response':'Something went wrong'})

@app.route('/get_collection_data', methods=['POST'])
def getcollection():
    data = request.get_json()
    collection_name = data.get('collection_name',None)
    try:
        database = db.chroma_client.get_collection(name=collection_name)
        print(database)
    except:
        database = None

    if database == None:
        return jsonify({"response":"Collection NOT FOUND!"})
    else:
        database = db.chroma_client.get_collection(name=collection_name)
    collection_data = db.show_database(database)
    collection_data = collection_data.to_dict()
    return jsonify({'response':f"following are the top 10 records in collection {collection_name}\n data:{collection_data}"})





if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
