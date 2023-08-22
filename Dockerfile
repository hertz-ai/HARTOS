# Use the official Python base image
FROM python:3.8-slim

# Set the working directory inside the container
WORKDIR /app

RUN export HNSWLIB_NO_NATIVE=1

# Copy the requirements file into the container
COPY requirements1.txt .

# Install the required dependencies
RUN pip install --no-cache-dir -r requirements1.txt

# Copy the rest of the application code into the container
COPY . .

# Expose the port that the Flask app will run on
EXPOSE 5002

# Define the command to run the Flask app
CMD ["python", "temp_one_on_one.py"]
