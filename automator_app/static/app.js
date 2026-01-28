const api = {
  async getState() {
    const res = await fetch('/api/state');
    if (!res.ok) throw new Error(`State request failed: ${res.status}`);
    return res.json();
  },
  async getCorehubOverview() {
    const res = await fetch('/api/corehub/overview');
    if (res.status === 401) return null;
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || 'Failed to load CoreHub overview');
    }
    return res.json();
  },
  async exportPipelineFull(pipelineId) {
    const response = await fetch(`/api/export/pipeline/${encodeURIComponent(pipelineId)}/full`);
    if (!response.ok) {
      throw new Error(`Full backup export failed: ${response.status} ${response.statusText}`);
    }
    const blob = await response.blob();
    const filename = response.headers.get('Content-Disposition')?.split('filename=')[1]?.replace(/"/g, '') || `pipeline_${pipelineId}_backup.zip`;
    return { blob, filename };
  },
  async login(payload) {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || 'Authentication failed');
    }
    return res.json();
  },
  async logout() {
    const res = await fetch('/api/logout', { method: 'POST' });
    if (!res.ok) throw new Error('Logout failed');
    return res.json();
  },
  async uploadYaml(file) {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch('/api/upload', {
      method: 'POST',
      body: form,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || 'Upload failed');
    }
    return res.json();
  },
  async startRun(payload) {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || 'Run start failed');
    }
    return res.json();
  },
  async getCurrentRun(includeLogs = false) {
    const res = await fetch(`/api/run/current${includeLogs ? '?include_logs=true' : ''}`);
    if (!res.ok) throw new Error('Failed to fetch run status');
    return res.json();
  },
  async listPipelines() {
    const res = await fetch('/api/pipelines');
    if (!res.ok) throw new Error('Failed to list pipelines');
    return res.json();
  },
  async bulkListSchemas(pipelineId) {
    const res = await fetch(`/api/bulk/schemas?pipelineId=${encodeURIComponent(pipelineId)}`);
    if (!res.ok) throw new Error('Failed to list source schemas');
    return res.json();
  },
  async bulkListTables(pipelineId, schema) {
    const res = await fetch(`/api/bulk/tables?pipelineId=${encodeURIComponent(pipelineId)}&schema=${encodeURIComponent(schema)}`);
    if (!res.ok) throw new Error('Failed to list source tables');
    return res.json();
  },
  async bulkCreate(payload) {
    const res = await fetch('/api/bulk/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || 'Bulk entity creation failed');
    }
    return res.json();
  },
  async bulkTemplate(payload) {
    const res = await fetch('/api/bulk/template', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || 'Bulk template export failed');
    }
    const blob = await res.blob();
    const filename = res.headers.get('Content-Disposition')?.split('filename=')[1]?.replace(/"/g, '')
      || 'template.yaml';
    return { blob, filename };
  },
  async exportPipeline(pipelineId) {
    const response = await fetch(`/api/export/pipeline/${encodeURIComponent(pipelineId)}`);
    if (!response.ok) {
      throw new Error(`Export failed: ${response.status} ${response.statusText}`);
    }
    const blob = await response.blob();
    const filename = response.headers.get('Content-Disposition')?.split('filename=')[1]?.replace(/"/g, '') || 'backup.yaml';
    return { blob, filename };
  },
  async exportAllPipelines() {
    const response = await fetch('/api/export/all-pipelines');
    if (!response.ok) {
      throw new Error(`Export all failed: ${response.status} ${response.statusText}`);
    }
    const blob = await response.blob();
    const filename = response.headers.get('Content-Disposition')?.split('filename=')[1]?.replace(/"/g, '') || 'pipeline_backups.zip';
    return { blob, filename };
  },
  async importConfig(file) {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch('/api/import/config', {
      method: 'POST',
      body: form,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || 'Config import failed');
    }
    return res.json();
  },
  async importAll(file, autoDeployAgents = true) {
    const form = new FormData();
    form.append('file', file);
    form.append('auto_deploy_agents', autoDeployAgents ? 'true' : 'false');
    
    // Get current CoreHub URL from state and derive Conductor URL
    const stateRes = await fetch('/api/state');
    if (stateRes.ok) {
      const state = await stateRes.json();
      if (state.baseUrl) {
        // Derive conductor URL: same base URL + /conductor path
        const conductorUrl = state.baseUrl.replace(/\/+$/, '') + '/conductor';
        form.append('conductor_url', conductorUrl);
      }
    }
    
    const res = await fetch('/api/import/all', {
      method: 'POST',
      body: form,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || 'Import all failed');
    }
    return res.json();
  },
  async validateAll(file) {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch('/api/import/validate-all', {
      method: 'POST',
      body: form,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || 'Validation failed');
    }
    return res.json();
  },
  async duplicatePipeline(payload, options = {}) {
    const { signal } = options;
    const requestInit = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    };
    if (signal) {
      requestInit.signal = signal;
    }
    const res = await fetch(`/api/duplicate/pipeline/${encodeURIComponent(payload.pipelineId)}`, requestInit);
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || 'Pipeline duplication failed');
    }
    return res.json();
  },
  async cancelDuplicate() {
    const res = await fetch('/api/duplicate/cancel', { method: 'POST' });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || 'Cancel duplicate failed');
    }
    return res.json();
  },
};

const uiState = {
  flags: {},
};

function applyUiFlags(flags = {}) {
  uiState.flags = { ...flags };
  const body = document.body;
  if (!body) return;

  const mapping = {
    iframeMode: 'iframe-mode',
    hideHeader: 'hide-header',
    hideEnvironment: 'hide-corehub-environment',
    hideCorehubTab: 'hide-corehub-tab',
  };

  Object.entries(mapping).forEach(([flag, className]) => {
    body.classList.toggle(className, !!flags[flag]);
  });

  if (window.automatorTabs?.hideTab) {
    window.automatorTabs.hideTab('corehub', !!flags.hideCorehubTab);
  }
}

