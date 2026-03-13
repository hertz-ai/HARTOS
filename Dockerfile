FROM python:3.10

ENV PYTHONDONTWRITEBYTECODE=1
ENV DOCKER_CONTAINER=true

WORKDIR /app

# ── Layer 1: System deps (rarely changes, cached long-term) ──
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 2: Python deps (only rebuilds when requirements.txt changes) ──
COPY requirements.txt .
COPY agent-ledger-opensource/ ./agent-ledger-opensource/

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir ./agent-ledger-opensource && \
    pip install --no-cache-dir \
        autogen-agentchat==0.2.37 \
        apscheduler \
        autobahn==23.1.2 \
        "autobahn[serialization]" \
        "autobahn[twisted]" \
        "autogen-agentchat[long-context]~=0.2" \
        json-repair \
        bs4

# ── Layer 3: Application code (rebuilds on any code change — fast, no pip) ──
COPY . .

RUN touch /app/langchain.log

# HevolveAI source protection: compile .py → .pyc, strip source, clean metadata
RUN python scripts/compile_hevolveai.py --strip-source \
    --manifest-out security/hevolveai_manifest.json 2>/dev/null || true && \
    find /usr/local/lib -path '*hevolveai*dist-info/direct_url.json' -delete 2>/dev/null || true && \
    find /usr/local/lib -path '*embodied*ai*dist-info/direct_url.json' -delete 2>/dev/null || true && \
    find /usr/local/lib -path '*hevolveai*dist-info/RECORD' -delete 2>/dev/null || true && \
    find /usr/local/lib -path '*embodied*ai*dist-info/RECORD' -delete 2>/dev/null || true

EXPOSE 6777

CMD [ "python", "langchain_gpt_api.py" ]
