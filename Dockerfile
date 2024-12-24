FROM python:3.10

WORKDIR /app

COPY . .

RUN touch /app/langchain.log
RUN pip install --upgrade pip
RUN pip install bs4
RUN pip install -r requirements.txt
RUN pip install autogen-agentchat==0.2.37 apscheduler autobahn==23.1.2
RUN pip install autobahn[serialization] autobahn[twisted]

EXPOSE 6777

CMD [ "python", "langchain_gpt_api.py" ]
