# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /opt/python

# Set build arguments
ARG FILE_CONF_PATH=/opt/config/config.json
ARG CORE_HUB_URL=http://localhost:1717
ARG DEFAULT_PASSWORD=admin
ARG CREATE_ENTITIES_FROM_SCHEMA
ARG TARGET_TYPE
ARG TARGET_SCHEMA
ARG ENTITY_START_TIMEOUT=1
ARG TABLE_LIST_YAML=/opt/config/TABLE_LIST.yaml

# Set environment variables
ENV FILE_CONF_PATH=${FILE_CONF_PATH}
ENV CORE_HUB_URL=${CORE_HUB_URL}
ENV DEFAULT_PASSWORD=${DEFAULT_PASSWORD}
ENV CREATE_ENTITIES_FROM_SCHEMA=${CREATE_ENTITIES_FROM_SCHEMA}
ENV TARGET_TYPE=${TARGET_TYPE}
ENV TARGET_SCHEMA=${TARGET_SCHEMA}
ENV ENTITY_START_TIMEOUT=${ENTITY_START_TIMEOUT}
ENV TABLE_LIST_YAML=${TABLE_LIST_YAML}

# Copy requirements.txt and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install PyYAML
RUN pip install PyYAML

# Copy the application code
COPY . .

# Command to run the application
CMD ["python", "main.py"]
