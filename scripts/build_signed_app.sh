#!/usr/bin/env bash
set -euo pipefail

# Build, wrap, sign, and optionally notarize the Gluesync Automator macOS app.
# Usage examples:
#   SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" ./scripts/build_signed_app.sh
#   SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#     NOTARIZE=1 APPLE_ID="you@example.com" TEAM_ID="TEAMID" NOTARY_PASSWORD="app-specific-password" \
#     ./scripts/build_signed_app.sh
#   SIGN_IDENTITY="Developer ID Application: MOLO17 SRL (935BPM8A8T)" \
#     NOTARIZE=1 NOTARY_KEYCHAIN_PROFILE="Gluesync" \
#     ./scripts/build_signed_app.sh

# SIGNED_IDENTITY="Developer ID Application: MOLO17 SRL (935BPM8A8T)" NOTARIZE=1 NOTARY_KEYCHAIN_PROFILE="Gluesync" ./scripts/build_signed_app.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
PYINSTALLER_SPEC="${PYINSTALLER_SPEC:-automator-macos.spec}"
PYINSTALLER_OUTPUT="${PYINSTALLER_OUTPUT:-${DIST_DIR}/gluesync-automator-macos}"

APP_NAME="${APP_NAME:-Gluesync Automator}"
APP_EXECUTABLE_NAME="${APP_EXECUTABLE_NAME:-gluesync-automator-macos}"
APP_BUNDLE="${DIST_DIR}/${APP_NAME}.app"
APP_CONTENTS="${APP_BUNDLE}/Contents"
APP_MACOS="${APP_CONTENTS}/MacOS"
APP_RESOURCES="${APP_CONTENTS}/Resources"
APP_AUTOMATOR_PAYLOAD="${APP_RESOURCES}/automator"
APP_VERSION_FILE="${APP_VERSION_FILE:-${ROOT_DIR}/automator_app/VERSION}"
MAIN_BINARY_NAME="${MAIN_BINARY_NAME:-gluesync-automator-macos}"
ICON_SOURCE="${ICON_SOURCE:-${ROOT_DIR}/automator_app/static/web-app-manifest-512x512.png}"
ICON_SOURCE_SVG="${ICON_SOURCE_SVG:-${ROOT_DIR}/automator_app/static/gluesync-bootstrapper.svg}"
ICON_BASENAME="${ICON_BASENAME:-GluesyncAutomator}"
ICON_BASE_SIZE="${ICON_BASE_SIZE:-1024}"

SIGN_IDENTITY="${SIGN_IDENTITY:-}"
ENTITLEMENTS_FILE="${ENTITLEMENTS_FILE:-${ROOT_DIR}/scripts/entitlements.plist}"
SKIP_PIP="${SKIP_PIP:-0}"
NOTARIZE="${NOTARIZE:-0}"
APPLE_ID="${APPLE_ID:-}"
TEAM_ID="${TEAM_ID:-}"
NOTARY_PASSWORD="${NOTARY_PASSWORD:-}"
NOTARY_KEYCHAIN_PROFILE="${NOTARY_KEYCHAIN_PROFILE:-}"

INFO_PLIST_TEMPLATE='<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key>
	<string>%s</string>
	<key>CFBundleDisplayName</key>
	<string>%s</string>
	<key>CFBundleIdentifier</key>
	<string>com.molo17.%s</string>
	<key>CFBundleVersion</key>
	<string>%s</string>
	<key>CFBundleShortVersionString</key>
	<string>%s</string>
	<key>CFBundleExecutable</key>
	<string>%s</string>
	<key>CFBundleIconFile</key>
	<string>%s</string>
	<key>CFBundleIconName</key>
	<string>%s</string>
	<key>CFBundleIconFiles</key>
	<array>
		<string>%s</string>
	</array>
	<key>CFBundleIcons</key>
	<dict>
		<key>CFBundlePrimaryIcon</key>
		<dict>
			<key>CFBundleIconName</key>
			<string>%s</string>
			<key>CFBundleIconFiles</key>
			<array>
				<string>%s</string>
			</array>
		</dict>
	</dict>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>LSMinimumSystemVersion</key>
	<string>11.0</string>
</dict>
</plist>
'

log() {
  printf '==> %s\n' "$*" >&2
}

ensure_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required tool '$1' not found in PATH." >&2
    exit 1
  fi
}

log "Checking required tools"
ensure_tool python3
ensure_tool pip3
ensure_tool pyinstaller
ensure_tool codesign
ensure_tool xcrun
ensure_tool sips
ensure_tool iconutil

APP_VERSION="${APP_VERSION:-1.0}"
if [[ -f "${APP_VERSION_FILE}" ]]; then
  read -r first_line < "${APP_VERSION_FILE}" || true
  first_line="${first_line:-}"
  if [[ -n "${first_line// }" ]]; then
    APP_VERSION="${first_line//[$'\t\r\n ']}"
  fi
fi

