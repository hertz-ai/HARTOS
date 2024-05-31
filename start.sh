#!bin/bash
# pip install --upgrade pip
# docker build -t langchain_gpt:v1 .
# docker kill langchain
# docker rm langchain
# docker run -dp 5055:5000 --network host --name langchain langchain_gpt:v1


sudo pip install --upgrade pip
sudo docker build -t langchain_gpt1:v1 .
sudo docker kill langchain
sudo docker rm langchain
sudo docker run -dp 5000:5000 --network host -v "/opt/LLM-langchain_Chatbot-Agent/langchain.log:/app/langchain.log" -v "/opt/langchain_input_images/:/app/output_images/" --name langchain langchain_gpt1:v1