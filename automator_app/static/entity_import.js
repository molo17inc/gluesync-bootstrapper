// Entity Customize (single-pipeline plan subset): picker, include/exclude, parts, onExisting, dryRun.
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

function splitLines(raw) {
  return (raw || '').split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
}

function advancedIncludeExclude(panel) {
  const include = splitLines(panel.querySelector('[data-include]')?.value);
  const exclude = splitLines(panel.querySelector('[data-exclude]')?.value);
  return { include, exclude, used: include.length > 0 || exclude.length > 0 };
}

/**
 * Build the plan from the panel.
 * @param selectedIds Set|null  null = picker not loaded (omit include); Set of exact ids when loaded.
 */
export function buildEntityPlan(panel, pipelineId, loadedEntities, selectedIds) {
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

  const advanced = advancedIncludeExclude(panel);
  if (advanced.used) {
    entry.entities = {};
    if (advanced.include.length) entry.entities.include = advanced.include;
    if (advanced.exclude.length) entry.entities.exclude = advanced.exclude;
  } else if (loadedEntities && loadedEntities.length && selectedIds) {
    if (selectedIds.size < loadedEntities.length) {
      entry.entities = { include: loadedEntities.map((e) => e.id).filter((id) => selectedIds.has(id)) };
    }
  }

  if (Object.keys(entry).length) plan.pipelines[pipelineId || 'entity'] = entry;
  return plan;
}

export function planIsEmpty(plan) {
  return !plan || (!Object.keys(plan.defaults || {}).length && !Object.keys(plan.pipelines || {}).length);
}

export function selectionError(panel, loadedEntities, selectedIds) {
  const advanced = advancedIncludeExclude(panel);
  if (advanced.used) return null;
  if (!loadedEntities || !loadedEntities.length) return null;
  if (!selectedIds || selectedIds.size === 0) {
    return 'Select at least one entity (or use Advanced include), or Select all to import every table';
  }
  return null;
}

function chainMembers(loaded, chainId) {
  if (!chainId) return [];
  return loaded.filter((e) => e.chainId && String(e.chainId) === String(chainId));
}


function rowMatchesFilter(entity, filter) {
  if (!filter) return true;
  const hay = [entity.id, entity.entityName, entity.source,
    `${entity.targetSchema}.${entity.targetTable}`, entity.schema, entity.chainId]
    .filter(Boolean).join(' ').toLowerCase();
  return hay.includes(filter);
}

function groupBySchema(entities) {
  const groups = new Map();
  entities.forEach((e) => {
    const key = e.schema || '(no schema)';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  });
  return groups;
}