if [[ "${SKIP_PIP}" != "1" ]]; then
  log "Installing/upgrading Python dependencies"
  pip3 install -r "${ROOT_DIR}/requirements.txt" --break-system-packages
fi

log "Building PyInstaller bundle using ${PYINSTALLER_SPEC}"
pushd "${ROOT_DIR}" >/dev/null
pyinstaller --noconfirm "${PYINSTALLER_SPEC}"
popd >/dev/null

if [[ -d "${PYINSTALLER_OUTPUT}" ]]; then
  PYINSTALLER_OUTPUT_KIND="dir"
elif [[ -f "${PYINSTALLER_OUTPUT}" ]]; then
  PYINSTALLER_OUTPUT_KIND="file"
else
  echo "Error: expected PyInstaller output at ${PYINSTALLER_OUTPUT}" >&2
  exit 1
fi

if [[ -n "${SIGN_IDENTITY}" && "${PYINSTALLER_OUTPUT_KIND}" == "dir" ]]; then
  log "Codesigning PyInstaller payload contents in ${PYINSTALLER_OUTPUT}"
  mapfile -t _pyinst_files_to_sign < <(
    find "${PYINSTALLER_OUTPUT}" -type f \( -perm -111 -o -name '*.dylib' -o -name '*.so' -o -name 'Python' \)
  )
  if [[ "${#_pyinst_files_to_sign[@]}" -gt 0 ]]; then
    prebundle_codesign_args=(--force --options runtime --sign "${SIGN_IDENTITY}")
    for file in "${_pyinst_files_to_sign[@]}"; do
      codesign "${prebundle_codesign_args[@]}" "${file}"
    done
  else
    log "No executable artifacts found under ${PYINSTALLER_OUTPUT} for codesigning"
  fi
  unset _pyinst_files_to_sign
fi

log "Preparing app bundle structure at ${APP_BUNDLE}"
rm -rf "${APP_BUNDLE}"
mkdir -p "${APP_MACOS}" "${APP_RESOURCES}"

log "Embedding PyInstaller payload"
if [[ "${PYINSTALLER_OUTPUT_KIND}" == "dir" ]]; then
  rsync -a "${PYINSTALLER_OUTPUT}/" "${APP_AUTOMATOR_PAYLOAD}/"
else
  mkdir -p "${APP_AUTOMATOR_PAYLOAD}"
  cp "${PYINSTALLER_OUTPUT}" "${APP_AUTOMATOR_PAYLOAD}/"
fi
PAYLOAD_ENTRY="${APP_AUTOMATOR_PAYLOAD}/${MAIN_BINARY_NAME}"
APP_EXECUTABLE_PATH="${APP_MACOS}/${MAIN_BINARY_NAME}"
cp "${PAYLOAD_ENTRY}" "${APP_EXECUTABLE_PATH}"
rm -rf "${APP_MACOS}/_internal"
ln -s "../Resources/automator/_internal" "${APP_MACOS}/_internal"
ln -sf "../Resources/automator/_internal/base_library.zip" "${APP_MACOS}/base_library.zip"
APP_FRAMEWORKS="${APP_CONTENTS}/Frameworks"
rm -rf "${APP_FRAMEWORKS}"
mkdir -p "${APP_FRAMEWORKS}"
ln -s "../Resources/automator/_internal/Python.framework" "${APP_FRAMEWORKS}/Python.framework"
ln -s "Python.framework/Versions/Current/Python" "${APP_FRAMEWORKS}/Python"

if [[ -n "${SIGN_IDENTITY}" && -f "${APP_EXECUTABLE_PATH}" ]]; then
  log "Codesigning app executable ${APP_EXECUTABLE_PATH}"
  exe_codesign_args=(--force --options runtime --timestamp --sign "${SIGN_IDENTITY}")
  if [[ -n "${ENTITLEMENTS_FILE}" ]]; then
    exe_codesign_args+=(--entitlements "${ENTITLEMENTS_FILE}")
  fi
  codesign "${exe_codesign_args[@]}" "${APP_EXECUTABLE_PATH}"
else
  log "Skipping executable codesign (either SIGN_IDENTITY unset or ${APP_EXECUTABLE_PATH} missing)"
fi

log "Generating Info.plist"
bundle_identifier_stub="$(echo "${APP_NAME}" | tr '[:upper:] ' '[:lower:]_' | tr -cd '[:alnum:]_')"
plist_icon_name=""
plist_icon_file=""
icon_temp_png=""
icon_input_path=""
if [[ -f "${ICON_SOURCE_SVG}" ]]; then
  log "Rendering SVG icon ${ICON_SOURCE_SVG} to ${ICON_BASE_SIZE}px PNG"
  icon_temp_png="$(mktemp "${TMPDIR:-/tmp}/gluesync-svg-$$.XXXXXX.png")"
  SVG_INPUT="${ICON_SOURCE_SVG}" PNG_OUTPUT="${icon_temp_png}" ICON_PX="${ICON_BASE_SIZE}" python3 <<'PY'
