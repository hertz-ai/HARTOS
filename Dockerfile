FROM python:3.10

WORKDIR /app

COPY . .

RUN touch /app/langchain.log

RUN pip install --upgrade pip
RUN pip install bs4
RUN pip install -r requirements.txt

EXPOSE 5000

CMD [ "python", "langchain_gpt_api.py" ]
