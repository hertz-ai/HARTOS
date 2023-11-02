#!/bin/bash
TOKEN="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"
#basic uprade and install
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

#clone repo
cd /opt/
git clone https://$TOKEN@github.com/hertz-ai/LLM-langchain_Chatbot-Agent.git
cd LLM-langchain_Chatbot-Agent

sudo docker build -t langchain_gpt:latest .

sudo docker run langchain_gpt

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