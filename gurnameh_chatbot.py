import faiss
from langchain.docstore import InMemoryDocstore
from langchain.vectorstores import FAISS
from langchain.memory import VectorStoreRetrieverMemory
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.prompts import PromptTemplate
from langchain.llms import OpenAI
from langchain.chains import ConversationChain
import json
from flask import Flask, request, jsonify
import os


os.environ["OPENAI_API_KEY"] = "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"


app = Flask(__name__)

llm = OpenAI(temperature=0) # Can be any valid LLM
_DEFAULT_TEMPLATE = """You are student and you need to act according to your age give in input.
anser as per your age if you don't know the answer just say Sorry! I don't know the answer

Relevant pieces of previous conversation:
{history}

(You do not need to use these pieces of information if not relevant)

Current conversation:
Human: {input}
AI:"""

PROMPT = PromptTemplate(
input_variables=["history", "input"], template=_DEFAULT_TEMPLATE
)

@app.route("/chat", methods=['POST'])
def api():
    data = request.get_json()
    prompt = data['prompt']
    conversation = data['conversation_list']
    print(prompt,conversation)
    embedding_size = 1536 # Dimensions of the OpenAIEmbeddings
    index = faiss.IndexFlatL2(embedding_size)
    embedding_fn = OpenAIEmbeddings().embed_query
    vectorstore = FAISS(embedding_fn, index, InMemoryDocstore({}), {})
    # In actual usage, you would set `k` to be a higher value, but we use k=1 to show that
    # the vector lookup still returns the semantically relevant information
    retriever = vectorstore.as_retriever(search_kwargs={"metadatas":{'user_id': 2, 'conv_id': 1},"k":2})
    memory = VectorStoreRetrieverMemory(retriever=retriever)

    # When added to an agent, the memory object can save pertinent information from conversations or used tools
    if conversation != []:
        for conv in conversation:
            memory.save_context({"input": conv[0]}, {"output": conv[1]})
    
    conversation_with_summary = ConversationChain(
        llm=llm, 
        prompt=PROMPT,
        # We set a very low max_token_limit for the purposes of testing.
        memory=memory,
        verbose=True
    )  
    ans = conversation_with_summary.predict(input=prompt) 
    return ans

if __name__=="__main__":
    app.run(host="0.0.0.0", debug=True, port=8088)