const ui = (() => {
  const statusEl = document.getElementById('status-indicator');
  const authMessageEl = document.getElementById('auth-message');
  const configMessageEl = document.getElementById('config-message');
  const exportMessageEl = document.getElementById('export-message');
  const bulkMessageEl = document.getElementById('bulk-message');
  const duplicateMessageEl = document.getElementById('duplicate-message');
  const logoutBtn = document.getElementById('logout-btn');
  const runBtn = document.getElementById('run-btn');
  const exportBtn = document.getElementById('export-btn');
  const exportPipelineFullBtn = document.getElementById('export-pipeline-full-btn');
  const exportAllBtn = document.getElementById('export-all-btn');
  const validateAllBtn = document.getElementById('validate-all-btn');
  const importConfigBtn = document.getElementById('import-config-btn');
  const importAllBtn = document.getElementById('import-all-btn');
  const logOutput = document.getElementById('log-output');
  const logTemplate = document.getElementById('log-line-template');
  const footerYear = document.getElementById('footer-year');
  const versionLabel = document.getElementById('version-label');
  const corehubUrlInput = document.getElementById('corehub-url');
  const usernameInput = document.getElementById('username');
  const passwordInput = document.getElementById('password');

  footerYear.textContent = new Date().getFullYear();

  // Hide actions that depend on user-provided files until those files are present
  if (runBtn) {
    runBtn.style.display = 'none';
  }
  if (importConfigBtn) {
    importConfigBtn.style.display = 'none';
  }
  if (importAllBtn) {
    importAllBtn.style.display = 'none';
  }
  if (validateAllBtn) {
    validateAllBtn.style.display = 'none';
  }

  function setStatus(status, text) {
    statusEl.textContent = text;
    statusEl.className = `status status-${status}`;
  }

  function setAuthMessage(message, type = '') {
    authMessageEl.textContent = message;
    authMessageEl.className = `message ${type}`;
  }

  function setConfigMessage(message, type = '') {
    configMessageEl.textContent = message;
    configMessageEl.className = `message ${type}`;
  }

  function setExportMessage(message, type = '') {
    if (!exportMessageEl) return;
    exportMessageEl.textContent = message;
    exportMessageEl.className = `message ${type}`;
  }

  function setBulkMessage(message, type = '') {
    if (!bulkMessageEl) return;
    bulkMessageEl.textContent = message;
    bulkMessageEl.className = `message ${type}`;
  }

  function setDuplicateMessage(message, type = '') {
    if (!duplicateMessageEl) return;
    duplicateMessageEl.textContent = message;
    duplicateMessageEl.className = `message ${type}`;
  }

  function setVersionLabel(message) {
    if (!versionLabel) return;
    versionLabel.textContent = message;
  }

  function formatAddress(url) {
    if (!url) return '';
    try {
      const parsed = new URL(url);
      return parsed.host + parsed.pathname.replace(/\/$/, '');
    } catch (err) {
      return url.replace(/^[a-zA-Z]+:\/\//, '');
    }
  }

  function setConnectionInfo(corehubUrl, pipelineCount) {
    const connectionInfoEl = document.getElementById('connection-info');
    const corehubInstanceEl = document.getElementById('corehub-instance');
    const pipelineCountEl = document.getElementById('pipeline-count');
    
    if (connectionInfoEl && corehubInstanceEl && pipelineCountEl) {
      if (corehubUrl && pipelineCount !== undefined) {
        const displayAddress = formatAddress(corehubUrl);
        corehubInstanceEl.textContent = `Connected to: ${displayAddress}`;
        pipelineCountEl.textContent = `${pipelineCount} pipelines`;
        connectionInfoEl.style.display = '';
      } else {
        connectionInfoEl.style.display = 'none';
      }
    }
  }

  function setAuthFieldsLocked(locked) {
    if (corehubUrlInput) corehubUrlInput.disabled = locked;
    if (usernameInput) usernameInput.disabled = locked;
    if (passwordInput) passwordInput.disabled = locked;
  }

  function setAuthEnabled(enabled) {
    logoutBtn.disabled = !enabled;
    document.querySelector('#login-form button[type="submit"]').disabled = enabled;
    runBtn.disabled = !enabled;
    if (exportBtn) {
      exportBtn.disabled = !enabled;
    }
    if (exportPipelineFullBtn) {
      exportPipelineFullBtn.disabled = !enabled;
    }
    if (exportAllBtn) {
      exportAllBtn.disabled = !enabled;
    }
    if (validateAllBtn) {
      validateAllBtn.disabled = !enabled;
    }
    if (importConfigBtn) {
      importConfigBtn.disabled = !enabled;
    }
    if (importAllBtn) {
      importAllBtn.disabled = !enabled;
    }
    const duplicateBtn = document.getElementById('duplicate-btn');
    if (duplicateBtn) {
      duplicateBtn.disabled = !enabled;
    }
    const duplicateSelect = document.getElementById('duplicate-pipeline-id');
    if (duplicateSelect) {
      duplicateSelect.disabled = !enabled;
    }
  }

  function makeLogNode(text) {
    if (logTemplate && logTemplate.content && logTemplate.content.firstElementChild) {
      const node = logTemplate.content.firstElementChild.cloneNode(true);
      node.textContent = text;
      return node;
    }
    const fallback = document.createElement('div');
    fallback.className = 'log-line';
    fallback.textContent = text;
    return fallback;
  }

  function appendLogLine(text) {
    if (!logOutput || typeof text !== 'string' || !text.trim()) return;
    logOutput.appendChild(makeLogNode(text));
    logOutput.scrollTop = logOutput.scrollHeight;
  }

  function renderLogs(logs = []) {
    if (!logOutput) return;
    logOutput.innerHTML = '';
    logs.forEach((line) => appendLogLine(line));
  }

  return {
    setStatus,
    setAuthMessage,
    setConfigMessage,
    setExportMessage,
    setBulkMessage,
    setDuplicateMessage,
    setAuthEnabled,
    renderLogs,
    appendLogLine,
    lockAuthFields: setAuthFieldsLocked,
    setVersion: setVersionLabel,
    setConnectionInfo,
    runBtn,
    logoutBtn,
    exportAllBtn,
    exportPipelineFullBtn,
  };
})();

const corehubUI = (() => {
  const summaryEl = document.getElementById('corehub-summary-message');
  const baseUrlEl = document.getElementById('corehub-base-url');
  const tlsEl = document.getElementById('corehub-ssl');
  const verifyEl = document.getElementById('corehub-verify');
  const chronosStatusEl = document.getElementById('corehub-chronos-status');
  const conductorStatusEl = document.getElementById('corehub-conductor-status');
  const pipelineTotalEl = document.getElementById('corehub-pipeline-count');
  const agentTotalEl = document.getElementById('corehub-agent-count');
  const entityTotalEl = document.getElementById('corehub-entity-count');
  const scheduleTotalEl = document.getElementById('corehub-schedule-count');
  const runningContainerTotalEl = document.getElementById('corehub-running-container-count');
  const pipelinesListEl = document.getElementById('corehub-pipelines-list');
  const envMetricsEl = document.getElementById('corehub-environment-metrics');
  const totalsMetricsEl = document.getElementById('corehub-total-metrics');

  if (!summaryEl || !pipelinesListEl) {
    return {
      updateEnvironment: () => {},
      showLoading: () => {},
      showSignedOut: () => {},
      showError: () => {},
      renderOverview: () => {},
    };
  }

  const setSummary = (message, type = 'info') => {
    summaryEl.textContent = message;
    summaryEl.className = message ? `message ${type}` : 'message';
  };

  const setText = (el, value) => {
    if (el) {
      el.textContent = value;
    }
  };

  const formatToggle = (value, labels = ['Disabled', 'Enabled']) => {
    if (value === null || value === undefined) return '—';
    return value ? labels[1] : labels[0];
  };

  const getChronosJobStats = (service = {}) => {
    const enabled = Number.isFinite(service.enabledJobs) ? service.enabledJobs : 0;
    let total = Number.isFinite(service.totalJobs) ? service.totalJobs : enabled;
    if (total < enabled) {
      total = enabled;
    }
    return { enabled, total };
  };

  const formatChronosStatus = (service = {}) => {
    if (!service.available) return 'Unavailable';
    return 'Available';
  };

  const formatChronosScheduleMetric = (service = {}) => {
    if (!service.available) return 'Unavailable';
    const { enabled, total } = getChronosJobStats(service);
    if (total === 0) return '0 jobs';
    return `${enabled}/${total} jobs enabled`;
  };

  const formatConductorStatus = (service = {}) => {
    if (!service.available) return 'Unavailable';
    const running = service.runningContainers ?? 0;
    if (running === 0) return 'Available (no containers running)';
    return 'Available';
  };

  const setMetricsLoading = (isLoading) => {
    [envMetricsEl, totalsMetricsEl].forEach((el) => {
      if (!el) return;
      el.classList.toggle('is-loading', isLoading);
    });
  };

  const renderPipelines = (pipelines = []) => {
    pipelinesListEl.innerHTML = '';
    if (!pipelines.length) {
      const empty = document.createElement('li');
      empty.className = 'corehub-empty';
      empty.textContent = 'No pipelines detected.';
      pipelinesListEl.appendChild(empty);
      return;
    }

    pipelines.forEach((pipeline) => {
      const li = document.createElement('li');
      li.className = 'corehub-pipeline';

      const header = document.createElement('div');
      header.className = 'corehub-pipeline-header';

      const title = document.createElement('h4');
      const pipelineId = pipeline.pipelineId || pipeline.id || pipeline.pipeline_id || '';
      const pipelineName = pipeline.name || pipelineId || 'Unnamed pipeline';
      title.textContent = pipelineId ? `${pipelineName} (${pipelineId})` : pipelineName;
      header.appendChild(title);

      const counts = document.createElement('span');
      counts.className = 'corehub-pipeline-meta';
      counts.textContent = `Agents: ${pipeline.agentCount || 0} · Entities: ${pipeline.entityCount || 0}`;
      header.appendChild(counts);

      li.appendChild(header);

      if (pipeline.description) {
        const desc = document.createElement('p');
        desc.className = 'corehub-pipeline-meta';
        desc.textContent = pipeline.description;
        li.appendChild(desc);
      }

      const agents = Array.isArray(pipeline.agents) ? pipeline.agents : [];
      if (agents.length) {
        const agentRow = document.createElement('div');
        agentRow.className = 'corehub-pipeline-meta';
        agents.forEach((agent) => {
          const pill = document.createElement('span');
          pill.className = 'corehub-agent-pill';
          const tag = agent.tag || 'unknown';
          const type = agent.type || '?';
          pill.textContent = `${type}: ${tag}`;
          agentRow.appendChild(pill);
        });
        li.appendChild(agentRow);
      }

      pipelinesListEl.appendChild(li);
    });
  };

  return {
    updateEnvironment(state) {
      if (!state) {
        return;
      }
      setText(baseUrlEl, state.baseUrl || '—');
      setText(tlsEl, formatToggle(state.useSsl));
      setText(verifyEl, formatToggle(state.skipVerify, ['Enforced', 'Skipped']));
      const services = state.services || {};
      setText(chronosStatusEl, formatChronosStatus(services.chronos));
      setText(conductorStatusEl, formatConductorStatus(services.conductor));
      const urlText = state.baseUrl || 'unknown Core Hub';
      const tlsText = formatToggle(state.useSsl);
      setSummary(`Connected to ${urlText} · TLS ${tlsText}`, 'success');
    },
    showLoading(message = 'Loading Core Hub data…') {
      if (!document.body.classList.contains('is-authenticated')) return;
      setSummary(message, 'info');
      pipelinesListEl.innerHTML = '';
      setMetricsLoading(true);
    },
    showSignedOut() {
      setSummary('Sign in to load Core Hub environment details.', 'info');
      setText(baseUrlEl, '—');
      setText(tlsEl, '—');
      setText(verifyEl, '—');
      setText(chronosStatusEl, '—');
      setText(conductorStatusEl, '—');
      setText(pipelineTotalEl, '0');
      setText(agentTotalEl, '0');
      setText(entityTotalEl, '0');
      setText(scheduleTotalEl, '0 jobs');
      setText(runningContainerTotalEl, '0');
      pipelinesListEl.innerHTML = '';
      const empty = document.createElement('li');
      empty.className = 'corehub-empty';
      empty.textContent = 'No data available.';
      pipelinesListEl.appendChild(empty);
      setMetricsLoading(false);
    },
    showError(message) {
      setSummary(message, 'error');
    },
    renderOverview(data) {
      if (!data) {
        this.showError('No overview data returned.');
        return;
      }
      const env = data.environment || {};
      this.updateEnvironment({
        baseUrl: env.baseUrl,
        useSsl: env.useSsl,
        skipVerify: env.skipVerify,
        enableScheduling: env.enableScheduling,
        createTables: env.createTables,
        services: data.services,
      });
      const totals = data.totals || {};
      const services = data.services || {};
      setText(pipelineTotalEl, String(totals.pipelines ?? 0));
      setText(agentTotalEl, String(totals.agents ?? 0));
      setText(entityTotalEl, String(totals.entities ?? 0));
      if (services.chronos) {
        setText(scheduleTotalEl, formatChronosScheduleMetric(services.chronos));
      } else {
        setText(scheduleTotalEl, String(totals.schedules ?? 0));
      }
      setText(runningContainerTotalEl, String(totals.runningContainers ?? 0));
      setSummary('Core Hub statistics updated.', 'success');
      renderPipelines(data.pipelines || []);
      setMetricsLoading(false);
    },
  };
})();

function initTabs() {
  const tabsRoot = document.getElementById('action-tabs');
  if (!tabsRoot) return;

  const tabButtons = Array.from(tabsRoot.querySelectorAll('.tab-button'));
  const tabPanels = Array.from(tabsRoot.querySelectorAll('.tab-panel'));
  const logCard = document.querySelector('.log-card');
  const layout = document.querySelector('.layout');

  const activateTab = (tabName) => {
    if (!tabName) return;
    tabButtons.forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.tab === tabName);
    });
    tabPanels.forEach((panel) => {
      panel.classList.toggle('active', panel.dataset.tabPanel === tabName);
    });
    if (logCard) {
      const shouldHide = tabName === 'corehub' || tabName === 'settings';
      logCard.classList.toggle('is-hidden', shouldHide);
    }
    if (layout) {
      layout.dataset.activeTab = tabName;
    }
  };

  const hideTab = (tabName, hidden) => {
    const button = tabButtons.find((btn) => btn.dataset.tab === tabName);
    const panel = tabPanels.find((p) => p.dataset.tabPanel === tabName);
    if (!button || !panel) return;
    button.classList.toggle('is-hidden', hidden);
    panel.classList.toggle('is-hidden', hidden);
    if (hidden && button.classList.contains('active')) {
      const fallback = tabButtons.find((btn) => !btn.classList.contains('is-hidden'));
      if (fallback) {
        activateTab(fallback.dataset.tab);
      }
    }
  };

  tabButtons.forEach((button) => {
    button.addEventListener('click', () => {
      activateTab(button.dataset.tab);
    });
  });

  const initialTab = tabButtons.find((btn) => btn.classList.contains('active'))?.dataset.tab
    || tabButtons[0]?.dataset.tab;
  activateTab(initialTab);

  window.automatorTabs = {
    activateTab,
    hideTab,
  };
}

