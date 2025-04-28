FROM python:3.10

WORKDIR /app

COPY . .

RUN touch /app/langchain.log
RUN pip install --upgrade pip
# Install system dependencies for OpenCV
RUN apt-get update && apt-get install -y \exit \




    libgl1-mesa-glx \
    libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
RUN pip install bs4
RUN pip install -r requirements.txt
RUN pip install autogen-agentchat==0.2.37 apscheduler autobahn==23.1.2
RUN pip install autobahn[serialization] autobahn[twisted]
RUN pip install autogen-agentchat[long-context]~=0.2
RUN pip install json-repair

EXPOSE 6777

CMD [ "python", "langchain_gpt_api.py" ]
