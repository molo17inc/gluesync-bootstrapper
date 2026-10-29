# Nexus raw cache helpers for the Linux CI jobs. Source it:  . scripts/ci/nexus-cache.sh
#
# GitLab has no shared cache server here (cache: is local to each runner host), so the cross-runner cache is the Nexus
# raw repository ui-build-cache, the same one the UI monorepo uses. Entries of this project live under
# gluesync-bootstrapper/. Nexus drops an entry that is not downloaded for 14 days.
#
# Nothing here ever fails a job: a miss, a failed upload or an empty $NEXUS_REPO_PASSWORD just means "no cache".
# The pip caches are content addressed (a hash of the requirements and of the Python/arch), so they are shared by every
# ref and need no fallback to the default branch.

NEXUS_CACHE="${NEXUS_CACHE:-https://nexus.hq.molo17.com/repository/ui-build-cache/gluesync-bootstrapper/shared}"

# cache_get NAME: restore NAME.tgz into $CI_PROJECT_DIR. Returns 0 on a hit, 1 on a miss or when the cache is off.
cache_get() {
  [ -n "${NEXUS_REPO_PASSWORD:-}" ] || return 1
  if curl -fsS --retry 2 --max-time 120 -u "gluesync:$NEXUS_REPO_PASSWORD" "$NEXUS_CACHE/$1.tgz" -o "/tmp/$1.tgz" 2>/dev/null \
    && tar -xzf "/tmp/$1.tgz" -C "$CI_PROJECT_DIR" 2>/dev/null; then
    echo "cache $1 <- nexus"
    rm -f "/tmp/$1.tgz"
    return 0
  fi
  rm -f "/tmp/$1.tgz"
  echo "cache $1: miss"
  return 1
}

# cache_put NAME PATH...: pack PATHs (relative to $CI_PROJECT_DIR) and upload them as NAME.tgz.
cache_put() {
  [ -n "${NEXUS_REPO_PASSWORD:-}" ] || return 0
  n=$1; shift
  { tar -czf "/tmp/$n.tgz" -C "$CI_PROJECT_DIR" "$@" 2>/dev/null \
    && curl -fsS --retry 2 --max-time 300 -u "gluesync:$NEXUS_REPO_PASSWORD" --upload-file "/tmp/$n.tgz" "$NEXUS_CACHE/$n.tgz" >/dev/null 2>&1 \
    && echo "cache $n -> nexus"; } || echo "cache $n: upload skipped"
  rm -f "/tmp/$n.tgz"
  return 0
}

# pip_install_cached PIP_ARGS...: pip install PIP_ARGS with the pip cache restored from (and, on a miss, saved to) Nexus.
# The index is $PIP_INDEX_URL (the Nexus PyPI proxy). Every argument that is a file (the -r requirements) goes into the key.
pip_install_cached() {
  export PIP_CACHE_DIR="$CI_PROJECT_DIR/.pip-cache"
  pip_key=$({ python3 --version; uname -m; echo "$*"; for a in "$@"; do if [ -f "$a" ]; then cat "$a"; fi; done; } | sha256sum | cut -c1-16)
  pip_name="pip-$pip_key"
  pip_hit=0
  cache_get "$pip_name" && pip_hit=1
  pip install "$@"
  if [ "$pip_hit" = 0 ] && [ -d "$PIP_CACHE_DIR" ]; then
    cache_put "$pip_name" .pip-cache
  fi
}
