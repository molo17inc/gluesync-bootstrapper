# Gluesync Automator container image
# Provides SDK-enabled FastAPI UI suitable for iframe embedding
FROM python:3.10-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/automator

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN pip install --no-cache-dir \
    websockets==11.0.3 \
    cryptography \
    pycryptodome \
    pyasn1 \
    pyasn1_modules \
    javaobj-py3 \
    pyjks

COPY automator_app ./automator_app
COPY utils ./utils
COPY gluesync-client-sdk ./gluesync-client-sdk
COPY commons.py create_all_entities.py create_all_tables.py create_user_defined_functions.py \
     add_agents_with_conductor.py export_all_pipelines.py export_template_from_corehub.py ./
COPY agents.json table-list-template.yaml table-list-template-basic.yaml ./
COPY run_automator.py ./
COPY docker/automator-entrypoint.sh /usr/local/bin/automator-entrypoint.sh
RUN chmod +x /usr/local/bin/automator-entrypoint.sh

# Install SDK package contents into site-packages
RUN python - <<'PY'
import pathlib, site, shutil
src = pathlib.Path('/opt/automator/gluesync-client-sdk/gluesync_sdk')
if not src.exists():
    raise SystemExit('gluesync-client-sdk submodule not found; ensure it is cloned')
dst = pathlib.Path(site.getsitepackages()[0]) / 'gluesync_sdk'
if dst.exists():
    shutil.rmtree(dst)
shutil.copytree(src, dst)
(dst / '__init__.py').touch()
PY

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
    AUTOMATOR_HOST=0.0.0.0 \
    AUTOMATOR_PORT=1717

EXPOSE 1717

CMD ["automator-entrypoint.sh"]
