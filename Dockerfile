# Copyright (c) 2024 MOLO17
# Author: Daniele Angeli
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /opt/python

# Set build arguments
ARG FILE_CONF_PATH=/opt/config/config.json
ARG CORE_HUB_URL=http://localhost:1717
ARG CREATE_ENTITIES_FROM_SCHEMA
ARG SOURCE_TYPE=SQL
ARG TARGET_TYPE
ARG TARGET_SCHEMA
ARG ENTITY_START_TIMEOUT=1
ARG TABLE_LIST_YAML=/opt/config/TABLE_LIST.yaml
ARG LOG_DIR=/logs
ARG DEFAULT_PASSWORD
ARG GLUESYNC_LICENSE_FILE
ARG GLUESYNC_MODULE_TAG=gluesync-bootstrapper
ARG GLUESYNC_USE_SSL=False
ARG GLUESYNC_SECURITY_CONFIG

# Set environment variables
ENV FILE_CONF_PATH=${FILE_CONF_PATH}
ENV CORE_HUB_URL=${CORE_HUB_URL}
ENV CREATE_ENTITIES_FROM_SCHEMA=${CREATE_ENTITIES_FROM_SCHEMA}
ENV SOURCE_TYPE=${SOURCE_TYPE}
ENV TARGET_TYPE=${TARGET_TYPE}
ENV TARGET_SCHEMA=${TARGET_SCHEMA}
ENV ENTITY_START_TIMEOUT=${ENTITY_START_TIMEOUT}
ENV TABLE_LIST_YAML=${TABLE_LIST_YAML}
ENV LOG_DIR=${LOG_DIR}
ENV DEFAULT_PASSWORD=""
ENV GLUESYNC_LICENSE_FILE=${GLUESYNC_LICENSE_FILE}
ENV GLUESYNC_MODULE_TAG=${GLUESYNC_MODULE_TAG}
ENV GLUESYNC_USE_SSL=${GLUESYNC_USE_SSL}
ENV GLUESYNC_SECURITY_CONFIG=${GLUESYNC_SECURITY_CONFIG}

# Copy requirements.txt and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Copy the gluesync-client-sdk as a submodule
COPY gluesync-client-sdk /opt/gluesync-client-sdk

# Install the SDK from the submodule first
RUN pip install -e /opt/gluesync-client-sdk

# Copy the license file
COPY ${GLUESYNC_LICENSE_FILE} /opt/gluesync/data/gs-license.dat

# Create logs directory
RUN mkdir -p ${LOG_DIR} && chmod 777 ${LOG_DIR}

# Command to run the application
CMD ["python", "main.py"]
