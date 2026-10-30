/** Execution log panel: structured lines, filters, follow, copy/download. */
const LOG_TZ = 'Europe/Rome';
const LOG_LEVEL_RANK = { debug: 10, info: 20, warning: 30, error: 40, critical: 50 };
const LEVEL_INFO = 'info';
const ORIGIN_SERVER = 'server';
const ORIGIN_CLIENT = 'client';
const BTN_COPY = 'Copy';
const NL = '\n';

const MAX_HISTORY = 12;
const HINT_RULES = [
  {
    id: 'ddl',
    test: /DDL|CREATE TABLE|create table|syntax error|does not exist|ORA-|SQLSTATE|jdbc/i,
    text: 'DDL / create-table issue: check target type (SQL vs NoSQL), schema name, and that Create missing tables is on.',
  },
  {
    id: 'masked',
    test: /masked|\*{7}|agent_passwords|password.*(required|missing|invalid)/i,
    text: 'Masked password: open Customize, enter the real SOURCE/TARGET passwords, then Preview before Import.',
  },
  {
    id: 'exists',
    test: /already exists|duplicate|onExisting|on-existing|conflict|EntityAlready/i,
    text: 'Already exists: set On existing entity to skip or update, or pick a new target table name.',
  },
];

function parseLinkedIds(message) {
  const text = String(message);
  const entity = /\b(?:entity|Entity)\s+["']?([A-Za-z0-9_.@:-]{2,80})/.exec(text);
  const pipeline = /\b(?:pipeline|Pipeline)\s+["']?([A-Za-z0-9_.@:-]{2,80})/.exec(text);
  const schemaTable = /\b([A-Za-z_]\w*)\.(?:[A-Za-z_][\w@]*)\b/.exec(text);
  let entityId = null;
  if (entity) entityId = entity[1];
  else if (schemaTable) entityId = schemaTable[0];
  return {
    entityId,
    pipelineId: pipeline ? pipeline[1] : null,
  };
}

function buildHints(entries) {
  const hit = new Set();
  const out = [];
  entries.forEach((entry) => {
    if ((LOG_LEVEL_RANK[entry.level] || 0) < LOG_LEVEL_RANK.warning) return;
    HINT_RULES.forEach((rule) => {
      if (hit.has(rule.id)) return;
      if (rule.test.test(entry.message)) {
        hit.add(rule.id);
        out.push(rule.text);
      }
    });
  });
  return out;
}

function countLevels(entries) {
  const counts = { debug: 0, info: 0, warning: 0, error: 0, critical: 0 };
  entries.forEach((e) => {
    const level = normalizeLevel(e.level);
    if (counts[level] != null) counts[level] += 1;
  });
  return counts;
}

function entityOutcomes(entries) {
  const map = new Map();
  entries.forEach((entry) => {
    const linked = entry.entityId || parseLinkedIds(entry.message).entityId;
    if (!linked) return;
    const lower = entry.message.toLowerCase();
    let outcome = 'info';
    if (/fail|error|exception/.test(lower) || (LOG_LEVEL_RANK[entry.level] || 0) >= LOG_LEVEL_RANK.error) {
      outcome = 'failed';
    } else if (/skip|already exists|unchanged/.test(lower)) {
      outcome = 'skipped';
    } else if (/creat|success|imported|done|\bok\b/.test(lower)) {
      outcome = 'ok';
    }
    const prev = map.get(linked);
    if (!prev || outcome === 'failed' || (outcome === 'ok' && prev !== 'failed')) {
      map.set(linked, outcome);
    }
  });
  return [...map.entries()].map(([id, outcome]) => ({ id, outcome }));
}

function focusEntityOrPipeline(kind, id) {
  if (kind === 'pipeline') {
    const sel = document.getElementById('pipeline-id');
    if (!sel) return;
    const opt = [...sel.options].find((o) => o.value === id || o.textContent.includes(id));
    if (opt) {
      sel.value = opt.value;
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    }
    sel.scrollIntoView({ behavior: 'smooth', block: 'center' });
    return;
  }
  const root = document.getElementById('entity-customize-root');
  const row = root?.querySelector(`[data-entity-id="${CSS.escape(id)}"], [data-entity="${CSS.escape(id)}"]`);
  if (!row) return;
  row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  row.classList.add('log-link-flash');
  setTimeout(() => row.classList.remove('log-link-flash'), 1600);
}


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
 *   logSummaryEl?: HTMLElement | null,
 *   logHintsEl?: HTMLElement | null,
 *   logHistoryEl?: HTMLSelectElement | null,
 *   logCollapseBtn?: HTMLElement | null,
 *   logCardEl?: HTMLElement | null,
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
    logSummaryEl,
    logHintsEl,
    logHistoryEl,
    logCollapseBtn,
    logCardEl,
  } = els;

  const logState = {
    runId: null,
    serverCount: 0,
    follow: true,
    minLevel: 'all',
    search: '',
    entries: [],
    startedAt: null,
    history: [],
  };

  function refreshSummary() {
    if (!logSummaryEl) return;
    const counts = countLevels(logState.entries);
    const outcomes = entityOutcomes(logState.entries);
    let duration = '';
    if (logState.startedAt) {
      const ms = Date.now() - logState.startedAt;
      duration = ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
    }
    const parts = [
      `info ${counts.info}`,
      `warn ${counts.warning}`,
      `error ${counts.error + counts.critical}`,
    ];
    if (duration) parts.push(`· ${duration}`);
    if (outcomes.length) {
      const ok = outcomes.filter((o) => o.outcome === 'ok').length;
      const failed = outcomes.filter((o) => o.outcome === 'failed').length;
      const skipped = outcomes.filter((o) => o.outcome === 'skipped').length;
      parts.push(`· entities ok ${ok} / skip ${skipped} / fail ${failed}`);
    }
    logSummaryEl.textContent = parts.join('  ');
    logSummaryEl.hidden = logState.entries.length === 0;
    if (logHintsEl) {
      const hints = buildHints(logState.entries);
      logHintsEl.replaceChildren();
      hints.forEach((hint) => {
        const li = document.createElement('li');
        li.textContent = hint;
        logHintsEl.appendChild(li);
      });
      logHintsEl.hidden = hints.length === 0;
    }
  }

  function snapshotHistory() {
    if (!logState.entries.length) return;
    const label = `${new Date().toLocaleTimeString([], { hour12: false })} · ${logState.entries.length} lines`
      + (logState.runId ? ` · ${String(logState.runId).slice(0, 8)}` : '');
    logState.history.unshift({
      label,
      runId: logState.runId,
      entries: logState.entries.map((e) => ({ ...e })),
      startedAt: logState.startedAt,
    });
    if (logState.history.length > MAX_HISTORY) logState.history.length = MAX_HISTORY;
    if (logHistoryEl) {
      logHistoryEl.replaceChildren(new Option('Current run', ''));
      logState.history.forEach((h, idx) => {
        logHistoryEl.appendChild(new Option(h.label, String(idx)));
      });
      logHistoryEl.hidden = false;
    }
  }

  function showHistoryRun(idx) {
    const item = logState.history[idx];
    if (!item || !logOutput) return;
    logOutput.innerHTML = '';
    logState.entries = item.entries.map((e) => ({ ...e }));
    logState.runId = item.runId;
    logState.startedAt = item.startedAt;
    logState.serverCount = 0;
    logState.entries.forEach((e) => logOutput.appendChild(makeLogNode(e)));
    refreshSummary();
  }

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
    if (msgEl) {
      msgEl.textContent = '';
      msgEl.appendChild(document.createTextNode(entry.message));
      const linked = parseLinkedIds(entry.message);
      if (linked.entityId) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'log-line-link';
        btn.textContent = 'entity';
        btn.title = `Open ${linked.entityId}`;
        btn.addEventListener('click', () => focusEntityOrPipeline('entity', linked.entityId));
        msgEl.appendChild(document.createTextNode(' '));
        msgEl.appendChild(btn);
      }
      if (linked.pipelineId) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'log-line-link';
        btn.textContent = 'pipeline';
        btn.title = `Select pipeline ${linked.pipelineId}`;
        btn.addEventListener('click', () => focusEntityOrPipeline('pipeline', linked.pipelineId));
        msgEl.appendChild(document.createTextNode(' '));
        msgEl.appendChild(btn);
      }
    }
    if (!passesFilters(entry)) node.classList.add('is-hidden-log');
    return node;
  }

  function scrollLogToBottom() {
    if (!logOutput || !logState.follow) return;
    logOutput.scrollTop = logOutput.scrollHeight;
  }

  function appendLogEntry(entry) {
    if (!logOutput || !entry?.message) return;
    if (!logState.entries.length) logState.startedAt = Date.now();
    logState.entries.push(entry);
    logOutput.appendChild(makeLogNode(entry));
    scrollLogToBottom();
    refreshSummary();
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
    snapshotHistory();
    logState.runId = null;
    logState.serverCount = 0;
    logState.entries = [];
    logState.startedAt = null;
    if (logOutput) logOutput.innerHTML = '';
    refreshSummary();
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

  if (logCollapseBtn && logCardEl) {
    logCollapseBtn.addEventListener('click', () => {
      const collapsed = logCardEl.classList.toggle('log-card--collapsed');
      logCollapseBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      logCollapseBtn.textContent = collapsed ? 'Expand' : 'Collapse';
    });
  }
  if (logHistoryEl) {
    logHistoryEl.addEventListener('change', () => {
      const val = logHistoryEl.value;
      if (val === '') return;
      showHistoryRun(Number(val));
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
