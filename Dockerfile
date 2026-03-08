FROM python:3.10

ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY . .

RUN touch /app/langchain.log
RUN pip install --upgrade pip
# Install system dependencies for OpenCV
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
RUN pip install bs4
RUN pip install -r requirements.txt
RUN pip install ./agent-ledger-opensource
RUN pip install autogen-agentchat==0.2.37 apscheduler autobahn==23.1.2
RUN pip install autobahn[serialization] autobahn[twisted]
RUN pip install autogen-agentchat[long-context]~=0.2
RUN pip install json-repair

# HevolveAI source protection: compile .py → .pyc, strip source, clean metadata
RUN python scripts/compile_hevolveai.py --strip-source \
    --manifest-out security/hevolveai_manifest.json 2>/dev/null || true && \
    find /usr/local/lib -path '*hevolveai*dist-info/direct_url.json' -delete 2>/dev/null || true && \
    find /usr/local/lib -path '*embodied*ai*dist-info/direct_url.json' -delete 2>/dev/null || true && \
    find /usr/local/lib -path '*hevolveai*dist-info/RECORD' -delete 2>/dev/null || true && \
    find /usr/local/lib -path '*embodied*ai*dist-info/RECORD' -delete 2>/dev/null || true

EXPOSE 6777

CMD [ "python", "langchain_gpt_api.py" ]
