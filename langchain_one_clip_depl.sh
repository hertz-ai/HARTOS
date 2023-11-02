#!/bin/bash

git clone https://github.com/hertz-ai/LLM-langchain_Chatbot-Agent.git

docker build -t langchain_gpt:latest .

dokcer run langchain_gpt