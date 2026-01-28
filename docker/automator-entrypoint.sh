#!/usr/bin/env bash
set -euo pipefail

AUTOMATOR_HOST=${AUTOMATOR_HOST:-0.0.0.0}
AUTOMATOR_PORT=${AUTOMATOR_PORT:-8080}

export GLUESYNC_LICENSE_FILE=${GLUESYNC_LICENSE_FILE:-/opt/gluesync/data/gs-license.dat}
export GLUESYNC_SECURITY_CONFIG=${GLUESYNC_SECURITY_CONFIG:-/opt/gluesync/data/security-config.json}
export CORE_HUB_URL=${CORE_HUB_URL:-https://gluesync-core-hub:1717}
export USE_SDK=${USE_SDK:-1}
export SSL_ENABLED=${SSL_ENABLED:-True}
export SSL_SKIP_VERIFY=${SSL_SKIP_VERIFY:-True}
export ENABLE_SCHEDULING=${ENABLE_SCHEDULING:-True}
export CREATE_TABLE_IF_NOT_EXISTS=${CREATE_TABLE_IF_NOT_EXISTS:-True}
export AUTOMATOR_HEADLESS=${AUTOMATOR_HEADLESS:-1}

iframe_default=${AUTOMATOR_IFRAME_MODE:-1}
export AUTOMATOR_IFRAME_MODE=${iframe_default}
export AUTOMATOR_HIDE_HEADER=${AUTOMATOR_HIDE_HEADER:-${iframe_default}}
export AUTOMATOR_HIDE_COREHUB_INFO=${AUTOMATOR_HIDE_COREHUB_INFO:-${iframe_default}}

if [[ ! -f "${GLUESYNC_LICENSE_FILE}" ]]; then
  echo "[automator-entrypoint] Warning: license file not found at ${GLUESYNC_LICENSE_FILE}" >&2
fi

if [[ ! -f "${GLUESYNC_SECURITY_CONFIG}" ]]; then
  echo "[automator-entrypoint] Warning: security config not found at ${GLUESYNC_SECURITY_CONFIG}" >&2
fi

exec python -m automator_app.cli --host "${AUTOMATOR_HOST}" --port "${AUTOMATOR_PORT}" --no-open-browser
