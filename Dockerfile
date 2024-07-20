# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /opt/python

# Set build arguments
ARG FILE_CONF_PATH=/opt/config/config.json
ARG CORE_HUB_URL=http://localhost:1717
ARG DEFAULT_PASSWORD=admin

# Set environment variables
ENV FILE_CONF_PATH=${FILE_CONF_PATH}
ENV CORE_HUB_URL=${CORE_HUB_URL}
ENV DEFAULT_PASSWORD=${DEFAULT_PASSWORD}

# Copy requirements.txt and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Command to run the application
CMD ["python", "main.py"]
