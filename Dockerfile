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

# ── Layer 2a: Upgrade pip ──
RUN pip install --upgrade pip

# ── Layer 2b: Google/gRPC stack (biggest backtracking source — install first) ──
#    Installing these first pins all their transitive deps, so the main install
#    doesn't backtrack through thousands of version combinations.
RUN pip install --no-cache-dir \
        "grpcio==1.57.0" \
        "grpcio-status==1.57.0" \
        "google-cloud-aiplatform==1.130.0" \
        "google-cloud-bigquery==3.13.0" \
        "google-cloud-storage==3.9.0" \
        "google-cloud-resource-manager==1.10.4" \
        "google-api-python-client==2.190.0" \
        "google-auth==2.49.0" \
        "google-api-core==2.28.0" \
        "googleapis-common-protos==1.59.1" \
        "grpc-google-iam-v1==0.14.2" \
        "proto-plus==1.22.3" \
        "protobuf==4.23.3" \
        "google-genai==1.56.0" \
        "langchain-google-genai==4.2.1"

# ── Layer 2c: LLM/AI stack (second backtracking source) ──
RUN pip install --no-cache-dir \
        "openai==1.82.0" \
        "anthropic==0.83.0" \
        "langchain-classic==1.0.1" \
        "langchain-community==0.4.1" \
        "langchain-anthropic==1.0.0" \
        "langchain-core==1.2.15" \
        "langchain-text-splitters==1.1.1" \
        "langchain-groq==1.1.2"

# ── Layer 2d: Remaining requirements ──
#    Use --no-deps for requirements.txt because layers 2b/2c already resolved
#    the heavy Google/LLM transitive deps. This avoids resolution-too-deep.
COPY requirements.txt .
COPY agent-ledger-opensource/ ./agent-ledger-opensource/

RUN pip install --no-cache-dir --no-deps -r requirements.txt && \
    pip install --no-cache-dir ./agent-ledger-opensource && \
    pip install --no-cache-dir \
        autogen-agentchat==0.2.37 \
        apscheduler \
        autobahn==23.1.2 \
        "autobahn[serialization]" \
        "autobahn[twisted]" \
        "autogen-agentchat[long-context]~=0.2" \
        json-repair \
        bs4 \
        youtube-transcript-api

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

# ── Layer 4: LiveKit SFU binary (regional/flat — voice/video for >4-party calls) ──
# Pre-stage the official livekit-server Go binary at /usr/local/bin so
# the supervisor in integrations.social.livekit_supervisor doesn't need
# to download it at first start.  Skipped automatically at runtime when
# HEVOLVE_DEPLOY_MODE=central or LIVEKIT_DISABLE=1 (cloud / sync-only
# deployments use Dockerfile.prod which omits this entire layer).
#
# The supervisor checks ~/.hevolve/livekit/livekit-server first AND
# falls through to PATH lookup, so dropping it at /usr/local/bin works.
ARG LIVEKIT_VERSION=1.7.2
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) tag=linux_amd64 ;; \
      arm64) tag=linux_arm64 ;; \
      *) echo "Unsupported arch $arch for livekit-server" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/livekit/livekit/releases/download/v${LIVEKIT_VERSION}/livekit_${LIVEKIT_VERSION}_${tag}.tar.gz"; \
    curl -fsSL "$url" -o /tmp/lk.tgz && \
    tar -xzf /tmp/lk.tgz -C /tmp && \
    install -m 0755 /tmp/livekit-server /usr/local/bin/livekit-server && \
    rm -f /tmp/lk.tgz /tmp/livekit-server

# LiveKit RTC ports: WS signaling 7880, TCP fallback 7881, UDP range
# 50000-60000 for media.  Operators expose only what their NAT requires.
EXPOSE 6777 7880 7881

CMD [ "python", "hart_intelligence_entry.py" ]
