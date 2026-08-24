#!/bin/bash
# SECURITY: GitHub token must be provided via environment variable, never hardcoded
TOKEN="${GITHUB_TOKEN:?ERROR: Set GITHUB_TOKEN environment variable}"
#basic upgrade and install
sudo apt update
sudo apt-get upgrade
sudo apt install  -y python-is-python3
sudo apt install pip
cd /opt
git clone https://$TOKEN@github.com/hertz-ai/auto_dns.git
cd auto_dns
python startup.py langchain aws
 
cd /opt/
 
#install nvidia-drivers
# sudo apt-get -y install linux-headers-$(uname -r)
# wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/x86_64/cuda-ubuntu2004.pin
# sudo mv cuda-ubuntu2004.pin /etc/apt/preferences.d/cuda-repository-pin-600
# wget https://developer.download.nvidia.com/compute/cuda/12.2.2/local_installers/cuda-repo-ubuntu2004-12-2-local_12.2.2-535.104.05-1_amd64.deb
# sudo dpkg -i cuda-repo-ubuntu2004-12-2-local_12.2.2-535.104.05-1_amd64.deb
# sudo cp /var/cuda-repo-ubuntu2004-12-2-local/cuda-*-keyring.gpg /usr/share/keyrings/
# sudo apt-get update
# sudo apt-get -y install cuda

#install docker
curl https://get.docker.com | sh \
&& sudo systemctl --now enable docker
cd /opt/
 
# #install nvidia container toolkit
# distribution=$(. /etc/os-release;echo $ID$VERSION_ID) \
# && curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add - \
# && curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | sudo tee /etc/apt/sources.list.d/nvidia-docker.list
# sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit && sudo apt-get install -y nvidia-docker2
# sudo systemctl restart docker

#clone repo (default branch — this script used to pin -b sahil, deploying a
#stale personal branch on every fresh VM)
cd /opt/
git clone https://$TOKEN@github.com/hertz-ai/HARTOS.git
cd HARTOS

sudo docker build -t langchain_gpt:latest .

# Canonical container start.  Until 2026-08-24 this line was a bare
# `docker run langchain_gpt` (foreground, unnamed, no ports) and the real
# invocation lived only in operators' shell history — every box drifted.
#
# The CANONICAL deploy is .github/workflows/deploy-hartos-deepbox.yml
# (sha-tagged build + signed release manifest + /status-gated rollback);
# this script only provisions a box the workflow has not adopted yet.  Its
# run invocation is a COPY of the workflow's — when you change one, change
# the other (the workflow is the authority).
#
# Networking: bridge + published 6777, same as the workflow.  A central-
# tier node is found by URL over HTTP and needs no UDP beacon.  Set
# HART_DOCKER_NETWORK=host ONLY for a flat/regional node on a LAN that
# must do zero-config discovery: deployment-manifest.json declares
# hart-discovery {6780, udp, always_enabled}, and bridge NAT cannot
# deliver subnet-broadcast beacons (DNAT matches the host IP, not
# 192.168.0.255) — host networking is the only mode where they arrive.
NETWORK_MODE="${HART_DOCKER_NETWORK:-bridge}"
if [ "$NETWORK_MODE" = "host" ]; then
    NET_ARGS="--network host"
else
    NET_ARGS="-p 6777:6777"
fi
KEY_ARGS=""
if sudo test -f /etc/hevolve/master_private_key.hex; then
    KEY_ARGS="-e HEVOLVE_MASTER_PRIVATE_KEY=$(sudo cat /etc/hevolve/master_private_key.hex)"
fi
MOUNT_ARGS=""
# -v creates a DIRECTORY for a missing host path, wedging the container's
# config.json — mount only what exists.
[ -f "$(pwd)/config.json" ] && MOUNT_ARGS="$MOUNT_ARGS -v $(pwd)/config.json:/app/config.json:ro"
[ -f "$(pwd)/release_manifest.json" ] && MOUNT_ARGS="$MOUNT_ARGS -v $(pwd)/release_manifest.json:/app/release_manifest.json:ro"
sudo mkdir -p /opt/hzai-LLM-Langchain-Chatbot-Agent/logs /opt/hzai-LLM-Langchain-Chatbot-Agent/mount/images
# Flywheel state (goals DB, outreach prospects, node keys) lives in
# /app/agent_data.  Unmounted, a rebuild wiped it — the 2026-04-28 wipe
# in FLYWHEEL_RECOVERY_BRIEF.md.  Same mount as the workflow.
sudo mkdir -p /opt/hzai-LLM-Langchain-Chatbot-Agent/agent_data
ENV_FILE_ARGS=""
[ -f "$(pwd)/.env" ] && ENV_FILE_ARGS="--env-file $(pwd)/.env"
# Routable self-URL for peers — get_advertisable_base_url precedence 1.
# A bridge container otherwise advertises its docker-internal NIC
# (measured 2026-08-24: http://172.17.0.4:6777 in a LAN peer's table).
# Same derivation as deploy-hartos-deepbox.yml; export HEVOLVE_BASE_URL
# before running to override.
ADV_ARGS=""
if [ "$NETWORK_MODE" != "host" ]; then
    _ADV="${HEVOLVE_BASE_URL}"
    if [ -z "$_ADV" ]; then
        _HOST_IP="$(ip route get 1 2>/dev/null | awk '{for(i=1;i<NF;i++) if ($i=="src") {print $(i+1); exit}}')"
        [ -n "$_HOST_IP" ] && _ADV="http://${_HOST_IP}:6777"
    fi
    [ -n "$_ADV" ] && ADV_ARGS="-e HEVOLVE_BASE_URL=$_ADV"
fi
sudo docker rm -f langchain 2>/dev/null || true
sudo docker run -d --name langchain --restart unless-stopped \
    $NET_ARGS \
    $ENV_FILE_ARGS \
    $ADV_ARGS \
    $KEY_ARGS \
    $MOUNT_ARGS \
    -v /opt/hzai-LLM-Langchain-Chatbot-Agent/logs:/app/logs \
    -v /opt/hzai-LLM-Langchain-Chatbot-Agent/mount/images:/app/output_images \
    -v /opt/hzai-LLM-Langchain-Chatbot-Agent/agent_data:/app/agent_data \
    -e HEVOLVE_KEY_DIR=/app/agent_data \
    langchain_gpt:latest

docker start $(docker ps -a -q)
sudo tee /etc/systemd/system/restart-docker-containers.service > /dev/null <<EOL
[Unit]
Description=Restart Docker Containers on VM Restart
After=docker.service
Requires=docker.service
 
[Service]
Type=oneshot
ExecStart=/usr/bin/docker restart \$(docker ps -q)
User=root
 
[Install]
WantedBy=default.target
EOL
 
# Enable and start the service
sudo systemctl enable restart-docker-containers.service
sudo systemctl start restart-docker-containers.service
 
# Check service status
sudo systemctl status restart-docker-containers.service