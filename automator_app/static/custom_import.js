// Customizable import of an Export All backup: Analyze -> Customize -> Preview -> Import selected.
// Passwords typed here stay in this page's memory: they are sent with the preview / import request only,
// never put in the plan JSON (Download plan) and never stored.

const PARTS = [
  ['udfs', 'UDFs'],
  ['functionCollection', 'Function collection'],
  ['schedules', 'Schedules'],
  ['groups', 'Groups'],
  ['createTables', 'Create missing tables'],
];
const AS_IN_BACKUP = '(as in backup)';

const GLOBALS = [
  ['notifications', 'Notifications (SMTP)'],
  ['users', 'Users, OIDC and user groups'],
  ['accessPolicies', 'Data access policies'],
];

// The preview gate: "Import selected" is enabled only by the OK preview of the plan on screen. Every edit (typed,
// changed, or Select none / all) bumps the generation; a preview response that comes back after an edit, or for a
// plan that is no longer the one on screen, is ignored (responses may arrive out of order).
export function createPreviewGate() {
  let generation = 0;
  let okKey = null;
  return {
    invalidate() { generation += 1; okKey = null; },
    start(key) { generation += 1; okKey = null; return { generation, key }; },
    isCurrent(ticket, key) { return ticket.generation === generation && ticket.key === key; },
    accept(ticket, key, ok) {
      if (!this.isCurrent(ticket, key)) return false;
      okKey = ok ? key : null;
      return true;
    },
    canImport(key) { return okKey !== null && okKey === key; },
  };
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (value === undefined || value === null || value === false) return;
    if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'className') node.className = value;
    else if (key in node && typeof value !== 'string') node[key] = value;
    else node.setAttribute(key, value === true ? '' : value);
  });
  children.flat().forEach((child) => {
    if (child !== null && child !== undefined) node.append(child instanceof Node ? child : String(child));
  });
  return node;
}

function renderPreview(preview, target) {
  const lines = [];
  preview.pipelines.forEach((p) => {
    if (!p.import) { lines.push(`${p.backupName}: not imported`); return; }
    lines.push(`${p.backupName}: ${p.action || p.error} - ${p.entityCount || 0} entities`);
    (p.entities || []).forEach((e) => lines.push(`  ${e.source} -> ${e.targetSchema}.${e.targetTable}`
      + (e.entityName ? ` (${e.entityName})` : '')));
    (p.agents || []).forEach((a) => lines.push(`  agent ${a.agentTag}: ${a.host}:${a.port} db=${a.database} `
      + `user=${a.username} password: ${a.password}`));
    if (p.conflicts?.length) lines.push(`  already in the pipeline: ${p.conflicts.join(', ')} (on existing: ${p.onExistingEntity})`);
    if (p.notes?.length) p.notes.forEach((n) => lines.push(`  ${n}`));
  });
  lines.push(
    `Globals: ${Object.entries(preview.globals).filter(([, v]) => v).map(([k]) => k).join(', ') || 'none'}`,
    `Total: ${preview.totals.pipelines} pipeline(s), ${preview.totals.entities} entities`,
  );
  preview.errors.forEach((e) => lines.push(`PROBLEM: ${paraphrasePreviewError(e)}`));
  target.textContent = lines.join('\n');
}

async function jsonOrThrow(res, fallback) {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || fallback);
  return body;
}


function paraphrasePreviewError(raw) {
  const text = String(raw || '');
  if (/empty include|include.*empty|no entities/i.test(text)) {
    return 'No entities selected. Tick at least one table, or turn the pipeline off.';
  }
  if (/pipeline.*off|import.*false|not selected/i.test(text)) {
    return 'This pipeline is switched off in the plan. Enable it or remove it from the selection.';
  }
  if (/masked|password|\*{3,}/i.test(text)) {
    return 'A masked password is still present. Fill the SOURCE/TARGET password fields, then Preview again.';
  }
  if (/already exists|onExisting|conflict/i.test(text)) {
    return 'Something already exists on the target. Adjust Existing entities (skip / update / fail) and Preview again.';
  }
  if (/uploadId|expired|not found/i.test(text)) {
    return 'The analysed backup expired. Choose the ZIP again and press Customize.';
  }
  return text.length > 220 ? `${text.slice(0, 217)}…` : text;
}

