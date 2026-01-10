#!/usr/bin/env bash
set -euo pipefail

log() {
  printf "==> %s\n" "$*"
}

maybe_sudo() {
  if "$@"; then
    return 0
  fi
  if [[ "${EUID}" -ne 0 ]]; then
    log "Retrying with sudo: $*"
    sudo "$@"
  else
    return 1
  fi
}

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
DEFAULT_APP_PATH="${ROOT_DIR}/dist/Gluesync Automator.app"
APP_SOURCE="${1:-${DEFAULT_APP_PATH}}"
DEST_DIR="${DEST_APP_DIR:-/Applications}"

if [[ ! -d "${APP_SOURCE}" ]]; then
  echo "Error: app bundle not found at ${APP_SOURCE}" >&2
  exit 1
fi

APP_BASENAME="$(basename "${APP_SOURCE}")"
DEST_PATH="${DEST_DIR}/${APP_BASENAME}"

log "Installing ${APP_SOURCE} -> ${DEST_PATH}"
if [[ -d "${DEST_PATH}" ]]; then
  log "Removing existing ${DEST_PATH}"
  maybe_sudo rm -rf "${DEST_PATH}"
fi

log "Copying app bundle"
maybe_sudo cp -R "${APP_SOURCE}" "${DEST_PATH}"

log "Clearing quarantine attributes"
maybe_sudo xattr -dr com.apple.quarantine "${DEST_PATH}" || true

log "Registering app with Launch Services"
maybe_sudo /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "${DEST_PATH}" >/dev/null 2>&1 || true

log "Launching ${DEST_PATH}"
maybe_sudo open "${DEST_PATH}"

log "Post-build install complete"
