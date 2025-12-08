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
  async importAll(file) {
    const form = new FormData();
    form.append('file', file);
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

  function setVersionLabel(message) {
    if (!versionLabel) return;
    versionLabel.textContent = message;
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
    lockAuthFields: setAuthFieldsLocked,
    setVersion: setVersionLabel,
    runBtn,
    logoutBtn,
    exportAllBtn,
  };
})();

const stateManager = {
  yamlFileId: null,
  pollHandle: null,
  lastStatus: 'idle',

  updateFromState(data) {
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
      ui.setStatus('ready', 'Ready');
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
      ui.lockAuthFields(true);
      await loadPipelines();
    } else {
      ui.lockAuthFields(false);
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

function bindEvents() {
  const loginForm = document.getElementById('login-form');
  const configForm = document.getElementById('config-form');
  const yamlInput = document.getElementById('yaml-file');
  const exportForm = document.getElementById('export-form');
  const exportPipelineSelect = document.getElementById('export-pipeline-id');
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

  if (customSchemasCheckbox && schemaFields) {
    updateSchemaVisibility();
    customSchemasCheckbox.addEventListener('change', updateSchemaVisibility);
  }

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
      ui.lockAuthFields(true);
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

  if (ui.exportAllBtn) {
    console.log('Export All button found, adding event listener');
    ui.exportAllBtn.addEventListener('click', async () => {
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

  if (importConfigBtnEl && importConfigInput) {
    importConfigBtnEl.addEventListener('click', async () => {
      const file = importConfigInput.files?.[0];
      if (!file) {
        ui.setExportMessage('Please choose a config file to import', 'error');
        return;
      }

      ui.setExportMessage('Importing config…');
      try {
        const result = await api.importConfig(file);
        ui.setExportMessage(result.message || 'Config imported successfully', 'success');
      } catch (err) {
        ui.setExportMessage(err.message, 'error');
      }
    });
  }

  if (importAllBtnEl && importAllInput) {
    importAllBtnEl.addEventListener('click', async () => {
      const file = importAllInput.files?.[0];
      if (!file) {
        ui.setExportMessage('Please choose a backup ZIP to import', 'error');
        return;
      }

      ui.setExportMessage('Importing full backup…');
      try {
        const result = await api.importAll(file);
        ui.setExportMessage(result.message || 'Backup imported successfully', 'success');
      } catch (err) {
        ui.setExportMessage(err.message, 'error');
      }
    });
  }

  if (validateAllBtnEl && importAllInput) {
    validateAllBtnEl.addEventListener('click', async () => {
      const file = importAllInput.files?.[0];
      if (!file) {
        ui.setExportMessage('Please choose a backup ZIP to validate', 'error');
        return;
      }

      ui.setExportMessage('Validating backup…');
      try {
        const result = await api.validateAll(file);
        const ok = result.success !== false;
        const text = result.message || '';
        if (text) {
          const lines = text.split('\n');
          ui.renderLogs(lines);
        }
        ui.setExportMessage(ok ? 'Validation OK' : 'Validation failed', ok ? 'success' : 'error');
      } catch (err) {
        ui.setExportMessage(err.message, 'error');
      }
    });
  }
}

async function loadPipelines() {
  const exportSelect = document.getElementById('export-pipeline-id');
  const configSelect = document.getElementById('pipeline-id');
  if (!exportSelect && !configSelect) return;

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

    const populateSelect = (selectEl) => {
      if (!selectEl) return;
      selectEl.innerHTML = '<option value="">Select a pipeline…</option>';
      pipelines.forEach((p) => {
        const opt = document.createElement('option');
        opt.value = p.id;
        const label = p.name ? `${p.name} (${p.id})` : p.id;
        opt.textContent = label;
        selectEl.appendChild(opt);
      });
    };

    populateSelect(exportSelect);
    populateSelect(configSelect);
  } catch (err) {
    console.error(err);
    ui.setExportMessage(err.message, 'error');
  }
}

bindEvents();
initialize();