function logActivity(scope, message) {
  if (!message) return;
  const timestamp = new Date().toLocaleTimeString();
  const prefix = scope ? `[${scope}]` : '';
  ui.appendLogLine(`${timestamp} ${prefix} ${message}`.trim());
}

let isRefreshingCorehub = false;

async function refreshCorehubOverview() {
  if (!document.body.classList.contains('is-authenticated')) {
    corehubUI.showSignedOut();
    return;
  }
  if (isRefreshingCorehub) return;
  isRefreshingCorehub = true;
  corehubUI.showLoading();
  try {
    const overview = await api.getCorehubOverview();
    if (!overview) {
      corehubUI.showError('Authentication required to view Core Hub overview.');
      return;
    }
    stateManager.corehubSnapshot = overview;
    corehubUI.renderOverview(overview);
  } catch (err) {
    console.error('Failed to refresh Core Hub overview', err);
    corehubUI.showError(err.message || 'Failed to load Core Hub overview.');
  } finally {
    isRefreshingCorehub = false;
  }
}

const stateManager = {
  yamlFileId: null,
  pollHandle: null,
  lastStatus: 'idle',
  corehubSnapshot: null,

  updateFromState(data) {
    applyUiFlags(data.ui || {});
    // Toggle global authenticated state for layout/visibility
    document.body.classList.toggle('is-authenticated', !!data.tokenPresent);
    const authCard = document.getElementById('auth-card');
    if (authCard) {
      authCard.classList.toggle('auth-card--collapsed', !!data.tokenPresent);
    }

    document.getElementById('corehub-url').value = data.baseUrl || '';
    document.getElementById('use-ssl').checked = !!data.useSsl;
    document.getElementById('skip-verify').checked = !!data.skipVerify;
    document.getElementById('enable-scheduling').checked = data.enableScheduling;
    document.getElementById('create-tables').checked = data.createTables;

    if (data.corehubOverview) {
      this.corehubSnapshot = data.corehubOverview;
      corehubUI.renderOverview(data.corehubOverview);
    } else if (this.corehubSnapshot) {
      corehubUI.renderOverview(this.corehubSnapshot);
    } else if (data.tokenPresent) {
      corehubUI.showLoading('Waiting for Core Hub overview…');
    }

    ui.setAuthEnabled(data.tokenPresent);
    if (!data.tokenPresent) {
      ui.setStatus('idle', 'Not authenticated');
      ui.renderLogs();
      this.setYamlFile(null);
      corehubUI.showSignedOut();
      return;
    }

    const run = data.run;
    if (run) {
      this.lastStatus = run.status;
      ui.setStatus(run.status, run.status.toUpperCase());
    } else {
      ui.setStatus('ready', 'Ready');
    }
  },

  setYamlFile(fileId) {
    this.yamlFileId = fileId;
     if (ui.runBtn) {
       ui.runBtn.style.display = fileId ? '' : 'none';
     }
  },

  startPolling() {
    this.stopPolling();
    this.pollHandle = setInterval(async () => {
      try {
        const response = await api.getCurrentRun(true);
        if (!response || !response.id) return;
        this.handleRunUpdate(response);
      } catch (err) {
        console.error(err);
      }
    }, 2000);
  },

  stopPolling() {
    if (this.pollHandle) {
      clearInterval(this.pollHandle);
      this.pollHandle = null;
    }
  },

  handleRunUpdate(run) {
    if (!run) return;
    ui.renderLogs(run.logs);
    ui.setStatus(run.status, run.status.toUpperCase());
    this.lastStatus = run.status;
    if (run.corehubOverview) {
      this.corehubSnapshot = run.corehubOverview;
      corehubUI.renderOverview(run.corehubOverview);
    } else if (this.corehubSnapshot) {
      corehubUI.renderOverview(this.corehubSnapshot);
    }
    if (run.status !== 'running') {
      this.stopPolling();
      ui.setAuthEnabled(true);
    }
  },
};

async function initialize() {
  try {
    const snapshot = await api.getState();
    stateManager.updateFromState(snapshot);
    if (snapshot.tokenPresent) {
      ui.lockAuthFields(true);
      await Promise.all([
        loadPipelines(),
        refreshCorehubOverview(),
      ]);
    } else {
      ui.lockAuthFields(false);
      corehubUI.showSignedOut();
    }
    await checkAutomatorVersion();
  } catch (err) {
    console.error(err);
    ui.setStatus('failed', 'API unavailable');
  }
}

function parseVersion(value) {
  if (!value) return [0, 0, 0];
  const match = String(value).match(/\d+(?:\.\d+)*/);
  const core = match ? match[0] : '0.0.0';
  return core.split('.').map((part) => {
    const n = parseInt(part, 10);
    return Number.isNaN(n) ? 0 : n;
  });
}

function isNewerVersion(remote, local) {
  const r = parseVersion(remote);
  const l = parseVersion(local);
  const length = Math.max(r.length, l.length);
  for (let i = 0; i < length; i += 1) {
    const rv = r[i] ?? 0;
    const lv = l[i] ?? 0;
    if (rv > lv) return true;
    if (rv < lv) return false;
  }
  return false;
}

