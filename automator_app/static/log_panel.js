/** Execution log panel: structured lines, filters, follow, copy/download. */
const LOG_TZ = 'Europe/Rome';
const LOG_LEVEL_RANK = { debug: 10, info: 20, warning: 30, error: 40, critical: 50 };
const LEVEL_INFO = 'info';
const ORIGIN_SERVER = 'server';
const ORIGIN_CLIENT = 'client';
const BTN_COPY = 'Copy';
const NL = '\n';

export function formatLogTs(date = new Date()) {
  try {
    const parts = new Intl.DateTimeFormat('en-GB', {
      timeZone: LOG_TZ,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).formatToParts(date);
    const get = (t) => parts.find((p) => p.type === t)?.value || '';
    return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}:${get('second')} ${LOG_TZ}`;
  } catch (err) {
    console.warn('log timestamp format failed', err);
    return `${date.toISOString()} UTC`;
  }
}

function normalizeLevel(level) {
  const raw = String(level || LEVEL_INFO).toLowerCase();
  if (raw === 'warn') return 'warning';
  if (LOG_LEVEL_RANK[raw] != null) return raw;
  return LEVEL_INFO;
}

function coerceLogEntry(raw, origin = ORIGIN_SERVER) {
  if (raw && typeof raw === 'object' && typeof raw.message === 'string') {
    return {
      ts: raw.ts || formatLogTs(),
      level: normalizeLevel(raw.level),
      scope: raw.scope || '',
      message: raw.message,
      origin,
    };
  }
  const text = String(raw ?? '').trim();
  if (!text) return null;
  return { ts: formatLogTs(), level: LEVEL_INFO, scope: '', message: text, origin };
}

function entryPlain(entry) {
  const scope = entry.scope ? `[${entry.scope}] ` : '';
  return `${entry.ts} ${entry.level.toUpperCase()} ${scope}${entry.message}`.trim();
}

/**
 * @param {{
 *   logOutput: HTMLElement | null,
 *   logTemplate: HTMLTemplateElement | null,
 *   logFollowEl?: HTMLInputElement | null,
 *   logLevelEl?: HTMLSelectElement | null,
 *   logSearchEl?: HTMLInputElement | null,
 *   logCopyBtn?: HTMLElement | null,
 *   logDownloadBtn?: HTMLElement | null,
 * }} els
 */
function pickServerRows(run) {
  if (Array.isArray(run.logEntries)) return run.logEntries;
  if (Array.isArray(run.log_entries)) return run.log_entries;
  if (Array.isArray(run.logs)) return run.logs;
  return [];
}

export function createLogPanel(els) {
  const {
    logOutput,
    logTemplate,
    logFollowEl,
    logLevelEl,
    logSearchEl,
    logCopyBtn,
    logDownloadBtn,
  } = els;

  const logState = {
    runId: null,
    serverCount: 0,
    follow: true,
    minLevel: 'all',
    search: '',
    entries: [],
  };

  function passesFilters(entry) {
    if (logState.minLevel !== 'all') {
      const need = LOG_LEVEL_RANK[logState.minLevel] || 0;
      if ((LOG_LEVEL_RANK[entry.level] || 0) < need) return false;
    }
    const q = logState.search;
    if (q) {
      const hay = `${entry.ts} ${entry.level} ${entry.scope} ${entry.message}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function makeLogNode(entry) {
    let node;
    if (logTemplate?.content?.firstElementChild) {
      node = logTemplate.content.firstElementChild.cloneNode(true);
    } else {
      node = document.createElement('div');
      node.className = 'log-line';
      node.innerHTML = '<span class="log-line-ts"></span><span class="log-line-level"></span>'
        + '<span class="log-line-scope"></span><span class="log-line-msg"></span>';
    }
    const level = normalizeLevel(entry.level);
    node.classList.add(`log-line--${level}`);
    node.dataset.level = level;
    const tsEl = node.querySelector('.log-line-ts');
    const levelEl = node.querySelector('.log-line-level');
    const scopeEl = node.querySelector('.log-line-scope');
    const msgEl = node.querySelector('.log-line-msg');
    if (tsEl) tsEl.textContent = entry.ts;
    if (levelEl) levelEl.textContent = level;
    if (scopeEl) scopeEl.textContent = entry.scope ? `[${entry.scope}]` : '';
    if (msgEl) msgEl.textContent = entry.message;
    if (!passesFilters(entry)) node.classList.add('is-hidden-log');
    return node;
  }

  function scrollLogToBottom() {
    if (!logOutput || !logState.follow) return;
    logOutput.scrollTop = logOutput.scrollHeight;
  }

  function appendLogEntry(entry) {
    if (!logOutput || !entry?.message) return;
    logState.entries.push(entry);
    logOutput.appendChild(makeLogNode(entry));
    scrollLogToBottom();
  }

  function appendLogLine(text, meta = {}) {
    const entry = coerceLogEntry({
      ts: meta.ts || formatLogTs(),
      level: meta.level || LEVEL_INFO,
      scope: meta.scope || '',
      message: typeof text === 'string' ? text : String(text ?? ''),
    }, meta.origin || ORIGIN_CLIENT);
    if (!entry) return;
    appendLogEntry(entry);
  }

  function clearLogPanel() {
    logState.runId = null;
    logState.serverCount = 0;
    logState.entries = [];
    if (logOutput) logOutput.innerHTML = '';
  }

  function reapplyLogFilters() {
    if (!logOutput) return;
    const nodes = logOutput.querySelectorAll('.log-line');
    nodes.forEach((node, idx) => {
      const entry = logState.entries[idx];
      if (!entry) return;
      node.classList.toggle('is-hidden-log', !passesFilters(entry));
    });
  }

  function renderLogs(logs = []) {
    if (!logOutput) return;
    clearLogPanel();
    (logs || []).forEach((row) => {
      const entry = coerceLogEntry(row, ORIGIN_SERVER);
      if (entry) appendLogEntry(entry);
    });
    logState.serverCount = logState.entries.length;
  }

  function appendServerRows(rows, fromIdx) {
    for (let i = fromIdx; i < rows.length; i += 1) {
      const entry = coerceLogEntry(rows[i], ORIGIN_SERVER);
      if (entry) appendLogEntry(entry);
    }
    logState.serverCount = rows.length;
  }

  function rebuildServerRows(runId, rows) {
    const clientTail = logState.entries.filter((e) => e.origin === ORIGIN_CLIENT);
    clearLogPanel();
    logState.runId = runId;
    appendServerRows(rows, 0);
    clientTail.forEach((e) => appendLogEntry({ ...e }));
  }

  function syncServerLogs(run) {
    if (!run) return;
    const runId = run.id || run.runId || null;
    const rows = pickServerRows(run);

    if (runId && runId !== logState.runId) {
      clearLogPanel();
      logState.runId = runId;
    } else if (!runId && logState.serverCount && rows.length === 0) {
      return;
    }

    if (rows.length < logState.serverCount) {
      rebuildServerRows(runId, rows);
      return;
    }
    appendServerRows(rows, logState.serverCount);
  }

  function exportLogText() {
    return logState.entries.filter(passesFilters).map(entryPlain).join(NL);
  }

  if (logOutput) {
    logOutput.addEventListener('scroll', () => {
      const distance = logOutput.scrollHeight - logOutput.scrollTop - logOutput.clientHeight;
      const atBottom = distance < 24;
      if (atBottom) {
        logState.follow = !logFollowEl || logFollowEl.checked;
        return;
      }
      if (logState.follow) {
        logState.follow = false;
        if (logFollowEl) logFollowEl.checked = false;
      }
    });
  }
  if (logFollowEl) {
    logFollowEl.addEventListener('change', () => {
      logState.follow = !!logFollowEl.checked;
      if (logState.follow) scrollLogToBottom();
    });
  }
  if (logLevelEl) {
    logLevelEl.addEventListener('change', () => {
      logState.minLevel = logLevelEl.value || 'all';
      reapplyLogFilters();
    });
  }
  if (logSearchEl) {
    logSearchEl.addEventListener('input', () => {
      logState.search = (logSearchEl.value || '').trim().toLowerCase();
      reapplyLogFilters();
    });
  }
  if (logCopyBtn) {
    logCopyBtn.addEventListener('click', async () => {
      const body = exportLogText();
      try {
        await navigator.clipboard.writeText(body);
        logCopyBtn.textContent = 'Copied';
        setTimeout(() => { logCopyBtn.textContent = BTN_COPY; }, 1500);
      } catch (err) {
        console.warn('log copy failed', err);
        logCopyBtn.textContent = 'Copy failed';
        setTimeout(() => { logCopyBtn.textContent = BTN_COPY; }, 1500);
      }
    });
  }
  if (logDownloadBtn) {
    logDownloadBtn.addEventListener('click', () => {
      const body = exportLogText();
      const blob = new Blob([body], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `automator-log-${new Date().toISOString().replace(/[:.]/g, '')}.txt`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    });
  }

  return {
    renderLogs,
    appendLogLine,
    syncServerLogs,
    clearLogPanel,
    formatLogTs,
  };
}
