# shellcheck shell=sh
# build-cache.sh: tgz caches for GitLab CI on the HQ build cache (k8s-prod app build-cache).
# Canonical copy: k8s-gitops apps/build-cache/client/build-cache.sh. Projects vendor it as ci/build-cache.sh.
#
#   . ci/build-cache.sh
#   key=$(bc_key package-lock.json)            # content hash of files (+ $BC_KEY_EXTRA, arch)
#   bc_restore "node_modules-$key" || { npm ci && bc_save "node_modules-$key" node_modules; }
#
# Trust model (enforced by the server, not by this script):
#   - protected refs write  $BUILD_CACHE_URL/ci-cache/<project>/<key>.tgz    (user ci-cache, protected var)
#   - other refs write      $BUILD_CACHE_URL/ci-cache-mr/<project>/<key>.tgz (user ci-cache-mr), 7-day TTL
#   - protected refs read only ci-cache; other refs read ci-cache, then ci-cache-mr.
# Use content-hash keys (lockfiles, toolchain versions), never branch names, so every branch shares entries.
# A miss, an unreachable server or a failed upload never fails the job. Reads are anonymous.
# Needs curl, tar, gzip and sha256sum (or shasum). Group CI variables: BUILD_CACHE_URL, BUILD_CACHE_PASSWORD,
# BUILD_CACHE_MR_PASSWORD. Entries expire 14 days after they were written.

BUILD_CACHE_URL="${BUILD_CACHE_URL:-https://nexus.hq.molo17.com/repository}"
_bc_project="${BC_NAMESPACE:-${CI_PROJECT_PATH_SLUG:-local}}"

_bc_sha() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -c1-16; else shasum -a 256 | cut -c1-16; fi
}

# bc_key <file|dir>...: 16-hex content hash of the given files (dirs: every file in them, sorted), plus
# $BC_KEY_EXTRA and the machine arch. Missing paths count as empty, so the key is stable.
bc_key() {
  {
    for _p in "$@"; do
      if [ -d "$_p" ]; then find "$_p" -type f | LC_ALL=C sort | while IFS= read -r _f; do echo "$_f"; cat "$_f"; done
      elif [ -f "$_p" ]; then echo "$_p"; cat "$_p"; else echo "$_p:absent"; fi
    done
    echo "${BC_KEY_EXTRA:-}"; uname -m
  } | _bc_sha
}

_bc_trusted() { [ "${CI_COMMIT_REF_PROTECTED:-false}" = "true" ]; }

# bc_restore <key> [dir]: extract <key>.tgz into dir (default .). Returns 1 on a miss.
bc_restore() {
  _key=$1; _dir=${2:-.}
  _tmp="${TMPDIR:-/tmp}/bc-$$-$_key.tgz"
  _repos="ci-cache"
  _bc_trusted || _repos="ci-cache ci-cache-mr"
  for _r in $_repos; do
    if curl -fsS --retry 2 --connect-timeout 10 -o "$_tmp" "$BUILD_CACHE_URL/$_r/$_bc_project/$_key.tgz" 2>/dev/null; then
      mkdir -p "$_dir"
      if tar -xzf "$_tmp" -C "$_dir"; then
        rm -f "$_tmp"; echo "build-cache: hit $_key ($_r)"; return 0
      fi
    fi
  done
  rm -f "$_tmp"; echo "build-cache: miss $_key"; return 1
}

# bc_save <key> <path>...: tar the paths (relative to the current dir) and upload, unless the entry exists.
bc_save() {
  _key=$1; shift
  if _bc_trusted; then _r=ci-cache; _u=ci-cache; _pw=${BUILD_CACHE_PASSWORD:-}
  else _r=ci-cache-mr; _u=ci-cache-mr; _pw=${BUILD_CACHE_MR_PASSWORD:-}; fi
  [ -n "$_pw" ] || { echo "build-cache: upload of $_key skipped (no credentials for $_r)"; return 0; }
  _url="$BUILD_CACHE_URL/$_r/$_bc_project/$_key.tgz"
  if curl -fsS -I --connect-timeout 10 "$_url" >/dev/null 2>&1; then echo "build-cache: $_key already in $_r"; return 0; fi
  # Never upload an entry that is missing a path or was not written completely: a bad trusted entry would be
  # restored by every later build. tar writes to a file (no pipe, so a tar failure is seen even without pipefail).
  for _p in "$@"; do
    [ -e "$_p" ] || { echo "build-cache: upload of $_key skipped ($_p missing)"; return 0; }
  done
  _tmp="${TMPDIR:-/tmp}/bc-$$-$_key.tar"
  { tar -cf "$_tmp" "$@" && gzip -1 -f "$_tmp" && _tmp="$_tmp.gz" && gzip -t "$_tmp" \
    && curl -fsS --retry 2 --connect-timeout 10 -u "$_u:$_pw" --upload-file "$_tmp" "$_url" \
    && echo "build-cache: saved $_key to $_r ($(du -h "$_tmp" | cut -f1))"; } \
    || echo "build-cache: upload of $_key failed (ignored)"
  rm -f "${TMPDIR:-/tmp}/bc-$$-$_key.tar" "${TMPDIR:-/tmp}/bc-$$-$_key.tar.gz"
  return 0
}
