const api = {
  async getState() {
    const res = await fetch('/api/state');
    if (!res.ok) throw new Error(`State request failed: ${res.status}`);
    return res.json();
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
};

const ui = (() => {
  const statusEl = document.getElementById('status-indicator');
  const authMessageEl = document.getElementById('auth-message');
  const configMessageEl = document.getElementById('config-message');
  const exportMessageEl = document.getElementById('export-message');
  const logoutBtn = document.getElementById('logout-btn');
  const runBtn = document.getElementById('run-btn');
  const exportBtn = document.getElementById('export-btn');
  const exportAllBtn = document.getElementById('export-all-btn');
  const logOutput = document.getElementById('log-output');
  const logTemplate = document.getElementById('log-line-template');
  const footerYear = document.getElementById('footer-year');

  footerYear.textContent = new Date().getFullYear();

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

  function setAuthEnabled(enabled) {
    logoutBtn.disabled = !enabled;
    document.querySelector('#login-form button[type="submit"]').disabled = enabled;
    runBtn.disabled = !enabled;
    if (exportBtn) {
      exportBtn.disabled = !enabled;
    }
    if (exportAllBtn) {
      exportAllBtn.disabled = !enabled;
    }
  }

  function renderLogs(logs = []) {
    logOutput.innerHTML = '';
    logs.forEach((line) => {
      const node = logTemplate.content.firstElementChild.cloneNode(true);
      node.textContent = line;
      logOutput.appendChild(node);
    });
    logOutput.scrollTop = logOutput.scrollHeight;
  }

  return {
    setStatus,
    setAuthMessage,
    setConfigMessage,
    setExportMessage,
    setAuthEnabled,
    renderLogs,
    runBtn,
    logoutBtn,
  };
})();

const stateManager = {
  yamlFileId: null,
  pollHandle: null,
  lastStatus: 'idle',

  updateFromState(data) {
    document.getElementById('corehub-url').value = data.baseUrl || '';
    document.getElementById('use-ssl').checked = !!data.useSsl;
    document.getElementById('skip-verify').checked = !!data.skipVerify;
    document.getElementById('enable-scheduling').checked = data.enableScheduling;
    document.getElementById('create-tables').checked = data.createTables;

    ui.setAuthEnabled(data.tokenPresent);
    if (!data.tokenPresent) {
      ui.setStatus('idle', 'Not authenticated');
      ui.renderLogs();
      this.yamlFileId = null;
      return;
    }

    const run = data.run;
    if (run) {
      this.lastStatus = run.status;
      ui.setStatus(run.status, run.status.toUpperCase());
    } else {
      ui.setStatus('idle', 'Ready');
    }
  },

  setYamlFile(fileId) {
    this.yamlFileId = fileId;
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
      await loadPipelines();
    }
  } catch (err) {
    console.error(err);
    ui.setStatus('failed', 'API unavailable');
  }
}

function bindEvents() {
  const loginForm = document.getElementById('login-form');
  const configForm = document.getElementById('config-form');
  const yamlInput = document.getElementById('yaml-file');
  const exportForm = document.getElementById('export-form');
  const exportPipelineSelect = document.getElementById('export-pipeline-id');

  loginForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    ui.setAuthMessage('Signing in…');

    const payload = {
      baseUrl: document.getElementById('corehub-url').value,
      username: document.getElementById('username').value,
      password: document.getElementById('password').value,
      useSsl: document.getElementById('use-ssl').checked,
      skipVerify: document.getElementById('skip-verify').checked,
      enableScheduling: document.getElementById('enable-scheduling').checked,
      createTables: document.getElementById('create-tables').checked,
    };

    try {
      await api.login(payload);
      ui.setAuthMessage('Authentication successful', 'success');
      const snapshot = await api.getState();
      stateManager.updateFromState(snapshot);
      await loadPipelines();
    } catch (err) {
      ui.setAuthMessage(err.message, 'error');
      ui.setAuthEnabled(false);
    }
  });

  ui.logoutBtn.addEventListener('click', async () => {
    try {
      await api.logout();
      ui.setAuthMessage('Logged out', 'success');
      ui.setAuthEnabled(false);
      ui.setStatus('idle', 'Not authenticated');
      stateManager.stopPolling();
      ui.renderLogs();
      if (exportPipelineSelect) {
        exportPipelineSelect.innerHTML = '<option value="">Select a pipeline…</option>';
      }
    } catch (err) {
      ui.setAuthMessage(err.message, 'error');
    }
  });

  yamlInput.addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    ui.setConfigMessage('Uploading…');
    try {
      const response = await api.uploadYaml(file);
      stateManager.setYamlFile(response.fileId);
      ui.setConfigMessage(`Uploaded ${response.filename}`, 'success');
    } catch (err) {
      ui.setConfigMessage(err.message, 'error');
      stateManager.setYamlFile(null);
    }
  });

  configForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!stateManager.yamlFileId) {
      ui.setConfigMessage('Please upload a YAML configuration file first', 'error');
      return;
    }

    const payload = {
      pipelineId: document.getElementById('pipeline-id').value,
      sourceSchema: document.getElementById('source-schema').value,
      targetSchema: document.getElementById('target-schema').value,
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

    try {
      await api.startRun(payload);
      stateManager.startPolling();
      ui.setConfigMessage('Entity creation started', 'success');
    } catch (err) {
      ui.setConfigMessage(err.message, 'error');
      ui.setAuthEnabled(true);
      ui.setStatus('failed', 'Failed to start');
    }
  });

  if (exportForm && exportPipelineSelect) {
    exportForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const pipelineId = exportPipelineSelect.value;
      if (!pipelineId) {
        ui.setExportMessage('Please select a pipeline to export', 'error');
        return;
      }

      ui.setExportMessage('Exporting…');
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
      } catch (err) {
        ui.setExportMessage(err.message, 'error');
      }
    });
  }

  if (exportAllBtn) {
    console.log('Export All button found, adding event listener');
    exportAllBtn.addEventListener('click', async () => {
      console.log('Export All button clicked');
      ui.setExportMessage('Exporting all pipelines…');
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
      } catch (err) {
        console.error('Export All failed:', err);
        ui.setExportMessage(err.message, 'error');
      }
    });
  } else {
    console.error('Export All button not found!');
  }
}

async function loadPipelines() {
  const select = document.getElementById('export-pipeline-id');
  if (!select) return;

  try {
    const data = await api.listPipelines();
    const pipelines = data.pipelines || [];

    // Sort pipelines by name, then by ID if names are the same
    pipelines.sort((a, b) => {
      const nameA = (a.name || '').toLowerCase();
      const nameB = (b.name || '').toLowerCase();
      if (nameA !== nameB) {
        return nameA.localeCompare(nameB);
      }
      return a.id.localeCompare(b.id);
    });

    select.innerHTML = '<option value="">Select a pipeline…</option>';
    pipelines.forEach((p) => {
      const opt = document.createElement('option');
      opt.value = p.id;
      const label = p.name ? `${p.name} (${p.id})` : p.id;
      opt.textContent = label;
      select.appendChild(opt);
    });
  } catch (err) {
    console.error(err);
    ui.setExportMessage(err.message, 'error');
  }
}

bindEvents();
initialize();