async function checkAutomatorVersion() {
  let currentVersion = '0.0.0';

  try {
    const res = await fetch('/api/version');
    if (!res.ok) {
      throw new Error(`Failed to get local version: ${res.status}`);
    }
    const data = await res.json();
    currentVersion = data.version || currentVersion;
    ui.setVersion(`v${currentVersion}`);
  } catch (err) {
    console.error('Failed to determine local Automator version', err);
    ui.setVersion('Automator version: unknown');
    return;
  }

  try {
    const res = await fetch('https://api.backoffice.molo17.com/agent/automator/version');
    if (!res.ok) {
      throw new Error(`Version check failed: ${res.status}`);
    }
    const data = await res.json();
    const latestGa = data.latestVersionGA || data.latestVersionGa || data.latestversionGA;
    if (!latestGa) {
      return;
    }

    if (!isNewerVersion(latestGa, currentVersion)) {
      return;
    }

    // Highlight the version label and make it clickable when an update is available
    ui.setVersion(`v${latestGa} is available`);

    const versionLabelEl = document.getElementById('version-label');
    const changelogContainer = document.getElementById('version-changelog');

    if (versionLabelEl) {
      versionLabelEl.classList.add('version-label--update');
      versionLabelEl.setAttribute('role', 'button');
      versionLabelEl.setAttribute('tabindex', '0');
      versionLabelEl.title = `New version v${latestGa} available. Click to view changelog and download.`;
    }

    const downloadUrl = 'https://molo17.com/gluesync-automator/';
    const attachDownloadHandlers = () => {
      if (!versionLabelEl) return;
      const openDownload = () => {
        window.open(downloadUrl, '_blank', 'noopener');
      };
      versionLabelEl.addEventListener('click', openDownload);
      versionLabelEl.addEventListener('keypress', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          openDownload();
        }
      });
    };

    try {
      const clRes = await fetch(
        `/api/changelog/automator/${encodeURIComponent(latestGa)}`,
      );
      if (clRes.status === 404) {
        attachDownloadHandlers();
        return;
      }
      if (!clRes.ok) {
        attachDownloadHandlers();
        throw new Error(`Changelog fetch failed: ${clRes.status}`);
      }

      // Endpoint returns JSON like:
      // { id, versionNumber, releaseDate, changelog, changelogSummary, module }
      const clJson = await clRes.json().catch(() => null);
      const changelog = clJson && typeof clJson.changelog === 'string' ? clJson.changelog : '';

      if (changelog) {
        console.info(`Automator ${latestGa} changelog:\n${changelog}`);
        const container = changelogContainer || document.getElementById('version-changelog');
        if (container) {
          const titleEl = document.createElement('div');
          titleEl.className = 'version-changelog-title';
          titleEl.textContent = `What's new in v${latestGa}`;

          const bodyEl = document.createElement('div');
          bodyEl.className = 'version-changelog-body';
          bodyEl.textContent = changelog;

          const linkEl = document.createElement('a');
          linkEl.href = downloadUrl;
          linkEl.target = '_blank';
          linkEl.rel = 'noopener';
          linkEl.className = 'version-download-link';
          linkEl.textContent = 'Download latest Automator';

          container.innerHTML = '';
          container.appendChild(titleEl);
          container.appendChild(bodyEl);
          container.appendChild(linkEl);

          // Initially keep it hidden; clicking the version label will toggle visibility
          container.classList.remove('is-visible');

          if (versionLabelEl) {
            let outsideClickHandler = null;

            const positionChangelog = () => {
              const rect = versionLabelEl.getBoundingClientRect();
              // For a fixed-position popover, use viewport coordinates only
              const top = rect.bottom + 8;
              const left = rect.left;
              container.style.top = `${top}px`;
              container.style.left = `${left}px`;
              container.style.right = 'auto';
            };

            const toggleChangelog = () => {
              const willShow = !container.classList.contains('is-visible');
              container.classList.toggle('is-visible');

              if (willShow) {
                positionChangelog();

                outsideClickHandler = (event) => {
                  if (
                    !container.contains(event.target) &&
                    !versionLabelEl.contains(event.target)
                  ) {
                    container.classList.remove('is-visible');
                    document.removeEventListener('click', outsideClickHandler);
                    outsideClickHandler = null;
                  }
                };

                // Delay registering to avoid immediately catching the opening click
                setTimeout(() => {
                  if (outsideClickHandler) {
                    document.addEventListener('click', outsideClickHandler);
                  }
                }, 0);
              } else if (outsideClickHandler) {
                document.removeEventListener('click', outsideClickHandler);
                outsideClickHandler = null;
              }
            };

            versionLabelEl.addEventListener('click', toggleChangelog);
            versionLabelEl.addEventListener('keypress', (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                toggleChangelog();
              }
            });
          }
        }
      } else {
        // No changelog content returned, go straight to download URL on click
        attachDownloadHandlers();
      }
    } catch (err) {
      console.error('Failed to fetch Automator changelog', err);
      attachDownloadHandlers();
    }
  } catch (err) {
    console.error('Failed to check for latest Automator version', err);
  }
}

function updateImportButtonsVisibility() {
  const importConfigInput = document.getElementById('import-config-file');
  const importConfigBtnEl = document.getElementById('import-config-btn');
  const importAllInput = document.getElementById('import-all-file');
  const importAllBtnEl = document.getElementById('import-all-btn');
  const validateAllBtnEl = document.getElementById('validate-all-btn');

  console.log('updateImportButtonsVisibility called');
  console.log('importAllInput:', importAllInput);
  console.log('importAllInput?.files:', importAllInput?.files);
  console.log('validateAllBtnEl:', validateAllBtnEl);
  console.log('importAllBtnEl:', importAllBtnEl);

  // Import config button: visible only when a config file is selected
  if (importConfigBtnEl) {
    const hasConfigFile = !!(importConfigInput && importConfigInput.files && importConfigInput.files[0]);
    importConfigBtnEl.disabled = !hasConfigFile;
  }

  // Validate / Import All buttons: visible only when a ZIP file is selected
  const hasZipFile = !!(importAllInput && importAllInput.files && importAllInput.files[0]);
  console.log('hasZipFile:', hasZipFile);
  console.log('Setting validateAllBtnEl.disabled =', !hasZipFile);
  console.log('Setting importAllBtnEl.disabled =', !hasZipFile);
  
  if (validateAllBtnEl) {
    validateAllBtnEl.disabled = !hasZipFile;
    validateAllBtnEl.style.display = hasZipFile ? '' : 'none';
  }
  if (importAllBtnEl) {
    importAllBtnEl.disabled = !hasZipFile;
    importAllBtnEl.style.display = hasZipFile ? '' : 'none';
  }
}

