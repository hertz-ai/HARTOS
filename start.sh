#!/bin/bash
# pip install --upgrade pip
# docker build -t langchain_gpt:v1 .
# docker kill langchain
# docker rm langchain
# docker run -dp 5055:5000 --network host --name langchain langchain_gpt:v1


# sudo pip install --upgrade pip
sudo docker build -t langchain_gpt1:v1 .
sudo docker kill langchain
sudo docker rm langchain
sudo docker run -dp 5000:5000 --network host -v "/opt/hzai-langchain/logs/langchain.log:/app/langchain.log" -v "/opt/hzai-langchain/mount/images:/app/output_images/" -v "/opt/hzai-langchain/mount/prompts:/app/prompts/" --name langchain langchain_gpt1:v1