function countCreateTables(plan, analysis) {
  let n = 0;
  const createDefault = plan.defaults?.createTables !== false;
  (analysis.pipelines || []).forEach((pipeline) => {
    const entry = plan.pipelines?.[pipeline.oldId];
    if (entry?.import === false) return;
    const partsCreate = entry?.parts?.createTables;
    const createOn = partsCreate === undefined ? createDefault : partsCreate;
    if (!createOn) return;
    const include = entry?.entities?.include;
    const selected = include || (pipeline.entities || []).map((e) => e.id);
    n += selected.length;
  });
  return n;
}

let sharedPasswords = {};

/** Passwords collected in the Customize panel (in-memory only). */
export function getCustomImportPasswords() {
  return sharedPasswords;
}


export function initCustomImport({ fileInput, root, setMessage, log }) {
  if (!fileInput || !root) return;
  let session = null; // {uploadId, analysis}
  let passwords = {}; // {pipelineId: {agentTag: password}} in memory only

  const analyzeBtn = document.getElementById('import-analyze-btn');
  const panel = el('div', { className: 'custom-import', hidden: true });
  root.append(panel);

  function reset() {
    session = null;
    passwords = {};
    sharedPasswords = {};
    panel.replaceChildren();
    panel.hidden = true;
  }

  fileInput.addEventListener('change', () => {
    reset();
    if (analyzeBtn) analyzeBtn.disabled = !fileInput.files?.[0];
  });

  function buildPlan() {
    const plan = { version: 1, globals: {}, defaults: {}, pipelines: {} };
    panel.querySelectorAll('[data-global]').forEach((box) => {
      if (!box.checked) plan.globals[box.dataset.global] = false;
    });
    panel.querySelectorAll('[data-default-part]').forEach((box) => {
      if (!box.checked) plan.defaults[box.dataset.defaultPart] = false;
    });
    const onEntity = panel.querySelector('[data-default="onExistingEntity"]')?.value;
    if (onEntity) plan.defaults.onExistingEntity = onEntity;
    const onPipeline = panel.querySelector('[data-default="onExistingPipeline"]')?.value;
    if (onPipeline) plan.defaults.onExistingPipeline = onPipeline;
    session.analysis.pipelines.forEach((pipeline) => {
      const box = panel.querySelector(`[data-pipeline="${CSS.escape(pipeline.oldId)}"]`);
      if (!box) return;
      const entry = {
        import: box.querySelector('[data-field="import"]').checked,
      };
      const name = box.querySelector('[data-field="name"]').value.trim();
      if (name && name !== pipeline.name) entry.name = name;
      const resume = box.querySelector('[data-field="resumeInto"]').value.trim();
      if (resume) entry.resumeInto = resume;
      const include = [];
      const overrides = {};
      box.querySelectorAll('[data-entity]').forEach((row) => {
        const id = row.dataset.entity;
        if (row.querySelector('[data-field="selected"]').checked) include.push(id);
        const override = {};
        ['targetSchema', 'targetTable', 'entityName'].forEach((field) => {
          const value = row.querySelector(`[data-field="${field}"]`).value.trim();
          if (value) override[field] = value;
        });
        if (Object.keys(override).length) overrides[id] = override;
      });
      entry.entities = {};
      if (include.length !== pipeline.entities.length) entry.entities.include = include;
      if (Object.keys(overrides).length) entry.entities.overrides = overrides;
      if (!Object.keys(entry.entities).length) delete entry.entities;
      const agents = {};
      box.querySelectorAll('[data-agent]').forEach((row) => {
        const original = pipeline.agents.find((a) => String(a.agentTag || a.agentId) === row.dataset.agent) || {};
        const changed = {};
        ['host', 'port', 'database', 'username'].forEach((field) => {
          const raw = row.querySelector(`[data-field="${field}"]`).value.trim();
          if (raw && raw !== String(original[field] ?? '')) {
            changed[field] = field === 'port' && /^\d+$/.test(raw) ? Number(raw) : raw;
          }
        });
        if (Object.keys(changed).length) agents[row.dataset.agent] = changed;
      });
      if (Object.keys(agents).length) entry.agents = agents;
      const parts = {};
      box.querySelectorAll('[data-part]').forEach((sel) => {
        if (sel.value) parts[sel.dataset.part] = sel.value === 'on';
      });
      if (Object.keys(parts).length) entry.parts = parts;
      const pOnEntity = box.querySelector('[data-field="onExistingEntity"]').value;
      if (pOnEntity) entry.onExistingEntity = pOnEntity;
      if (Object.keys(entry).length > 1 || entry.import === false) plan.pipelines[pipeline.oldId] = entry;
    });
    ['globals', 'defaults'].forEach((key) => {
      if (!Object.keys(plan[key]).length) delete plan[key];
    });
    return plan;
  }

  function collectPasswords() {
    passwords = {};
    panel.querySelectorAll('[data-agent-password]').forEach((input) => {
      if (!input.value) return;
      const [pid, ref] = [input.dataset.pipelineId, input.dataset.agentPassword];
      passwords[pid] = passwords[pid] || {};
      passwords[pid][ref] = input.value;
    });
    sharedPasswords = passwords;
    return passwords;
  }

  function onExistingSelect(attrs, label) {
    return el('label', {}, label, el('select', attrs,
      el('option', { value: '' }, 'keep current behaviour'),
      el('option', { value: 'skip' }, 'skip (leave as-is)'),
      el('option', { value: 'update' }, 'update in place'),
      el('option', { value: 'fail' }, 'fail if it already exists')));
  }

  function renderPipeline(pipeline) {
    const tables = el('table', { className: 'custom-import-tables' },
      el('thead', {}, el('tr', {}, el('th', {}, 'Import'), el('th', {}, 'Source table'),
        el('th', {}, 'Target schema'), el('th', {}, 'Target table'), el('th', {}, 'Entity name'))),
      el('tbody', {}, pipeline.entities.map((entity) => el('tr', { dataset: { entity: entity.id } },
        el('td', {}, el('input', { type: 'checkbox', checked: true, dataset: { field: 'selected' } })),
        el('td', {}, entity.id),
        el('td', {}, el('input', { type: 'text', placeholder: AS_IN_BACKUP, dataset: { field: 'targetSchema' } })),
        el('td', {}, el('input', { type: 'text', placeholder: AS_IN_BACKUP, dataset: { field: 'targetTable' } })),
        el('td', {}, el('input', { type: 'text', placeholder: AS_IN_BACKUP, dataset: { field: 'entityName' } }))))));
    const toggleAll = el('button', { type: 'button', className: 'secondary small' }, 'Select none / all');
    toggleAll.addEventListener('click', () => {
      const boxes = [...tables.querySelectorAll('[data-field="selected"]')];
      const value = !boxes.every((b) => b.checked);
      boxes.forEach((b) => { b.checked = value; });
      // Setting .checked fires no event: tell the panel, so the last preview no longer enables the import.
      tables.dispatchEvent(new Event('change', { bubbles: true }));
    });
    const agents = el('div', { className: 'custom-import-agents' }, pipeline.agents.map((agent) => {
      const ref = String(agent.agentTag || agent.agentId);
      const role = String(agent.agentType || '').toUpperCase();
      const badge = (role === 'SOURCE' || role === 'TARGET')
        ? el('span', { className: `agent-role-badge agent-role-badge--${role.toLowerCase()}` }, role)
        : null;
      const pwdCaption = el('span', { className: 'custom-import-password-caption' },
        badge ? badge.cloneNode(true) : null,
        ` Password (${agent.password}; leave empty to keep)`);
      return el('div', { className: 'custom-import-agent', dataset: { agent: ref } },
        el('div', { className: 'custom-import-agent-title' },
          badge, el('strong', {}, `${agent.agentType} agent ${ref}`)),
        ['host', 'port', 'database', 'username'].map((field) => el('label', {}, field,
          el('input', { type: 'text', value: agent[field] ?? '', dataset: { field } }))),
        el('label', { className: 'custom-import-password-label' }, pwdCaption,
          el('input', { type: 'password', autocomplete: 'new-password',
            dataset: { agentPassword: ref, pipelineId: pipeline.oldId } })));
    }));
    const parts = el('div', { className: 'custom-import-parts' }, PARTS.map(([key, label]) => el('label', {}, label,
      el('select', { dataset: { part: key } }, el('option', { value: '' }, 'default'),
        el('option', { value: 'on' }, 'import'), el('option', { value: 'off' }, 'skip')))));
    const counts = pipeline.parts || {};
    return el('details', { className: 'custom-import-pipeline', open: true, dataset: { pipeline: pipeline.oldId } },
      el('summary', {}, el('input', { type: 'checkbox', checked: true, dataset: { field: 'import' } }),
        ` ${pipeline.name} (backup id ${pipeline.oldId}, ${pipeline.entities.length} tables, `
        + `${counts.udfs || 0} UDFs, ${counts.groups || 0} groups)`),
      el('div', { className: 'form-grid' },
        el('label', {}, 'Pipeline name', el('input', { type: 'text', value: pipeline.name, dataset: { field: 'name' } })),
        el('label', {}, 'Resume into existing pipeline id (optional)',
          el('input', { type: 'text', placeholder: 'create a new pipeline', dataset: { field: 'resumeInto' } })),
        onExistingSelect({ dataset: { field: 'onExistingEntity' } }, 'Existing entities')),
      el('h4', {}, 'Parts'), parts,
      el('h4', {}, 'Agents'), agents,
      el('h4', {}, 'Tables '), toggleAll, tables);
  }

  function render(analysis) {
    const previewOut = el('pre', { className: 'custom-import-preview' });
    const previewBtn = el('button', { type: 'button', className: 'secondary' }, 'Preview');
    const importBtn = el('button', { type: 'button', className: 'primary', disabled: true }, 'Import selected');
    const downloadBtn = el('button', { type: 'button', className: 'secondary' }, 'Download plan JSON');
    const summaryChip = el('span', { className: 'import-summary-chip', hidden: true }, '');
    const stepper = el('ol', {
      className: 'import-stepper',
      dataset: { importStepper: '1' },
      'aria-label': 'Import steps',
    },
      ...['Analyze', 'Customize', 'Preview', 'Import'].map((label, i) => {
        let stepClass = '';
        if (i === 1) stepClass = 'is-current';
        else if (i === 0) stepClass = 'is-done';
        return el('li', {
          dataset: { step: String(i) },
          className: stepClass,
        }, el('span', { className: 'import-stepper-index' }, String(i + 1)), el('span', {}, label));
      }));
    const gate = createPreviewGate();
    const planKey = () => JSON.stringify(buildPlan());
    const setStep = (idx) => {
      stepper.querySelectorAll('[data-step]').forEach((node) => {
        const n = Number(node.dataset.step);
        node.classList.toggle('is-current', n === idx);
        node.classList.toggle('is-done', n < idx);
      });
    };
    const refreshChip = () => {
      const n = countCreateTables(buildPlan(), analysis);
      summaryChip.textContent = n === 1 ? '1 table will be created' : `${n} tables will be created`;
      summaryChip.hidden = false;
    };
    const invalidate = () => {
      gate.invalidate();
      importBtn.disabled = true;
      setStep(1);
      refreshChip();
    };
    const actionBar = el('div', { className: 'custom-import-actions sticky-action-bar' },
      summaryChip, previewBtn, importBtn, downloadBtn);
    panel.replaceChildren(
      stepper,
      el('h3', {}, 'Customize the import'),
      el('p', { className: 'hint-text' },
        'Enter SOURCE and TARGET passwords when the backup shows masked values, then Preview before Import selected.'),
      el('fieldset', { className: 'fieldset' }, el('legend', {}, 'Global settings'),
        GLOBALS.map(([key, label]) => el('label', { className: 'checkbox' },
          el('input', { type: 'checkbox', checked: true, disabled: !analysis.globals[key], dataset: { global: key } }),
          ` ${label}${analysis.globals[key] ? '' : ' (not in backup)'}`))),
      el('fieldset', { className: 'fieldset' }, el('legend', {}, 'Defaults for every pipeline'),
        PARTS.map(([key, label]) => el('label', { className: 'checkbox' },
          el('input', { type: 'checkbox', checked: true, dataset: { defaultPart: key } }), ` ${label}`)),
        onExistingSelect({ dataset: { default: 'onExistingEntity' } }, 'If an entity already exists'),
        el('label', {}, 'Pipeline name already taken', el('select', { dataset: { default: 'onExistingPipeline' } },
          el('option', { value: '' }, 'add "(restored)" (default)'), el('option', { value: 'fail' }, 'fail')))),
      ...analysis.pipelines.map(renderPipeline),
      analysis.errors?.length
        ? el('p', { className: 'message error' }, analysis.errors.map(paraphrasePreviewError).join('\n'))
        : null,
      actionBar,
      previewOut);
    panel.hidden = false;
    refreshChip();
    panel.addEventListener('input', invalidate);
    panel.addEventListener('change', invalidate);

    previewBtn.addEventListener('click', async () => {
      setMessage('Previewing the import (read-only)…');
      importBtn.disabled = true;
      const plan = buildPlan();
      const ticket = gate.start(JSON.stringify(plan));
      try {
        const res = await fetch('/api/import/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ uploadId: session.uploadId, plan, agent_passwords: collectPasswords() }),
        });
        const preview = await jsonOrThrow(res, 'Preview failed');
        if (!gate.accept(ticket, planKey(), preview.ok)) return; // stale: the plan changed meanwhile
        renderPreview(preview, previewOut);
        importBtn.disabled = !gate.canImport(planKey());
        setStep(2);
        if (preview.ok) {
          setMessage('Preview OK: review it, then Import selected', 'success');
        } else {
          const problems = (preview.errors || []).map(paraphrasePreviewError).join(' · ');
          setMessage(problems || 'The preview found problems', 'error');
        }
      } catch (err) {
        if (gate.isCurrent(ticket, planKey())) setMessage(paraphrasePreviewError(err.message), 'error');
      }
    });

    importBtn.addEventListener('click', async () => {
      if (!gate.canImport(planKey())) { importBtn.disabled = true; setMessage('Preview the current plan first', 'error'); return; }
      const form = new FormData();
      form.append('uploadId', session.uploadId);
      form.append('plan', JSON.stringify(buildPlan()));
      form.append('agent_passwords', JSON.stringify(collectPasswords()));
      importBtn.disabled = true;
      setStep(3);
      setMessage('Importing the selected pipelines…');
      log('Config', 'Importing the customized backup…');
      try {
        const res = await fetch('/api/import/all', { method: 'POST', body: form });
        const result = await jsonOrThrow(res, 'Import failed');
        (result.logs || []).forEach((line) => log('Config', line));
        setMessage(result.message || 'Import finished', result.success === false ? 'error' : 'success');
        log('Config', result.message || 'Customized import finished');
        if (res.status !== 409) reset(); // the stored upload is deleted after the import
      } catch (err) {
        setMessage(paraphrasePreviewError(err.message), 'error');
        log('Config', `Customized import failed: ${err.message}`);
        importBtn.disabled = !gate.canImport(planKey());
        setStep(2);
      }
    });

    downloadBtn.addEventListener('click', () => {
      const blob = new Blob([JSON.stringify(buildPlan(), null, 2)], { type: 'application/json' });
      const link = el('a', { href: URL.createObjectURL(blob), download: 'import-plan.json' });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    });
  }

  if (analyzeBtn) {
    analyzeBtn.addEventListener('click', async () => {
      const file = fileInput.files?.[0];
      if (!file) { setMessage('Please choose a backup ZIP to analyse', 'error'); return; }
      reset();
      setMessage('Analysing the backup…');
      try {
        const form = new FormData();
        form.append('file', file);
        const res = await fetch('/api/import/analyze', { method: 'POST', body: form });
        const analysis = await jsonOrThrow(res, 'Analysis failed');
        session = { uploadId: analysis.uploadId, analysis };
        render(analysis);
        setMessage('Enter agent passwords if needed, then Preview', 'success');
      } catch (err) {
        setMessage(err.message, 'error');
      }
    });
  }
}