function bindEvents() {
  const loginForm = document.getElementById('login-form');
  const configForm = document.getElementById('config-form');
  const yamlInput = document.getElementById('yaml-file');
  const exportForm = document.getElementById('export-form');
  const exportPipelineSelect = document.getElementById('export-pipeline-id');
  const bulkForm = document.getElementById('bulk-form');
  const bulkPipelineSelect = document.getElementById('bulk-pipeline-id');
  const bulkSchemaSelect = document.getElementById('bulk-source-schema');
  const bulkTargetSchemaInput = document.getElementById('bulk-target-schema');
  const bulkTablesList = document.getElementById('bulk-tables-list');
  const bulkTablesSummary = document.getElementById('bulk-tables-summary');
  const bulkToggleAllBtn = document.getElementById('bulk-toggle-all-btn');
  const bulkCreateBtn = document.getElementById('bulk-create-btn');
  const bulkLoadSchemasBtn = document.getElementById('bulk-load-schemas-btn');
  const bulkDownloadYamlBtn = document.getElementById('bulk-download-yaml-btn');
  const importConfigInput = document.getElementById('import-config-file');
  const importConfigBtnEl = document.getElementById('import-config-btn');
  const importAllInput = document.getElementById('import-all-file');
  const importAllBtnEl = document.getElementById('import-all-btn');
  const validateAllBtnEl = document.getElementById('validate-all-btn');
  const customSchemasCheckbox = document.getElementById('custom-schemas');
  const schemaFields = document.getElementById('schema-fields');
  const typeFields = document.getElementById('type-fields');
  const sourceTypeSelect = document.getElementById('source-type');
  const targetTypeSelect = document.getElementById('target-type');
  const corehubRefreshBtn = document.getElementById('corehub-refresh-btn');

  let bulkTablesState = [];
  let bulkSourceType = 'SQL';
  let bulkTargetType = 'SQL';

  // ...
  function updateSchemaVisibility() {
    if (!customSchemasCheckbox) return;
    const enabled = customSchemasCheckbox.checked;

    if (schemaFields) {
      schemaFields.style.display = enabled ? '' : 'none';
    }
    if (typeFields) {
      typeFields.style.display = enabled ? '' : 'none';
    }

    // When schemas are driven by YAML (default), keep type dropdowns read-only/hidden
    if (sourceTypeSelect) sourceTypeSelect.disabled = !enabled;
    if (targetTypeSelect) targetTypeSelect.disabled = !enabled;
  }

  function updateBulkSelectionSummary() {
    if (!bulkTablesSummary) return;
    const total = bulkTablesState.length;
    const selected = bulkTablesState.filter((t) => t.selected).length;
    if (!total) {
      bulkTablesSummary.textContent = bulkSchemaSelect && bulkSchemaSelect.value
        ? `No tables found for schema ${bulkSchemaSelect.value}.`
        : 'Select a schema to load tables.';
    } else {
      bulkTablesSummary.textContent = `${total} table(s) available, ${selected} selected.`;
    }
    if (bulkCreateBtn) {
      bulkCreateBtn.textContent = `Create all entities (${selected})`;
      bulkCreateBtn.disabled = !selected;
    }
    if (bulkToggleAllBtn) {
      bulkToggleAllBtn.disabled = !total;
    }
  }

  function resetBulkState() {
    bulkTablesState = [];
    bulkSourceType = 'SQL';
    bulkTargetType = 'SQL';
    if (bulkSchemaSelect) {
      bulkSchemaSelect.innerHTML = '<option value="">Select a schema…</option>';
      bulkSchemaSelect.disabled = true;
    }
    if (bulkTargetSchemaInput) {
      bulkTargetSchemaInput.value = '';
    }
    if (bulkTablesList) {
      bulkTablesList.innerHTML = '';
    }
    if (bulkCreateBtn) {
      bulkCreateBtn.textContent = 'Create all entities (0)';
      bulkCreateBtn.disabled = true;
    }
    if (bulkToggleAllBtn) {
      bulkToggleAllBtn.disabled = true;
    }
    updateBulkSelectionSummary();
  }

  if (customSchemasCheckbox && schemaFields) {
    updateSchemaVisibility();
    customSchemasCheckbox.addEventListener('change', updateSchemaVisibility);
  }

  if (corehubRefreshBtn) {
    corehubRefreshBtn.addEventListener('click', () => {
      refreshCorehubOverview();
    });
  }

  if (bulkPipelineSelect) {
    resetBulkState();
    bulkPipelineSelect.addEventListener('change', () => {
      resetBulkState();
      if (bulkLoadSchemasBtn) {
        bulkLoadSchemasBtn.disabled = !bulkPipelineSelect.value;
      }
      if (bulkDownloadYamlBtn) {
        bulkDownloadYamlBtn.disabled = !bulkPipelineSelect.value;
      }
      ui.setBulkMessage('', '');
    });
  }

  if (bulkLoadSchemasBtn && bulkSchemaSelect) {
    bulkLoadSchemasBtn.disabled = !(bulkPipelineSelect && bulkPipelineSelect.value);
    bulkLoadSchemasBtn.addEventListener('click', async () => {
      if (!bulkPipelineSelect || !bulkPipelineSelect.value) {
        ui.setBulkMessage('Please select a pipeline first', 'error');
        return;
      }

      bulkLoadSchemasBtn.disabled = true;
      resetBulkState();
      ui.setBulkMessage('Loading source schemas…');
      logActivity('Bulk', `Loading source schemas for pipeline ${bulkPipelineSelect.value}`);

      try {
        const res = await api.bulkListSchemas(bulkPipelineSelect.value);
        const schemas = (res && res.schemas) || [];

        bulkSourceType = (res && res.sourceType) || 'SQL';
        bulkTargetType = (res && res.targetType) || 'SQL';

        bulkSchemaSelect.innerHTML = '<option value="">Select a schema…</option>';
        schemas.forEach((schema) => {
          const opt = document.createElement('option');
          opt.value = schema;
          opt.textContent = schema;
          bulkSchemaSelect.appendChild(opt);
        });
        bulkSchemaSelect.disabled = !schemas.length;

        if (!schemas.length) {
          ui.setBulkMessage('No source schemas found for this pipeline', 'error');
          logActivity('Bulk', `No schemas returned for pipeline ${bulkPipelineSelect.value}`);
        } else {
          ui.setBulkMessage(`Loaded ${schemas.length} schema(s). Select a schema to load tables.`, 'success');
          logActivity('Bulk', `Loaded ${schemas.length} schema(s) for pipeline ${bulkPipelineSelect.value}`);
        }
      } catch (err) {
        ui.setBulkMessage(err.message, 'error');
        logActivity('Bulk', `Failed to load schemas: ${err.message}`);
      } finally {
        if (bulkLoadSchemasBtn) {
          bulkLoadSchemasBtn.disabled = !(bulkPipelineSelect && bulkPipelineSelect.value);
        }
      }
    });
  }

  if (bulkDownloadYamlBtn) {
    bulkDownloadYamlBtn.addEventListener('click', async () => {
      if (!bulkPipelineSelect || !bulkPipelineSelect.value) {
        ui.setBulkMessage('Please select a pipeline first', 'error');
        return;
      }
      if (!bulkSchemaSelect || !bulkSchemaSelect.value) {
        ui.setBulkMessage('Please select a source schema first', 'error');
        return;
      }

      const selectedTables = bulkTablesState.filter((t) => t.selected).map((t) => t.name);
      if (!selectedTables.length) {
        ui.setBulkMessage('Please select at least one table', 'error');
        return;
      }

      const payload = {
        pipelineId: bulkPipelineSelect.value,
        sourceSchema: bulkSchemaSelect.value,
        tableNames: selectedTables,
      };

      ui.setBulkMessage('Preparing YAML template…');
      logActivity('Bulk', `Generating YAML template for ${selectedTables.length} table(s) in schema ${bulkSchemaSelect.value}`);

      try {
        const { blob, filename } = await api.bulkTemplate(payload);
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
        ui.setBulkMessage(`Downloaded ${filename}`, 'success');
        logActivity('Bulk', `Downloaded YAML template ${filename}`);
      } catch (err) {
        ui.setBulkMessage(err.message, 'error');
        logActivity('Bulk', `Failed to generate YAML template: ${err.message}`);
      }
    });
  }

  if (bulkSchemaSelect) {
    bulkSchemaSelect.addEventListener('change', async () => {
      if (!bulkSchemaSelect.value) {
        resetBulkState();
        return;
      }

      if (!bulkPipelineSelect || !bulkPipelineSelect.value) {
        ui.setBulkMessage('Please select a pipeline first', 'error');
        resetBulkState();
        return;
      }

      if (bulkTablesSummary) {
        bulkTablesSummary.textContent = 'Loading tables…';
      }
      if (bulkTablesList) {
        bulkTablesList.innerHTML = '';
      }
      bulkTablesState = [];
      if (bulkCreateBtn) {
        bulkCreateBtn.textContent = 'Create all entities (0)';
        bulkCreateBtn.disabled = true;
      }
      if (bulkToggleAllBtn) {
        bulkToggleAllBtn.disabled = true;
      }

      try {
        const res = await api.bulkListTables(bulkPipelineSelect.value, bulkSchemaSelect.value);
        const tables = (res && res.tables) || [];

        bulkTablesState = tables.map((name) => ({ name, selected: true }));

        if (bulkTablesList) {
          bulkTablesList.innerHTML = '';
          bulkTablesState.forEach((table, index) => {
            const label = document.createElement('label');
            label.className = 'checkbox';
            const input = document.createElement('input');
            input.type = 'checkbox';
            input.checked = table.selected;
            input.dataset.index = String(index);
            input.addEventListener('change', () => {
              const i = Number(input.dataset.index);
              if (!Number.isNaN(i) && bulkTablesState[i]) {
                bulkTablesState[i].selected = input.checked;
                updateBulkSelectionSummary();
              }
            });
            label.appendChild(input);
            const textNode = document.createTextNode(table.name);
            label.appendChild(textNode);
            bulkTablesList.appendChild(label);
          });
        }

        updateBulkSelectionSummary();

        if (!tables.length) {
          ui.setBulkMessage(`No tables found for schema ${bulkSchemaSelect.value}`, 'error');
          logActivity('Bulk', `No tables found for schema ${bulkSchemaSelect.value}`);
        } else {
          ui.setBulkMessage('', '');
          logActivity('Bulk', `Loaded ${tables.length} table(s) for schema ${bulkSchemaSelect.value}`);
        }
      } catch (err) {
        ui.setBulkMessage(err.message, 'error');
        logActivity('Bulk', `Failed to load tables: ${err.message}`);
        bulkTablesState = [];
        if (bulkTablesList) {
          bulkTablesList.innerHTML = '';
        }
        updateBulkSelectionSummary();
      }
    });
  }

  if (bulkToggleAllBtn) {
    bulkToggleAllBtn.addEventListener('click', () => {
      if (!bulkTablesState.length) return;
      const allSelected = bulkTablesState.every((t) => t.selected);
      const next = !allSelected;
      bulkTablesState.forEach((t) => {
        t.selected = next;
      });
      if (bulkTablesList) {
        const inputs = bulkTablesList.querySelectorAll('input[type="checkbox"]');
        inputs.forEach((input) => {
          input.checked = next;
        });
      }
      updateBulkSelectionSummary();
    });
  }

  if (bulkCreateBtn) {
    bulkCreateBtn.addEventListener('click', async () => {
      if (!bulkPipelineSelect || !bulkPipelineSelect.value) {
        ui.setBulkMessage('Please select a pipeline first', 'error');
        return;
      }
      if (!bulkSchemaSelect || !bulkSchemaSelect.value) {
        ui.setBulkMessage('Please select a source schema first', 'error');
        return;
      }

      const selectedTables = bulkTablesState.filter((t) => t.selected).map((t) => t.name);
      if (!selectedTables.length) {
        ui.setBulkMessage('Please select at least one table', 'error');
        return;
      }

      const sourceSchema = bulkSchemaSelect.value;
      const targetSchema = (bulkTargetSchemaInput && bulkTargetSchemaInput.value)
        ? bulkTargetSchemaInput.value
        : sourceSchema;

      const chunkSizeInput = document.getElementById('chunk-size');
      const skipErrorsInput = document.getElementById('skip-errors');
      const enableSchedulingInput = document.getElementById('enable-scheduling');
      const createTablesInput = document.getElementById('create-tables');

      const payload = {
        pipelineId: bulkPipelineSelect.value,
        sourceSchema,
        targetSchema,
        sourceType: bulkSourceType || 'SQL',
        targetType: bulkTargetType || 'SQL',
        tableNames: selectedTables,
        chunkSize: Number((chunkSizeInput && chunkSizeInput.value) || 50),
        skipErrors: !!(skipErrorsInput && skipErrorsInput.checked),
        enableScheduling: !!(enableSchedulingInput && enableSchedulingInput.checked),
        createTables: !!(createTablesInput && createTablesInput.checked),
      };

      ui.setBulkMessage('Starting bulk entity creation…');
      logActivity('Bulk', `Starting bulk entity creation for ${selectedTables.length} table(s) from schema ${sourceSchema}`);

      try {
        const res = await api.bulkCreate(payload);
        if (res && typeof res.message === 'string') {
          ui.setBulkMessage(res.message, res.success === false ? 'error' : 'success');
          logActivity('Bulk', res.message);
        } else {
          ui.setBulkMessage('Bulk entity creation completed', 'success');
          logActivity('Bulk', 'Bulk entity creation completed');
        }
      } catch (err) {
        ui.setBulkMessage(err.message, 'error');
        logActivity('Bulk', `Bulk entity creation failed: ${err.message}`);
      }
    });
  }

  loginForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    ui.setAuthMessage('Signing in…');
    logActivity('Auth', 'Authenticating with CoreHub…');

    const payload = {
      baseUrl: document.getElementById('corehub-url').value,
      username: document.getElementById('username').value,
      password: document.getElementById('password').value,
      useSsl: document.getElementById('use-ssl').checked,
      skipVerify: document.getElementById('skip-verify').checked,
      enableScheduling: document.getElementById('enable-scheduling').checked,
      createTables: document.getElementById('create-tables').checked,
    };

    // Retry logic for transient network errors
    const maxRetries = 2;
    let lastError = null;
    
    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        if (attempt > 1) {
          // Add a small delay before retry
          await new Promise(resolve => setTimeout(resolve, 1000));
          ui.setAuthMessage(`Retrying authentication (attempt ${attempt}/${maxRetries})…`);
          logActivity('Auth', `Retrying authentication (attempt ${attempt}/${maxRetries})…`);
        }
        
        await api.login(payload);
        ui.setAuthMessage('Authentication successful', 'success');
        logActivity('Auth', 'Authentication successful');
        const snapshot = await api.getState();
        stateManager.updateFromState(snapshot);
        ui.lockAuthFields(true);
        await Promise.all([
          loadPipelines(),
          refreshCorehubOverview(),
        ]);
        return; // Success, exit
      } catch (err) {
        lastError = err;
        // Only retry on network errors, not on authentication failures
        const isNetworkError = err.message.includes('Failed to fetch') || 
                               err.message.includes('No route to host') ||
                               err.message.includes('Connection') ||
                               err.message.includes('Network');
        
        if (!isNetworkError || attempt === maxRetries) {
          // Don't retry - either it's an auth error or we're out of retries
          break;
        }
        
        logActivity('Auth', `Network error on attempt ${attempt}, will retry...`);
      }
    }
    
    // All retries failed
    ui.setAuthMessage(lastError.message, 'error');
    ui.setAuthEnabled(false);
    logActivity('Auth', `Authentication failed: ${lastError.message}`);
  });

  ui.logoutBtn.addEventListener('click', async () => {
    logActivity('Auth', 'Logging out…');
    try {
      await api.logout();
      ui.setAuthMessage('Logged out', 'success');
      logActivity('Auth', 'Logged out successfully');
      ui.setAuthEnabled(false);
      ui.setStatus('idle', 'Not authenticated');
      ui.setConnectionInfo(null, null);
      ui.lockAuthFields(false);
      document.body.classList.remove('is-authenticated');
      const authCard = document.getElementById('auth-card');
      if (authCard) {
        authCard.classList.remove('auth-card--collapsed');
      }
      stateManager.stopPolling();
      ui.renderLogs();
      if (exportPipelineSelect) {
        exportPipelineSelect.innerHTML = '<option value="">Select a pipeline…</option>';
      }
      const configPipelineSelect = document.getElementById('pipeline-id');
      if (configPipelineSelect && configPipelineSelect.tagName === 'SELECT') {
        configPipelineSelect.innerHTML = '<option value="">Select a pipeline…</option>';
      }
      const duplicatePipelineSelect = document.getElementById('duplicate-pipeline-id');
      if (duplicatePipelineSelect && duplicatePipelineSelect.tagName === 'SELECT') {
        duplicatePipelineSelect.innerHTML = '<option value="">Select a pipeline to duplicate…</option>';
      }
      // Clear any selected import files and hide related buttons again
      if (importConfigInput) {
        importConfigInput.value = '';
      }
      if (importAllInput) {
        importAllInput.value = '';
      }
      if (typeof updateImportButtonsVisibility === 'function') {
        updateImportButtonsVisibility();
      }
      stateManager.setYamlFile(null);
    } catch (err) {
      ui.setAuthMessage(err.message, 'error');
      logActivity('Auth', `Logout failed: ${err.message}`);
    }
  });

  yamlInput.addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    ui.setConfigMessage('Uploading…');
    logActivity('Config', `Uploading ${file.name}…`);
    try {
      const response = await api.uploadYaml(file);
      stateManager.setYamlFile(response.fileId);
      ui.setConfigMessage(`Uploaded ${response.filename}`, 'success');
      logActivity('Config', `Uploaded ${response.filename}`);
    } catch (err) {
      ui.setConfigMessage(err.message, 'error');
      stateManager.setYamlFile(null);
      logActivity('Config', `Upload failed: ${err.message}`);
    }
  });

  configForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!stateManager.yamlFileId) {
      ui.setConfigMessage('Please upload a YAML configuration file first', 'error');
      return;
    }

    const customSchemasEnabled = customSchemasCheckbox ? customSchemasCheckbox.checked : false;
    const sourceSchemaValue = document.getElementById('source-schema').value;
    const targetSchemaValue = document.getElementById('target-schema').value;

    const payload = {
      pipelineId: document.getElementById('pipeline-id').value,
      sourceSchema: customSchemasEnabled ? sourceSchemaValue : '',
      targetSchema: customSchemasEnabled ? targetSchemaValue : '',
      autoSchemas: !customSchemasEnabled,
      sourceType: document.getElementById('source-type').value,
      targetType: document.getElementById('target-type').value,
      chunkSize: Number(document.getElementById('chunk-size').value || 50),
      yamlFileId: stateManager.yamlFileId,
      skipErrors: document.getElementById('skip-errors').checked,
      enableScheduling: document.getElementById('enable-scheduling').checked,
      createTables: document.getElementById('create-tables').checked,
      useSsl: document.getElementById('use-ssl').checked,
      skipVerify: document.getElementById('skip-verify').checked,
    };

    ui.setConfigMessage('Starting…');
    ui.setAuthEnabled(false);
    ui.renderLogs();
    ui.setStatus('running', 'Running');
    logActivity('Config', `Starting entity creation for pipeline ${payload.pipelineId || '(none)'}…`);

    try {
      await api.startRun(payload);
      stateManager.startPolling();
      ui.setConfigMessage('Entity creation started', 'success');
      logActivity('Config', 'Entity creation started');
    } catch (err) {
      ui.setConfigMessage(err.message, 'error');
      ui.setAuthEnabled(true);
      ui.setStatus('failed', 'Failed to start');
      logActivity('Config', `Failed to start entity creation: ${err.message}`);
    }
  });

  if (exportForm && exportPipelineSelect) {
    exportForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const pipelineId = exportPipelineSelect.value;
      if (!pipelineId) {
        ui.setExportMessage('Please select a pipeline to export metadata for', 'error');
        return;
      }

      ui.setExportMessage('Exporting metadata…');
       logActivity('Export', `Exporting metadata for pipeline ${pipelineId}…`);
      try {
        const { blob, filename } = await api.exportPipeline(pipelineId);
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
        ui.setExportMessage(`Exported ${filename}`, 'success');
        logActivity('Export', `Metadata export completed: ${filename}`);
      } catch (err) {
        ui.setExportMessage(err.message, 'error');
        logActivity('Export', `Metadata export failed: ${err.message}`);
      }
    });
  }

  if (ui.exportPipelineFullBtn && exportPipelineSelect) {
    ui.exportPipelineFullBtn.addEventListener('click', async () => {
      const pipelineId = exportPipelineSelect.value;
      if (!pipelineId) {
        ui.setExportMessage('Please select a pipeline to export a full backup for', 'error');
        return;
      }

      ui.setExportMessage('Exporting full backup…');
      logActivity('Export', `Exporting full backup for pipeline ${pipelineId}…`);
      try {
        const { blob, filename } = await api.exportPipelineFull(pipelineId);
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
        ui.setExportMessage(`Exported ${filename}`, 'success');
        logActivity('Export', `Full backup completed: ${filename}`);
      } catch (err) {
        ui.setExportMessage(err.message, 'error');
        logActivity('Export', `Full backup failed: ${err.message}`);
      }
    });
  }

  if (ui.exportAllBtn) {
    console.log('Export All button found, adding event listener');
    ui.exportAllBtn.addEventListener('click', async () => {
      console.log('Export All button clicked');
      ui.setExportMessage('Exporting all pipelines…');
      logActivity('Export', 'Exporting all pipelines…');
      try {
        const { blob, filename } = await api.exportAllPipelines();
        console.log('Export All API call successful, filename:', filename);
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
        ui.setExportMessage(`Exported ${filename}`, 'success');
        logActivity('Export', `All pipelines export completed: ${filename}`);
      } catch (err) {
        console.error('Export All failed:', err);
        ui.setExportMessage(err.message, 'error');
        logActivity('Export', `Export all failed: ${err.message}`);
      }
    });
  } else {
    console.error('Export All button not found!');
  }

  if (importConfigInput) {
    importConfigInput.addEventListener('change', updateImportButtonsVisibility);
  }

  if (importAllInput) {
    importAllInput.addEventListener('change', updateImportButtonsVisibility);
  }

  if (importConfigBtnEl && importConfigInput) {
    importConfigBtnEl.addEventListener('click', async () => {
      const file = importConfigInput.files?.[0];
      if (!file) {
        ui.setConfigMessage('Please choose a config file to import', 'error');
        return;
      }

      ui.setConfigMessage('Importing config…');
      logActivity('Config', `Importing config file ${file.name}…`);
      try {
        const result = await api.importConfig(file);
        ui.setConfigMessage(result.message || 'Config imported successfully', 'success');
        logActivity('Config', result.message || 'Config import completed');
      } catch (err) {
        ui.setConfigMessage(err.message, 'error');
        logActivity('Config', `Config import failed: ${err.message}`);
      }
    });
  }

  if (importAllBtnEl && importAllInput) {
    importAllBtnEl.addEventListener('click', async () => {
      const file = importAllInput.files?.[0];
      if (!file) {
        ui.setConfigMessage('Please choose a backup ZIP to import', 'error');
        return;
      }

      // Get auto-deploy checkbox value
      const autoDeployCheckbox = document.getElementById('auto-deploy-agents');
      const autoDeployAgents = autoDeployCheckbox ? autoDeployCheckbox.checked : true;

      ui.setConfigMessage('Importing full backup…');
      logActivity('Config', `Importing full backup ${file.name}…`);
      try {
        const result = await api.importAll(file, autoDeployAgents);
        
        // Display logs if present
        if (result.logs && Array.isArray(result.logs)) {
          result.logs.forEach(log => logActivity('Config', log));
        }
        
        if (result.success === false) {
          ui.setConfigMessage(result.message || 'Import failed', 'error');
          logActivity('Config', `Full backup import failed: ${result.message}`);
        } else {
          ui.setConfigMessage(result.message || 'Backup imported successfully', 'success');
          logActivity('Config', result.message || 'Full backup import completed');
        }
      } catch (err) {
        ui.setConfigMessage(err.message, 'error');
        logActivity('Config', `Full backup import failed: ${err.message}`);
      }
    });
  }

  if (validateAllBtnEl && importAllInput) {
    validateAllBtnEl.addEventListener('click', async () => {
      const file = importAllInput.files?.[0];
      if (!file) {
        ui.setConfigMessage('Please choose a backup ZIP to validate', 'error');
        return;
      }

      ui.setConfigMessage('Validating backup…');
      logActivity('Config', `Validating backup ${file.name}…`);
      try {
        const result = await api.validateAll(file);
        const ok = result.success !== false;
        const text = result.message || '';
        if (text) {
          const lines = text.split('\n');
          ui.renderLogs(lines);
          logActivity('Config', 'Validation output displayed in log');
        }
        ui.setConfigMessage(ok ? 'Validation OK' : 'Validation failed', ok ? 'success' : 'error');
        logActivity('Config', ok ? 'Backup validation OK' : 'Backup validation failed');
      } catch (err) {
        ui.setConfigMessage(err.message, 'error');
        logActivity('Config', `Backup validation failed: ${err.message}`);
      }
    });
  }

  const duplicateForm = document.getElementById('duplicate-form');
  const duplicatePipelineSelect = document.getElementById('duplicate-pipeline-id');
  const duplicateBtn = document.getElementById('duplicate-btn');
  if (duplicateBtn && !duplicateBtn.dataset.defaultLabel) {
    duplicateBtn.dataset.defaultLabel = duplicateBtn.textContent;
  }

  let duplicateRequestController = null;

  function setDuplicateControlsDisabled(disabled) {
    const controlIds = [
      'duplicate-pipeline-id',
      'duplicate-new-name',
      'customize-agents',
      'duplicate-source-tag',
      'duplicate-target-tag',
      'customize-conductor',
      'duplicate-conductor-url',
      'duplicate-source-host',
      'duplicate-target-host',
      'customize-schemas',
      'duplicate-source-schema',
      'duplicate-target-schema',
    ];
    controlIds.forEach((id) => {
      const el = document.getElementById(id);
      if (!el || el === duplicateBtn) return;
      if (disabled) {
        el.setAttribute('data-prev-disabled', el.disabled ? 'true' : 'false');
        el.disabled = true;
      } else if (el.hasAttribute('data-prev-disabled')) {
        const wasDisabled = el.getAttribute('data-prev-disabled') === 'true';
        el.disabled = wasDisabled;
        el.removeAttribute('data-prev-disabled');
      } else {
        el.disabled = false;
      }
    });
  }

  function setDuplicateBusy(isBusy, label) {
    if (!duplicateBtn) return;
    const defaultLabel = duplicateBtn.dataset.defaultLabel || 'Duplicate Pipeline';
    duplicateBtn.textContent = isBusy ? (label || 'Stop duplication') : defaultLabel;
    if (isBusy) {
      duplicateBtn.classList.add('danger');
    } else {
      duplicateBtn.classList.remove('danger');
    }
    if (!duplicatePipelineSelect) return;
    if (!isBusy) {
      duplicateBtn.disabled = !duplicatePipelineSelect.value;
    }
  }

  function resetDuplicateState() {
    if (duplicateRequestController) {
      duplicateRequestController = null;
    }
    setDuplicateBusy(false);
    setDuplicateControlsDisabled(false);
  }

  if (duplicateForm) {
    duplicateForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (duplicateRequestController) {
        try {
          const response = await api.cancelDuplicate();
          ui.setDuplicateMessage(response.message || 'Cancelling duplicate request…', 'warning');
          logActivity('duplicate', response.message || 'Cancellation requested by user');
        } catch (err) {
          ui.setDuplicateMessage(err.message || 'Failed to request cancellation', 'error');
          logActivity('duplicate', `Failed to request cancellation: ${err.message}`);
        } finally {
          duplicateRequestController.abort();
        }
        return;
      }
      const pipelineId = document.getElementById('duplicate-pipeline-id').value;
      if (!pipelineId) {
        ui.setDuplicateMessage('Please select a pipeline to duplicate', 'error');
        return;
      }
      logActivity('duplicate', `Starting duplication for pipeline ${pipelineId}`);

      const sourcePasswordInput = document.getElementById('duplicate-source-password');
      const targetPasswordInput = document.getElementById('duplicate-target-password');
      const sourcePassword = sourcePasswordInput ? sourcePasswordInput.value.trim() : '';
      const targetPassword = targetPasswordInput ? targetPasswordInput.value.trim() : '';

      if (!sourcePassword || !targetPassword) {
        ui.setDuplicateMessage('Both source and target agent passwords are required.', 'error');
        return;
      }

      const customizeAgentsEnabled = !!(document.getElementById('customize-agents') && document.getElementById('customize-agents').checked);
      const fallbackSourceTag = duplicateForm.dataset.currentSourceTag || '';
      const fallbackTargetTag = duplicateForm.dataset.currentTargetTag || '';

      const sourceAgentTagValue = customizeAgentsEnabled
        ? document.getElementById('duplicate-source-tag').value
        : fallbackSourceTag;
      const targetAgentTagValue = customizeAgentsEnabled
        ? document.getElementById('duplicate-target-tag').value
        : fallbackTargetTag;

      if (!sourceAgentTagValue || !targetAgentTagValue) {
        ui.setDuplicateMessage('Current agent tags are unavailable. Please fetch pipeline details or enable customization.', 'error');
        return;
      }
      logActivity(
        'duplicate',
        `Cloning with SOURCE tag "${sourceAgentTagValue}" and TARGET tag "${targetAgentTagValue}"`
      );

      const newPipelineNameInput = document.getElementById('duplicate-new-name');
      const newPipelineNameValue = newPipelineNameInput ? newPipelineNameInput.value.trim() : '';
      const cloneEntitiesToggle = document.getElementById('duplicate-clone-entities');
      const cloneEntitiesValue = cloneEntitiesToggle ? cloneEntitiesToggle.checked : true;

      const payload = {
        pipelineId: pipelineId,
        newPipelineName: newPipelineNameValue || undefined,
        sourceAgentTag: sourceAgentTagValue,
        targetAgentTag: targetAgentTagValue,
        sourceAgentPassword: sourcePassword,
        targetAgentPassword: targetPassword,
        cloneEntities: cloneEntitiesValue,
        conductorUrl: document.getElementById('duplicate-conductor-url').value || undefined,
      };
      if (payload.conductorUrl) {
        logActivity('duplicate', `Using custom Conductor URL: ${payload.conductorUrl}`);
      } else {
        logActivity('duplicate', 'Using CoreHub-derived Conductor URL');
      }

      const sourceHostInput = document.getElementById('duplicate-source-host');
      const targetHostInput = document.getElementById('duplicate-target-host');
      const sourceHostValue = sourceHostInput ? sourceHostInput.value.trim() : '';
      const targetHostValue = targetHostInput ? targetHostInput.value.trim() : '';
      if (sourceHostValue) {
        payload.sourceHost = sourceHostValue;
        logActivity('duplicate', `Overriding source host to ${sourceHostValue}`);
      }
      if (targetHostValue) {
        payload.targetHost = targetHostValue;
        logActivity('duplicate', `Overriding target host to ${targetHostValue}`);
      }

      const customizeSchemasToggle = document.getElementById('customize-schemas');
      if (customizeSchemasToggle && customizeSchemasToggle.checked) {
        const sourceSchemaInput = document.getElementById('duplicate-source-schema');
        const targetSchemaInput = document.getElementById('duplicate-target-schema');
        const sourceSchemaValue = sourceSchemaInput ? sourceSchemaInput.value.trim() : '';
        const targetSchemaValue = targetSchemaInput ? targetSchemaInput.value.trim() : '';
        
        // Allow overriding either or both schemas independently
        if (sourceSchemaValue || targetSchemaValue) {
          payload.overrideSchemas = true;
          if (sourceSchemaValue) {
            payload.overrideSourceSchema = sourceSchemaValue;
          }
          if (targetSchemaValue) {
            payload.overrideTargetSchema = targetSchemaValue;
          }
          const overrideMsg = [
            sourceSchemaValue ? `source: ${sourceSchemaValue}` : null,
            targetSchemaValue ? `target: ${targetSchemaValue}` : null
          ].filter(Boolean).join(', ');
          logActivity('duplicate', `Overriding schemas (${overrideMsg})`);
        }
      }

      duplicateRequestController = new AbortController();
      setDuplicateControlsDisabled(true);
      setDuplicateBusy(true);
      ui.setDuplicateMessage('Duplicating pipeline… exporting configuration and checking agent availability. Click "Stop" to cancel.', 'info');
      logActivity('duplicate', 'Payload sent to backend. Awaiting response…');
      try {
        const result = await api.duplicatePipeline(payload, { signal: duplicateRequestController.signal });
        const duplicatedName = result.newPipelineName || result.pipelineName || '(unnamed pipeline)';
        const duplicatedId = result.newPipelineId || result.pipelineId || '(unknown id)';
        ui.setDuplicateMessage(`Pipeline duplicated successfully: ${duplicatedName} (${duplicatedId})`, 'success');
        logActivity('duplicate', `Duplicate succeeded: ${duplicatedName} (${duplicatedId})`);
        // Refresh pipeline list to include the new pipeline
        await loadPipelines();
      } catch (err) {
        if (err.name === 'AbortError') {
          ui.setDuplicateMessage('Duplicate request cancelled by user', 'warning');
          logActivity('duplicate', 'Duplicate request cancelled');
        } else {
          ui.setDuplicateMessage(err.message, 'error');
          logActivity('duplicate', `Duplicate failed: ${err.message}`);
        }
      }
      logActivity('duplicate', 'Duplicate flow finished. Resetting UI state.');
      resetDuplicateState();
    });
  }

  // Update duplicate button state when pipeline selection changes
  if (duplicatePipelineSelect) {
    const setCurrentTags = (sourceTag = '', targetTag = '') => {
      const currentSourceTagInput = document.getElementById('duplicate-current-source-tag');
      const currentTargetTagInput = document.getElementById('duplicate-current-target-tag');
      if (currentSourceTagInput) currentSourceTagInput.value = sourceTag || '';
      if (currentTargetTagInput) currentTargetTagInput.value = targetTag || '';
    };

    const clearNewTags = () => {
      const sourceTagInput = document.getElementById('duplicate-source-tag');
      const targetTagInput = document.getElementById('duplicate-target-tag');
      if (sourceTagInput) sourceTagInput.value = '';
      if (targetTagInput) targetTagInput.value = '';
    };

    duplicatePipelineSelect.addEventListener('change', async () => {
      const pipelineId = duplicatePipelineSelect.value;
      const duplicateBtn = document.getElementById('duplicate-btn');
      if (duplicateBtn && !duplicateRequestController) {
        duplicateBtn.disabled = !pipelineId;
      }

      setCurrentTags('', '');
      clearNewTags();

      if (!pipelineId) {
        ui.setDuplicateMessage('', '');
        return;
      }

      ui.setDuplicateMessage('Loading current agent tags…');
      try {
        const response = await fetch(`/api/pipeline/${encodeURIComponent(pipelineId)}/agents`);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.detail || 'Failed to load pipeline agents');
        }

        if (duplicateForm) {
          duplicateForm.dataset.currentSourceTag = data.sourceAgentTag || '';
          duplicateForm.dataset.currentTargetTag = data.targetAgentTag || '';
        }

        setCurrentTags(data.sourceAgentTag, data.targetAgentTag);
        if (customizeAgentsCheckbox && !customizeAgentsCheckbox.checked) {
          clearNewTags();
        }
        ui.setDuplicateMessage('', '');
      } catch (err) {
        console.error('Failed to fetch pipeline agents for auto-population:', err);
        ui.setDuplicateMessage(err.message || 'Failed to fetch pipeline agents', 'error');
      }
    });
  }

  const customizeAgentsCheckbox = document.getElementById('customize-agents');
  const agentCustomizationFields = document.getElementById('agent-customization-fields');
  const customizeConductorCheckbox = document.getElementById('customize-conductor');
  const conductorFields = document.getElementById('conductor-fields');
  const customizeSchemasCheckbox = document.getElementById('customize-schemas');
  const schemaOverrideFields = document.getElementById('schema-override-fields');

  function updateAgentCustomizationVisibility() {
    if (!customizeAgentsCheckbox || !agentCustomizationFields) return;
    const enabled = customizeAgentsCheckbox.checked;
    agentCustomizationFields.style.display = enabled ? '' : 'none';
    const editableInputs = [
      document.getElementById('duplicate-source-tag'),
      document.getElementById('duplicate-target-tag'),
    ];
    editableInputs.forEach((input) => {
      if (!input) return;
      input.disabled = !enabled;
      if (enabled) {
        input.setAttribute('required', 'required');
      } else {
        input.removeAttribute('required');
        input.value = '';
      }
    });
  }

  function updateConductorVisibility() {
    if (!customizeConductorCheckbox || !conductorFields) return;
    const enabled = customizeConductorCheckbox.checked;
    conductorFields.style.display = enabled ? '' : 'none';
  }

  function updateSchemaOverrideVisibility() {
    if (!customizeSchemasCheckbox || !schemaOverrideFields) return;
    const enabled = customizeSchemasCheckbox.checked;
    schemaOverrideFields.style.display = enabled ? '' : 'none';
    if (enabled) {
      schemaOverrideFields.removeAttribute('hidden');
    } else if (!schemaOverrideFields.hasAttribute('hidden')) {
      schemaOverrideFields.setAttribute('hidden', '');
    }
    const inputs = [
      document.getElementById('duplicate-source-schema'),
      document.getElementById('duplicate-target-schema'),
    ];
    inputs.forEach((input) => {
      if (!input) return;
      input.disabled = !enabled;
      if (!enabled) {
        input.value = '';
      }
    });
  }

  if (customizeAgentsCheckbox) {
    updateAgentCustomizationVisibility();
    customizeAgentsCheckbox.addEventListener('change', updateAgentCustomizationVisibility);
  }
  if (customizeConductorCheckbox) {
    updateConductorVisibility();
    customizeConductorCheckbox.addEventListener('change', updateConductorVisibility);
  }
  if (customizeSchemasCheckbox) {
    updateSchemaOverrideVisibility();
    customizeSchemasCheckbox.addEventListener('change', updateSchemaOverrideVisibility);
  }
}