import os
from cairosvg import svg2png

svg_path = os.environ["SVG_INPUT"]
png_path = os.environ["PNG_OUTPUT"]
size = int(os.environ["ICON_PX"])
svg2png(url=svg_path, write_to=png_path, output_width=size, output_height=size)
PY
  icon_input_path="${icon_temp_png}"
elif [[ -f "${ICON_SOURCE}" ]]; then
  icon_input_path="${ICON_SOURCE}"
fi

if [[ -n "${icon_input_path}" ]]; then
  log "Generating .icns icon from ${icon_input_path}"
  iconset_dir="$(mktemp -d "${TMPDIR:-/tmp}/gluesync-icon.XXXXXX.iconset")"
  declare -a base_sizes=(16 32 64 128 256 512)
  for size in "${base_sizes[@]}"; do
    target="${iconset_dir}/icon_${size}x${size}.png"
    sips -s format png -z "${size}" "${size}" "${icon_input_path}" --out "${target}" >/dev/null
    retina_size=$((size * 2))
    retina_target="${iconset_dir}/icon_${size}x${size}@2x.png"
    sips -s format png -z "${retina_size}" "${retina_size}" "${icon_input_path}" --out "${retina_target}" >/dev/null
  done
  ICON_FILE="${APP_RESOURCES}/${ICON_BASENAME}.icns"
  iconutil -c icns -o "${ICON_FILE}" "${iconset_dir}" >/dev/null
  rm -rf "${iconset_dir}"
  plist_icon_name="${ICON_BASENAME}"
  plist_icon_file="${ICON_BASENAME}.icns"
  if [[ -n "${icon_temp_png}" && -f "${icon_temp_png}" ]]; then
    rm -f "${icon_temp_png}"
  fi
else
  log "Icon sources ${ICON_SOURCE_SVG} / ${ICON_SOURCE} not found; skipping icon generation"
fi
printf "${INFO_PLIST_TEMPLATE}" \
  "${APP_NAME}" \
  "${APP_NAME}" \
  "${bundle_identifier_stub}" \
  "${APP_VERSION}" \
  "${APP_VERSION}" \
  "${APP_EXECUTABLE_NAME}" \
  "${plist_icon_file}" \
  "${plist_icon_name}" \
  "${plist_icon_name}" \
  "${plist_icon_name}" \
  "${plist_icon_name}" \
  > "${APP_CONTENTS}/Info.plist"

if [[ -n "${SIGN_IDENTITY}" ]]; then
  log "Codesigning app bundle with identity: ${SIGN_IDENTITY}"
  codesign_args=(--force --options runtime --timestamp --sign "${SIGN_IDENTITY}")
  if [[ -n "${ENTITLEMENTS_FILE}" ]]; then
    codesign_args+=(--entitlements "${ENTITLEMENTS_FILE}")
  fi
  codesign "${codesign_args[@]}" "${APP_BUNDLE}"
  log "Verifying code signature"
  codesign --verify --strict --verbose=2 "${APP_BUNDLE}"
else
  log "Skipping codesign step (SIGN_IDENTITY not set)"
fi

log "Clearing extended attributes and registering bundle locally"
xattr -cr "${APP_BUNDLE}" || true
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "${APP_BUNDLE}" >/dev/null 2>&1 || true
touch "${APP_BUNDLE}"

if [[ "${NOTARIZE}" == "1" ]]; then
  ZIP_PATH="${DIST_DIR}/$(echo "${APP_NAME}" | tr ' ' '-').zip"
  log "Creating notarization archive at ${ZIP_PATH}"
  rm -f "${ZIP_PATH}"
  ditto -ck --rsrc --sequesterRsrc --keepParent "${APP_BUNDLE}" "${ZIP_PATH}"

  NOTARY_ARGS=(submit "${ZIP_PATH}" --wait)
  if [[ -n "${NOTARY_KEYCHAIN_PROFILE}" ]]; then
    NOTARY_ARGS+=(--keychain-profile "${NOTARY_KEYCHAIN_PROFILE}")
  else
    if [[ -z "${APPLE_ID}" || -z "${TEAM_ID}" || -z "${NOTARY_PASSWORD}" ]]; then
      echo "Error: NOTARIZE=1 requires either NOTARY_KEYCHAIN_PROFILE or APPLE_ID/TEAM_ID/NOTARY_PASSWORD." >&2
      exit 1
    fi
    NOTARY_ARGS+=(--apple-id "${APPLE_ID}" --team-id "${TEAM_ID}" --password "${NOTARY_PASSWORD}")
  fi

  log "Submitting for notarization"
  xcrun notarytool "${NOTARY_ARGS[@]}"

  log "Stapling notarization ticket"
  xcrun stapler staple "${APP_BUNDLE}"
else
  log "Skipping notarization (NOTARIZE != 1)"
fi

log "Done. App available at ${APP_BUNDLE}"
