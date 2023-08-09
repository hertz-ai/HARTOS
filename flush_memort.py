from langchain.memory import ZepMemory
from langchain.retrievers import ZepRetriever
from langchain import OpenAI
from langchain.schema import HumanMessage, AIMessage
from langchain.utilities import WikipediaAPIWrapper
from langchain.agents import initialize_agent, AgentType, Tool
from uuid import uuid4

ZEP_API_URL = "http://4.224.46.164:8000"

session_id = 'user_10077'


memory = ZepMemory(
    session_id=session_id,
    url=ZEP_API_URL,
    memory_key="chat_history",
)

memory.clear()