async function loadPipelines() {
  const exportSelect = document.getElementById('export-pipeline-id');
  const configSelect = document.getElementById('pipeline-id');
  const bulkSelect = document.getElementById('bulk-pipeline-id');
  const duplicateSelect = document.getElementById('duplicate-pipeline-id');
  if (!exportSelect && !configSelect && !bulkSelect && !duplicateSelect) return;

  try {
    const data = await api.listPipelines();
    const pipelines = data.pipelines || [];

    // Update connection info in header
    const corehubUrl = document.getElementById('corehub-url')?.value || '';
    ui.setConnectionInfo(corehubUrl, pipelines.length);

    // Sort pipelines by name, then by ID if names are the same
    pipelines.sort((a, b) => {
      const nameA = (a.name || '').toLowerCase();
      const nameB = (b.name || '').toLowerCase();
      if (nameA !== nameB) {
        return nameA.localeCompare(nameB);
      }
      return a.id.localeCompare(b.id);
    });

    const populateSelect = (selectEl) => {
      if (!selectEl) return;
      selectEl.disabled = false;
      const placeholder = selectEl.id === 'duplicate-pipeline-id' ? 'Select a pipeline to duplicate…' : 'Select a pipeline…';
      selectEl.innerHTML = `<option value="">${placeholder}</option>`;
      pipelines.forEach((p) => {
        const pipelineId = p.pipelineId || p.id || p.pipeline_id;
        if (!pipelineId) return;
        const opt = document.createElement('option');
        opt.value = pipelineId;
        const displayId = pipelineId.length > 12 ? pipelineId.slice(0, 8) + '…' : pipelineId;
        const label = p.name ? `${p.name} (${displayId})` : pipelineId;
        opt.textContent = label;
        selectEl.appendChild(opt);
      });
    };

    populateSelect(exportSelect);
    populateSelect(configSelect);
    populateSelect(bulkSelect);
    populateSelect(duplicateSelect);
  } catch (err) {
    console.error(err);
    ui.setExportMessage(err.message, 'error');
    ui.setDuplicateMessage(err.message, 'error');
  }
}

initTabs();
bindEvents();
updateImportButtonsVisibility();
initialize();
