# Gluesync Automator container image
# Provides SDK-enabled FastAPI UI suitable for iframe embedding
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/automator

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

ARG GITLAB_TOKEN
RUN pip install --no-cache-dir \
    --extra-index-url "https://oauth2:${GITLAB_TOKEN}@gitlab.com/api/v4/projects/68232363/packages/pypi/simple" \
    -r requirements.txt

RUN pip install --no-cache-dir \
    websockets==11.0.3 \
    cryptography \
    pycryptodome \
    pyasn1 \
    pyasn1_modules \
    javaobj-py3 \
    pyjks

# Copy and install the gluesync-sdk (only for local testing)
# In production, SDK is installed via requirements.txt from PyPI
COPY gluesync-client-sdk ./gluesync-client-sdk
RUN python -c "import gluesync_sdk; print('SDK already installed from requirements.txt')" 2>/dev/null || \
    (if [ -d "./gluesync-client-sdk/gluesync_sdk" ]; then \
        echo "Installing SDK from local submodule for testing..."; \
        SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])"); \
        mkdir -p $SITE_PACKAGES/gluesync_sdk; \
        cp -r ./gluesync-client-sdk/gluesync_sdk/* $SITE_PACKAGES/gluesync_sdk/; \
        touch $SITE_PACKAGES/gluesync_sdk/__init__.py; \
        python -c "import gluesync_sdk; print('SDK import successful from submodule')" || exit 1; \
    else \
        echo "ERROR: SDK not in requirements.txt and submodule not found"; \
        exit 1; \
    fi)

COPY automator_app ./automator_app
COPY utils ./utils
COPY commons.py create_all_entities.py create_all_tables.py create_user_defined_functions.py \
     add_agents_with_conductor.py export_all_pipelines.py export_template_from_corehub.py ./
COPY agents.json table-list-template.yaml table-list-template-basic.yaml ./
COPY run_automator.py ./
COPY docker/automator-entrypoint.sh /usr/local/bin/automator-entrypoint.sh
RUN chmod +x /usr/local/bin/automator-entrypoint.sh

RUN mkdir -p /opt/gluesync/shared

ENV GLUESYNC_LICENSE_FILE=/opt/gluesync/shared/gs-license.dat \
    GLUESYNC_SECURITY_CONFIG=/opt/gluesync/shared/security-config.json \
    CORE_HUB_URL=https://gluesync-core-hub:1717 \
    USE_SDK=1 \
    SSL_ENABLED=True \
    SSL_SKIP_VERIFY=True \
    ENABLE_SCHEDULING=True \
    CREATE_TABLE_IF_NOT_EXISTS=True \
    AUTOMATOR_IFRAME_MODE=1 \
    AUTOMATOR_HIDE_HEADER=1 \
    AUTOMATOR_HIDE_COREHUB_INFO=1 \
    AUTOMATOR_HEADLESS=1 \
    AUTOMATOR_BASE_PATH=/automator \
    AUTOMATOR_HOST=0.0.0.0 \
    AUTOMATOR_PORT=1717 \
    AUTOMATOR_SSL_CERTFILE=/opt/gluesync/shared/gluesync-cert.pem \
    AUTOMATOR_SSL_KEYFILE=/opt/gluesync/shared/gluesync-key.pem

EXPOSE 1717

CMD ["automator-entrypoint.sh"]
