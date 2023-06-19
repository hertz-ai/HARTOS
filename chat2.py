from langchain.chains.conversation.memory import ConversationBufferWindowMemory
from langchain.chains import RetrievalQA
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
from langchain.docstore.document import Document


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
    return [
        # TODO: Chroma can do batch querying,
        # we shouldn't hard code to the 1st result
        (Document(page_content=result[0], metadata=result[1] or {}), result[2])
        for result in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        )
    ]


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
        k: int = DEFAULT_K,
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
        docs_and_scores = self.similarity_search_with_score(
            query, k, filter=filter)
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
        collection = self.chroma_client.get_collection(name='user1_conv1')
        passage = collection.query(query_texts=[query], n_results=4, where={
                                   'user_id': 1, 'conv_id': 1})
        print("Hi I am passage", type(passage))
        x = _results_to_docs_and_scores(passage)
        print("Hii")
        print(type(x[0]))
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
        embeddings = self.client.encode(texts)
        return embeddings.tolist()

    def create_chroma_db(self, name):
        db = self.chroma_client.create_collection(
            name=name, embedding_function=self.embed_function)
        return db

    def store_embedding(self, documents, db):
        for i, d in enumerate(documents):
            db.add(
                documents=d,
                ids=str(uuid4()),
                metadatas={'user_id': 1, 'conv_id': 1}
            )

    def get_relevant_passage(self, query, db):
        passage = db.query(query_texts=[query], n_results=4, where={
                           'user_id': 1, 'conv_id': 1})['documents'][0][0]
        return passage

    def show_database(self, db):
        return pd.DataFrame(db.peek(10))


eb = HuggingFaceEmbeddings()
db = Chroma(embedding_function=eb)


prompt_template = PromptTemplate(
    template=config.prompt_template, input_variables=["context", "question"])

chain_type_kwargs = {"prompt": prompt_template}

qa = RetrievalQA.from_chain_type(llm=VicunaLLM(), chain_type="stuff", retriever=db.as_retriever(), chain_type_kwargs=chain_type_kwargs)


'''
qa = RetrievalQA(
    combine_documents_chain=doc_chain,
    chain_type="stuff",
    retriever=db.as_retriever()
)
'''
print(qa.run("tell me a joke"))
