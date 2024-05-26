FROM python:3.10

WORKDIR /app

COPY . .

RUN touch /app/langchain.log
# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgl1-mesa-glx
RUN pip install --upgrade pip
RUN pip install bs4
RUN pip install -r requirements.txt

EXPOSE 5000

CMD [ "python", "langchain_gpt_api.py" ]
