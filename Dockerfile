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
ARG CHRONOS_URL=http://gluesync-chronos:8000

ARG SOURCE_TYPE=SQL
ARG TARGET_TYPE

ARG ENTITY_START_TIMEOUT=1
ARG TABLE_LIST_YAML=/opt/config/TABLE_LIST.yaml
ARG LOG_DIR=/logs
ARG UDF_PATH=/opt/udfs
ARG DEFAULT_PASSWORD
ARG GLUESYNC_LICENSE_FILE
ARG GLUESYNC_MODULE_TAG=gluesync-bootstrapper
ARG SSL_ENABLED=False
ARG GLUESYNC_SECURITY_CONFIG
ARG ENABLE_SCHEDULING=true
ARG USE_SDK=False
ARG SSL_SKIP_VERIFY=True
ARG CREATE_TABLE_IF_NOT_EXISTS=true
ARG LOG_LEVEL=INFO
ARG TEST_NAME=DRY_RUN
ARG VERSION=latest
ARG JOB_ID=not_set
ARG HANDLE_WITH_CONDUCTOR=false

# Set environment variables
ENV FILE_CONF_PATH=${FILE_CONF_PATH}
ENV CORE_HUB_URL=${CORE_HUB_URL}
ENV CHRONOS_URL=${CHRONOS_URL}

ENV SOURCE_TYPE=${SOURCE_TYPE}
ENV TARGET_TYPE=${TARGET_TYPE}

ENV ENTITY_START_TIMEOUT=${ENTITY_START_TIMEOUT}
ENV TABLE_LIST_YAML=${TABLE_LIST_YAML}
ENV LOG_DIR=${LOG_DIR}
ENV UDF_PATH=${UDF_PATH}
ENV DEFAULT_PASSWORD=""
ENV USE_SDK=${USE_SDK}
ENV GLUESYNC_LICENSE_FILE=${GLUESYNC_LICENSE_FILE}
ENV GLUESYNC_MODULE_TAG=${GLUESYNC_MODULE_TAG}
ENV SSL_ENABLED=${SSL_ENABLED}
ENV GLUESYNC_SECURITY_CONFIG=${GLUESYNC_SECURITY_CONFIG}
ENV ENABLE_SCHEDULING=${ENABLE_SCHEDULING}
ENV SSL_SKIP_VERIFY=${SSL_SKIP_VERIFY}
ENV CREATE_TABLE_IF_NOT_EXISTS=${CREATE_TABLE_IF_NOT_EXISTS}
ENV LOG_LEVEL=${LOG_LEVEL}
ENV TEST_NAME=${TEST_NAME}
ENV VERSION=${VERSION}
ENV JOB_ID=${JOB_ID}

# Copy requirements.txt and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code including the submodule
COPY . .

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libssl-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install SDK dependencies one by one
RUN pip install websockets==11.0.3 && \
    pip install cryptography && \
    pip install pycryptodome && \
    pip install pyasn1 && \
    pip install pyasn1_modules && \
    pip install javaobj-py3 && \
    pip install pyjks

# Install the SDK by directly copying it to the Python path
RUN if [ -d "./gluesync-client-sdk/gluesync_sdk" ]; then \
    # Get the Python site-packages directory
    SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])"); \
    # Create the directory if it doesn't exist
    mkdir -p $SITE_PACKAGES/gluesync_sdk; \
    # Copy the SDK module
    cp -r ./gluesync-client-sdk/gluesync_sdk/* $SITE_PACKAGES/gluesync_sdk/; \
    # Make sure __init__.py exists
    touch $SITE_PACKAGES/gluesync_sdk/__init__.py; \
    # Add aliases to the __init__.py file for backward compatibility
    echo "# Add aliases for backward compatibility" >> $SITE_PACKAGES/gluesync_sdk/__init__.py; \
    echo "GluesyncSDK = GluesyncClient" >> $SITE_PACKAGES/gluesync_sdk/__init__.py; \
    echo "class MockGluesyncSDK:" >> $SITE_PACKAGES/gluesync_sdk/__init__.py; \
    echo "    def __init__(self, **kwargs):" >> $SITE_PACKAGES/gluesync_sdk/__init__.py; \
    echo "        self.module_tag = kwargs.get('module_tag', 'unknown')" >> $SITE_PACKAGES/gluesync_sdk/__init__.py; \
    echo "        self._token = 'mock-token'" >> $SITE_PACKAGES/gluesync_sdk/__init__.py; \
    echo "        import logging" >> $SITE_PACKAGES/gluesync_sdk/__init__.py; \
    echo "        logging.warning(f'Initialized mock SDK with module_tag={self.module_tag}')" >> $SITE_PACKAGES/gluesync_sdk/__init__.py; \
    echo "    def get_core_hub_url(self):" >> $SITE_PACKAGES/gluesync_sdk/__init__.py; \
    echo "        import os" >> $SITE_PACKAGES/gluesync_sdk/__init__.py; \
    echo "        return os.getenv('CORE_HUB_URL', 'http://gluesync-core-hub:1717')" >> $SITE_PACKAGES/gluesync_sdk/__init__.py; \
    echo "SDK copied to $SITE_PACKAGES/gluesync_sdk/"; \
    # Verify the installation
    python -c "import gluesync_sdk; print('SDK import successful')" || exit 1; \
    else \
    echo "ERROR: SDK submodule not found"; \
    ls -la; \
    ls -la ./gluesync-client-sdk || echo "gluesync-client-sdk directory not found"; \
    exit 1; \
    fi

# Stay in the python directory
WORKDIR /opt/python

# Copy the license file
COPY ${GLUESYNC_LICENSE_FILE} /opt/gluesync/data/gs-license.dat

# Create logs and UDF directories
RUN mkdir -p ${LOG_DIR} && chmod 777 ${LOG_DIR}
RUN mkdir -p ${UDF_PATH} && chmod 777 ${UDF_PATH}

# Command to run the application
CMD ["python", "main.py"]
