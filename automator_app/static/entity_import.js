// Entity Customize (single-pipeline plan subset): include/exclude, overrides, remap, parts, onExisting, dryRun.
// Reuses the same plan JSON as backup Customize minus agents/globals/resumeInto/name. No passwords.

const PARTS = [
  ['udfs', 'UDFs'],
  ['functionCollection', 'Function collection'],
  ['schedules', 'Schedules'],
  ['groups', 'Groups'],
  ['createTables', 'Create missing tables'],
];

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

async function jsonOrThrow(res, fallback) {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = body.detail;
    throw new Error(typeof detail === 'string' ? detail : (detail && JSON.stringify(detail)) || fallback);
  }
  return body;
}

export function buildEntityPlan(panel, pipelineId) {
  const plan = { version: 1, defaults: {}, pipelines: {} };
  const entry = {};
  const onExisting = panel.querySelector('[data-on-existing]')?.value;
  if (onExisting) plan.defaults.onExistingEntity = onExisting;
  const parts = {};
  PARTS.forEach(([key]) => {
    const box = panel.querySelector(`[data-part="${key}"]`);
    if (box && !box.checked) parts[key] = false;
  });
  if (Object.keys(parts).length) entry.parts = parts;
  const includeRaw = panel.querySelector('[data-include]')?.value?.trim();
  if (includeRaw) {
    entry.entities = {
      include: includeRaw.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean),
    };
  }
  const excludeRaw = panel.querySelector('[data-exclude]')?.value?.trim();
  if (excludeRaw) {
    entry.entities = entry.entities || {};
    entry.entities.exclude = excludeRaw.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
  }
  if (Object.keys(entry).length) plan.pipelines[pipelineId || 'entity'] = entry;
  return plan;
}

export function initEntityImport({ root, getContext, setMessage, log }) {
  if (!root) return { currentPlan() { return null; }, isDryRun() { return false; } };

  const panel = el('div', { className: 'entity-import', id: 'entity-customize-panel' });
  root.append(panel);

  const partsRow = el('div', { className: 'entity-parts' },
    el('span', { className: 'hint-text' }, 'Parts to apply:'),
    ...PARTS.map(([key, label]) => el('label', { className: 'checkbox' },
      el('input', { type: 'checkbox', dataset: { part: key }, checked: true }),
      ` ${label}`)));

  const onExisting = el('label', {}, 'On existing entity ',
    el('select', { dataset: { onExisting: '1' } },
      el('option', { value: '' }, 'default'),
      el('option', { value: 'skip' }, 'skip'),
      el('option', { value: 'update' }, 'update'),
      el('option', { value: 'fail' }, 'fail')));

  const include = el('label', {}, 'Include (SCHEMA.TABLE, one per line; empty = all)',
    el('textarea', { dataset: { include: '1' }, rows: 3, placeholder: 'SCH.TAB1' }));
  const exclude = el('label', {}, 'Exclude (SCHEMA.TABLE)',
    el('textarea', { dataset: { exclude: '1' }, rows: 2, placeholder: '' }));

  const dryRun = el('label', { className: 'checkbox' },
    el('input', { type: 'checkbox', id: 'entity-plan-dry-run' }),
    ' Dry run (preview writes only; nothing created)');

  const previewOut = el('pre', { className: 'entity-plan-preview', id: 'entity-plan-preview-out' });
  const btnRow = el('div', { className: 'button-group' },
    el('button', { type: 'button', className: 'secondary', id: 'entity-plan-draft-btn' }, 'Download plan JSON'),
    el('button', { type: 'button', className: 'secondary', id: 'entity-plan-preview-btn' }, 'Preview'),
  );

  panel.append(
    el('h3', {}, 'Customize entity creation'),
    el('p', { className: 'hint-text' },
      'Optional plan for this run only. Empty choices keep current behaviour. '
      + 'Agents, globals, resumeInto and pipeline rename are backup Customize only.'),
    partsRow, onExisting, include, exclude, dryRun, btnRow, previewOut,
  );

  function currentPlan() {
    const ctx = getContext() || {};
    return buildEntityPlan(panel, ctx.pipelineId);
  }

  function isDryRun() {
    return Boolean(panel.querySelector('#entity-plan-dry-run')?.checked);
  }

  function planIsEmpty(plan) {
    return !plan || (!Object.keys(plan.defaults || {}).length && !Object.keys(plan.pipelines || {}).length);
  }

  btnRow.querySelector('#entity-plan-draft-btn').addEventListener('click', async () => {
    const ctx = getContext() || {};
    if (!ctx.pipelineId || !ctx.yamlFileId) {
      setMessage?.('Select a pipeline and upload a YAML first', 'error');
      return;
    }
    try {
      const res = await fetch('/api/entities/plan/draft', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pipelineId: ctx.pipelineId, yamlFileId: ctx.yamlFileId }),
      });
      const draft = await jsonOrThrow(res, 'Draft failed');
      const blob = new Blob([JSON.stringify(draft, null, 2)], { type: 'application/json' });
      const a = el('a', { href: URL.createObjectURL(blob), download: `entity-plan-${ctx.pipelineId}.json` });
      document.body.append(a); a.click(); a.remove();
      log?.('Config', 'Downloaded entity plan JSON');
    } catch (err) {
      setMessage?.(err.message, 'error');
    }
  });

  btnRow.querySelector('#entity-plan-preview-btn').addEventListener('click', async () => {
    const ctx = getContext() || {};
    if (!ctx.pipelineId || !ctx.yamlFileId) {
      setMessage?.('Select a pipeline and upload a YAML first', 'error');
      return;
    }
    const plan = currentPlan();
    try {
      const res = await fetch('/api/entities/plan/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pipelineId: ctx.pipelineId, yamlFileId: ctx.yamlFileId, plan }),
      });
      const preview = await jsonOrThrow(res, 'Preview failed');
      const lines = [
        `Pipeline ${preview.pipelineId}: ${preview.entityCount} entities`,
        `onExistingEntity: ${preview.onExistingEntity}`,
        `parts: ${JSON.stringify(preview.parts)}`,
      ];
      (preview.entities || []).forEach((e) => {
        lines.push(`  ${e.source} -> ${e.targetSchema}.${e.targetTable}`
          + (e.entityName ? ` (${e.entityName})` : ''));
      });
      if (preview.conflicts?.length) lines.push(`Already in pipeline: ${preview.conflicts.join(', ')}`);
      (preview.notes || []).forEach((n) => lines.push(n));
      previewOut.textContent = lines.join('\n');
      log?.('Config', `Entity plan preview: ${preview.entityCount} entities`);
    } catch (err) {
      previewOut.textContent = '';
      setMessage?.(err.message, 'error');
    }
  });

  return {
    currentPlan,
    isDryRun,
    planPayload() {
      const plan = currentPlan();
      if (planIsEmpty(plan) && !isDryRun()) return {};
      return { plan: planIsEmpty(plan) ? {} : plan, dryRun: isDryRun() };
    },
  };
}