export function initEntityImport({ root, getContext, setMessage, log }) {
  if (!root) return { currentPlan() { return null; }, isDryRun() { return false; } };

  let loadedEntities = [];
  let selectedIds = null; // null until first successful load; then a Set of exact ids

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

  const pickerStatus = el('p', { className: 'hint-text', id: 'entity-picker-status' },
    'Upload a YAML and select a pipeline to load entities.');
  const pickerFilter = el('input', {
    type: 'search', className: 'entity-picker-filter',
    placeholder: 'Filter by name, id, schema…', dataset: { pickerFilter: '1' },
  });
  const pickerCount = el('span', { className: 'entity-picker-count', dataset: { pickerCount: '1' } }, '');
  const pickerWarn = el('p', { className: 'hint-text entity-picker-warn', dataset: { pickerWarn: '1' } });
  const pickerToolbar = el('div', { className: 'entity-picker-toolbar' },
    el('button', { type: 'button', className: 'secondary', dataset: { selectAll: '1' } }, 'Select all'),
    el('button', { type: 'button', className: 'secondary', dataset: { selectNone: '1' } }, 'Select none'),
    pickerFilter, pickerCount);
  const pickerBody = el('div', { className: 'entity-picker-body', dataset: { pickerBody: '1' } });
  const picker = el('div', { className: 'entity-picker', id: 'entity-picker' },
    el('h4', {}, 'Entities to import'),
    pickerStatus, pickerToolbar, pickerWarn, pickerBody);

  const include = el('label', {}, 'Include (SCHEMA.KEY or unique entityName, one per line)',
    el('textarea', { dataset: { include: '1' }, rows: 3, placeholder: 'SCH.TAB1' }));
  const exclude = el('label', {}, 'Exclude (SCHEMA.KEY or unique entityName)',
    el('textarea', { dataset: { exclude: '1' }, rows: 2, placeholder: '' }));
  const advanced = el('details', { className: 'entity-advanced' },
    el('summary', {}, 'Advanced: free-text include / exclude'),
    el('p', { className: 'hint-text' },
      'When filled, these override the checkbox picker. Empty include with a loaded picker means all checked.'),
    include, exclude);

  const dryRun = el('label', { className: 'checkbox' },
    el('input', { type: 'checkbox', id: 'entity-plan-dry-run' }),
    ' Dry run (preview writes only; nothing created)');

  const previewOut = el('pre', { className: 'entity-plan-preview', id: 'entity-plan-preview-out' });
  const btnRow = el('div', { className: 'button-group' },
    el('button', { type: 'button', className: 'secondary', id: 'entity-plan-draft-btn' }, 'Download plan JSON'),
    el('button', { type: 'button', className: 'secondary', id: 'entity-plan-preview-btn' }, 'Preview'),
    el('button', { type: 'button', className: 'secondary', id: 'entity-plan-reload-btn' }, 'Reload entities'),
  );

  panel.append(
    el('h3', {}, 'Customize entity creation'),
    el('p', { className: 'hint-text' },
      'Optional plan for this run only. Empty choices keep current behaviour. '
      + 'Pick entities below after upload; Agents, globals, resumeInto and pipeline rename are backup Customize only.'),
    partsRow, onExisting, picker, advanced, dryRun, btnRow, previewOut,
  );

  function currentPlan() {
    const ctx = getContext() || {};
    return buildEntityPlan(panel, ctx.pipelineId, loadedEntities, selectedIds);
  }

  function isDryRun() {
    return Boolean(panel.querySelector('#entity-plan-dry-run')?.checked);
  }

  function updateCount() {
    const total = loadedEntities.length;
    const n = selectedIds ? selectedIds.size : 0;
    pickerCount.textContent = total ? `${n} of ${total} selected` : '';
    const partial = selectedIds ? partialChains(loadedEntities, selectedIds) : [];
    pickerWarn.textContent = partial.length
      ? `Warning: MultiTable chain(s) partly selected: ${partial.join(', ')}. `
        + 'Chains should be selected or deselected together.'
      : '';
  }

  function partialChains(loaded, checkedSet) {
    const byChain = new Map();
    loaded.forEach((e) => {
      if (!e.chainId) return;
      if (!byChain.has(e.chainId)) byChain.set(e.chainId, []);
      byChain.get(e.chainId).push(e.id);
    });
    const out = [];
    byChain.forEach((members, chainId) => {
      const n = members.filter((id) => checkedSet.has(id)).length;
      if (n > 0 && n < members.length) out.push(String(chainId));
    });
    return out;
  }

  function onBoxChange(entity, checked) {
    if (!selectedIds) selectedIds = new Set();
    const targets = entity.chainId
      ? chainMembers(loadedEntities, entity.chainId)
      : [entity];
    targets.forEach((member) => {
      if (checked) selectedIds.add(member.id);
      else selectedIds.delete(member.id);
      const box = panel.querySelector(`[data-entity-id="${CSS.escape(member.id)}"]`);
      if (box) box.checked = checked;
    });
    updateCount();
  }

  function renderPicker() {
    pickerBody.innerHTML = '';
    const filter = (pickerFilter.value || '').trim().toLowerCase();
    const visible = loadedEntities.filter((e) => rowMatchesFilter(e, filter));
    if (!loadedEntities.length) {
      pickerStatus.textContent = 'Upload a YAML and select a pipeline to load entities.';
      updateCount();
      return;
    }
    pickerStatus.textContent = filter
      ? `Showing ${visible.length} of ${loadedEntities.length} entities.`
      : `${loadedEntities.length} entities loaded from the export file.`;
    const table = el('table', { className: 'entity-picker-table' });
    table.append(el('thead', {}, el('tr', {},
      el('th', {}, ''),
      el('th', {}, 'entityName'),
      el('th', {}, 'Id'),
      el('th', {}, 'Source → target'),
      el('th', {}, 'Type'),
      el('th', {}, 'Exists'),
    )));
    const tbody = el('tbody');
    const groups = groupBySchema(visible);
    groups.forEach((rows, schema) => {
      tbody.append(el('tr', { className: 'entity-picker-schema' },
        el('td', { colSpan: 6 }, schema)));
      rows.forEach((entity) => {
        const isChecked = !selectedIds || selectedIds.has(entity.id);
        const box = el('input', {
          type: 'checkbox',
          checked: isChecked,
          dataset: { entityId: entity.id, chainId: entity.chainId || '' },
        });
        box.addEventListener('change', () => onBoxChange(entity, box.checked));
        const exists = entity.alreadyExists ? 'yes' : '';
        const typeLabel = entity.type || '';
        const nameLabel = entity.entityName || '—';
        const chainNote = entity.chainId ? ` [chain ${entity.chainId}]` : '';
        tbody.append(el('tr', {},
          el('td', {}, box),
          el('td', {}, nameLabel + chainNote),
          el('td', { className: 'mono' }, entity.id),
          el('td', {}, `${entity.source} → ${entity.targetSchema}.${entity.targetTable}`),
          el('td', {}, typeLabel),
          el('td', {}, exists),
        ));
      });
    });
    table.append(tbody);
    pickerBody.append(table);
    updateCount();
  }

  pickerToolbar.querySelector('[data-select-all]').addEventListener('click', () => {
    selectedIds = new Set(loadedEntities.map((e) => e.id));
    renderPicker();
  });
  pickerToolbar.querySelector('[data-select-none]').addEventListener('click', () => {
    selectedIds = new Set();
    renderPicker();
  });
  pickerFilter.addEventListener('input', () => renderPicker());

  async function reloadEntities() {
    const ctx = getContext() || {};
    if (!ctx.pipelineId || !ctx.yamlFileId) {
      loadedEntities = [];
      selectedIds = null;
      renderPicker();
      return;
    }
    try {
      const res = await fetch('/api/entities/plan/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pipelineId: ctx.pipelineId, yamlFileId: ctx.yamlFileId, plan: {} }),
      });
      const preview = await jsonOrThrow(res, 'Could not load entities');
      loadedEntities = preview.entities || [];
      selectedIds = new Set(loadedEntities.map((e) => e.id));
      renderPicker();
      log?.('Config', `Loaded ${loadedEntities.length} entities for picker`);
    } catch (err) {
      loadedEntities = [];
      selectedIds = null;
      renderPicker();
      setMessage?.(err.message, 'error');
    }
  }

  btnRow.querySelector('#entity-plan-reload-btn').addEventListener('click', () => reloadEntities());

  btnRow.querySelector('#entity-plan-draft-btn').addEventListener('click', async () => {
    const ctx = getContext() || {};
    if (!ctx.pipelineId || !ctx.yamlFileId) {
      setMessage?.('Select a pipeline and upload a YAML first', 'error');
      return;
    }
    const err = selectionError(panel, loadedEntities, selectedIds);
    if (err) {
      setMessage?.(err, 'error');
      return;
    }
    try {
      const plan = currentPlan();
      const blob = new Blob([JSON.stringify(plan, null, 2)], { type: 'application/json' });
      const a = el('a', { href: URL.createObjectURL(blob), download: `entity-plan-${ctx.pipelineId}.json` });
      document.body.append(a); a.click(); a.remove();
      log?.('Config', 'Downloaded entity plan JSON (current selection)');
    } catch (downloadErr) {
      setMessage?.(downloadErr.message, 'error');
    }
  });

  btnRow.querySelector('#entity-plan-preview-btn').addEventListener('click', async () => {
    const ctx = getContext() || {};
    if (!ctx.pipelineId || !ctx.yamlFileId) {
      setMessage?.('Select a pipeline and upload a YAML first', 'error');
      return;
    }
    const err = selectionError(panel, loadedEntities, selectedIds);
    if (err) {
      setMessage?.(err, 'error');
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
          + (e.entityName ? ` (${e.entityName})` : '')
          + (e.alreadyExists ? ' [exists]' : '')
          + (e.chainId ? ` chain=${e.chainId}` : ''));
      });
      if (preview.conflicts?.length) lines.push(`Already in pipeline: ${preview.conflicts.join(', ')}`);
      (preview.notes || []).forEach((n) => lines.push(n));
      previewOut.textContent = lines.join('\n');
      log?.('Config', `Entity plan preview: ${preview.entityCount} entities`);
    } catch (previewErr) {
      previewOut.textContent = '';
      setMessage?.(previewErr.message, 'error');
    }
  });

  return {
    currentPlan,
    isDryRun,
    reloadEntities,
    selectionError: () => selectionError(panel, loadedEntities, selectedIds),
    planPayload() {
      const err = selectionError(panel, loadedEntities, selectedIds);
      if (err) throw new Error(err);
      const plan = currentPlan();
      if (planIsEmpty(plan) && !isDryRun()) return {};
      return { plan: planIsEmpty(plan) ? {} : plan, dryRun: isDryRun() };
    },
  };
}
