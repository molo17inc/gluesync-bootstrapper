// Entity Customize (single-pipeline plan subset): picker, include/exclude, parts, onExisting, dryRun.
// Reuses the same plan JSON as backup Customize minus agents/globals/resumeInto/name. No passwords.
// Per-entity target schema/table: New (editable) or Existing (discovery dropdown).

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

/** Resolve effective target for an entity from overrides Map/object or YAML defaults. */
export function resolveTarget(entity, overrides) {
  const ov = overrides instanceof Map ? overrides.get(entity.id) : overrides?.[entity.id];
  const targetSchema = String(ov?.targetSchema ?? entity.targetSchema ?? '').trim();
  const targetTable = String(ov?.targetTable ?? entity.targetTable ?? '').trim();
  const mode = ov?.mode === 'existing' ? 'existing' : 'new';
  return { targetSchema, targetTable, mode };
}

/** True when override differs from the YAML-default target. */
export function targetDiffersFromDefault(entity, resolved) {
  const defSchema = String(entity.targetSchema || '').trim();
  const defTable = String(entity.targetTable || '').trim();
  return resolved.targetSchema !== defSchema || resolved.targetTable !== defTable;
}

/**
 * Build the plan from the panel.
 * @param selectedIds Set|null  null = picker not loaded (omit include); Set of exact ids when loaded.
 * @param targetOverrides Map|object|null  per-entity {targetSchema,targetTable,mode}
 */
export function buildEntityPlan(panel, pipelineId, loadedEntities, selectedIds, targetOverrides) {
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
  } else if (loadedEntities?.length && selectedIds) {
    if (selectedIds.size < loadedEntities.length) {
      entry.entities = { include: loadedEntities.map((e) => e.id).filter((id) => selectedIds.has(id)) };
    }
  }

  const overrideMap = {};
  (loadedEntities || []).forEach((entity) => {
    if (selectedIds && !selectedIds.has(entity.id)) return;
    const resolved = resolveTarget(entity, targetOverrides);
    if (!targetDiffersFromDefault(entity, resolved)) return;
    const piece = {};
    if (resolved.targetSchema) piece.targetSchema = resolved.targetSchema;
    if (resolved.targetTable) piece.targetTable = resolved.targetTable;
    if (Object.keys(piece).length) overrideMap[entity.id] = piece;
  });
  if (Object.keys(overrideMap).length) {
    entry.entities = entry.entities || {};
    entry.entities.overrides = overrideMap;
  }

  if (Object.keys(entry).length) plan.pipelines[pipelineId || 'entity'] = entry;
  return plan;
}

export function planIsEmpty(plan) {
  return !plan || (!Object.keys(plan.defaults || {}).length && !Object.keys(plan.pipelines || {}).length);
}

/** Validate picker selection and per-entity target names (empty / duplicate new targets). */
export function selectionError(panel, loadedEntities, selectedIds, targetOverrides) {
  const advanced = advancedIncludeExclude(panel);
  if (advanced.used) return null;
  if (!loadedEntities?.length) return null;
  if (!selectedIds?.size) {
    return 'Select at least one entity (or use Advanced include), or Select all to import every table';
  }
  const seen = new Map();
  for (const entity of loadedEntities) {
    if (!selectedIds.has(entity.id)) continue;
    const resolved = resolveTarget(entity, targetOverrides);
    if (!resolved.targetTable) {
      return `Entity ${entity.id}: target table (or collection) name is required`;
    }
    const key = `${resolved.targetSchema}.${resolved.targetTable}`.toLowerCase();
    if (seen.has(key)) {
      return `Duplicate target ${resolved.targetSchema || '(no schema)'}.${resolved.targetTable}`
        + ` for ${seen.get(key)} and ${entity.id}`;
    }
    seen.set(key, entity.id);
  }
  return null;
}

function chainMembers(loaded, chainId) {
  if (!chainId) return [];
  return loaded.filter((e) => e.chainId && String(e.chainId) === String(chainId));
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

function sourceTableParts(entity) {
  const source = String(entity.source || '');
  const dot = source.indexOf('.');
  if (dot < 0) return { schema: entity.schema || '', table: source || entity.id || '' };
  return { schema: source.slice(0, dot), table: source.slice(dot + 1) };
}

export function columnMismatchNote(sourceCols, targetCols) {
  if (!sourceCols || !targetCols) return 'Column check unverified (discovery unavailable).';
  if (!sourceCols.length && !targetCols.length) {
    return 'Column check unverified (no columns returned).';
  }
  const src = new Set(sourceCols.map((c) => c.toLowerCase()));
  const tgt = new Set(targetCols.map((c) => c.toLowerCase()));
  const missingOnTarget = [...src].filter((c) => !tgt.has(c));
  const extraOnTarget = [...tgt].filter((c) => !src.has(c));
  if (!missingOnTarget.length && !extraOnTarget.length) return '';
  const bits = [];
  if (missingOnTarget.length) bits.push(`missing on target: ${missingOnTarget.slice(0, 8).join(', ')}`);
  if (extraOnTarget.length) bits.push(`extra on target: ${extraOnTarget.slice(0, 8).join(', ')}`);
  return `Column mismatch — ${bits.join('; ')}`;
}

export function initEntityImport({ root, getContext, setMessage, log }) {
  if (!root) return { currentPlan() { return null; }, isDryRun() { return false; } };

  let loadedEntities = [];
  let selectedIds = null; // null until first successful load; then a Set of exact ids
  const targetOverrides = new Map(); // id -> {targetSchema, targetTable, mode}
  let targetSchemas = []; // discovery cache
  const tablesBySchema = new Map(); // schema -> string[]
  const columnWarnById = new Map(); // id -> warn string
  let discoveryOk = true;

  const panel = el('div', { className: 'entity-import', id: 'entity-customize-panel' });
  root.append(panel);

  const partsRow = el('div', { className: 'entity-parts' },
    el('span', { className: 'hint-text' }, 'Parts to apply:'),
    ...PARTS.map(([key, label]) => el('label', { className: 'checkbox' },
      el('input', { type: 'checkbox', dataset: { part: key }, checked: true }),
      ` ${label}`)));

  const createTablesHint = el('p', { className: 'hint-text entity-create-hint' },
    'Create missing tables is global (CREATE IF NOT EXISTS). '
    + 'Picking an existing target for an entity routes data there; that entity does not need a new table. '
    + 'New names still respect the global checkbox.');

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
      + 'Pick entities below after upload; Agents, globals, resumeInto and pipeline rename are backup Customize only. '
      + 'Per row you can rename the target (New) or pick an existing target table/collection (Existing).'),
    partsRow, createTablesHint, onExisting, picker, advanced, dryRun, btnRow, previewOut,
  );

  function currentPlan() {
    const ctx = getContext() || {};
    return buildEntityPlan(panel, ctx.pipelineId, loadedEntities, selectedIds, targetOverrides);
  }

  function isDryRun() {
    return Boolean(panel.querySelector('#entity-plan-dry-run')?.checked);
  }

  function updateCount() {
    const total = loadedEntities.length;
    const n = selectedIds ? selectedIds.size : 0;
    pickerCount.textContent = total ? `${n} of ${total} selected` : '';
    const partial = selectedIds ? partialChains(loadedEntities, selectedIds) : [];
    const bits = [];
    if (partial.length) {
      bits.push(`Warning: MultiTable chain(s) partly selected: ${partial.join(', ')}. `
        + 'Chains should be selected or deselected together.');
    }
    if (!discoveryOk) {
      bits.push('Target discovery unavailable — enter schema/table (or collection) as free text.');
    }
    pickerWarn.textContent = bits.join(' ');
  }

  function setOverride(entityId, patch) {
    const prev = targetOverrides.get(entityId) || {};
    const next = { ...prev, ...patch };
    targetOverrides.set(entityId, next);
  }

  async function ensureTargetSchemas(pipelineId) {
    if (targetSchemas.length) return targetSchemas;
    try {
      const res = await fetch(`/api/bulk/target-schemas?pipelineId=${encodeURIComponent(pipelineId)}`);
      const body = await jsonOrThrow(res, 'Could not list target schemas');
      targetSchemas = body.schemas || [];
      discoveryOk = true;
    } catch (err) {
      discoveryOk = false;
      targetSchemas = [];
      log?.('Config', `Target schema discovery failed: ${err.message}`);
    }
    return targetSchemas;
  }

  async function ensureTargetTables(pipelineId, schema = '') {
    const key = schema || '';
    if (tablesBySchema.has(key)) return tablesBySchema.get(key);
    try {
      const qs = new URLSearchParams({ pipelineId, schema: key });
      const res = await fetch(`/api/bulk/target-tables?${qs}`);
      const body = await jsonOrThrow(res, 'Could not list target tables');
      const tables = body.tables || [];
      tablesBySchema.set(key, tables);
      return tables;
    } catch (err) {
      log?.('Config', `Target table discovery failed for ${key}: ${err.message}`);
      tablesBySchema.set(key, []);
      return [];
    }
  }

  async function refreshColumnWarn(entity) {
    const resolved = resolveTarget(entity, targetOverrides);
    if (resolved.mode !== 'existing' || !resolved.targetTable) {
      columnWarnById.delete(entity.id);
      return;
    }
    const ctx = getContext() || {};
    if (!ctx.pipelineId) return;
    const src = sourceTableParts(entity);
    let sourceCols = null;
    let targetCols = null;
    try {
      const qs = new URLSearchParams({
        pipelineId: ctx.pipelineId, schema: src.schema, table: src.table,
      });
      const res = await fetch(`/api/bulk/columns?${qs}`);
      const body = await jsonOrThrow(res, 'source columns');
      sourceCols = body.columns || [];
    } catch (err) {
      // Discovery may be unavailable on Core Hub <2.3 or NoSQL — warn stays "unverified".
      log?.('Config', `Source column discovery failed: ${err.message}`);
      sourceCols = null;
    }
    try {
      const qs = new URLSearchParams({
        pipelineId: ctx.pipelineId,
        schema: resolved.targetSchema,
        table: resolved.targetTable,
      });
      const res = await fetch(`/api/bulk/target-columns?${qs}`);
      const body = await jsonOrThrow(res, 'target columns');
      targetCols = body.columns || [];
    } catch (err) {
      // Same as source: missing columns endpoint → unverified, not a hard failure.
      log?.('Config', `Target column discovery failed: ${err.message}`);
      targetCols = null;
    }
    const note = columnMismatchNote(sourceCols, targetCols);
    if (note) columnWarnById.set(entity.id, note);
    else columnWarnById.delete(entity.id);
    const warnEl = panel.querySelector(`[data-col-warn="${CSS.escape(entity.id)}"]`);
    if (warnEl) warnEl.textContent = note || '';
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

  function buildTargetControls(entity) {
    const resolved = resolveTarget(entity, targetOverrides);
    const wrap = el('div', { className: 'entity-target-cell' });
    const schemaInput = el('input', {
      type: 'text',
      className: 'entity-target-schema',
      value: resolved.targetSchema,
      list: 'entity-target-schema-list',
      placeholder: 'schema',
      dataset: { targetSchemaFor: entity.id },
      title: 'Target schema (or NoSQL database)',
    });
    const modeSelect = el('select', {
      className: 'entity-target-mode',
      dataset: { targetModeFor: entity.id },
      title: 'New table name, or pick an existing target table/collection',
    },
      el('option', { value: 'new', selected: resolved.mode === 'new' }, 'New'),
      el('option', { value: 'existing', selected: resolved.mode === 'existing' }, 'Existing'),
    );
    const tableInput = el('input', {
      type: 'text',
      className: 'entity-target-table',
      value: resolved.targetTable,
      placeholder: 'table / collection',
      dataset: { targetTableFor: entity.id },
      title: 'Target table or collection name',
    });
    const tableSelect = el('select', {
      className: 'entity-target-table-select',
      dataset: { targetTableSelectFor: entity.id },
      title: 'Existing target tables/collections from discovery',
    });
    const warn = el('div', {
      className: 'entity-target-col-warn',
      dataset: { colWarn: entity.id },
    }, columnWarnById.get(entity.id) || '');

    const showMode = (tablesForSchema) => {
      const mode = modeSelect.value === 'existing' ? 'existing' : 'new';
      const tables = tablesForSchema !== undefined
        ? tablesForSchema
        : (tablesBySchema.get(schemaInput.value || '') || []);
      // Free-text when discovery failed OR returned no collections/tables.
      const useSelect = mode === 'existing' && discoveryOk && tables.length > 0;
      tableInput.hidden = useSelect;
      tableSelect.hidden = !useSelect;
    };
    showMode();

    const persist = () => {
      const mode = modeSelect.value === 'existing' ? 'existing' : 'new';
      const tableVal = mode === 'existing' && !tableSelect.hidden
        ? (tableSelect.value || '')
        : (tableInput.value || '');
      setOverride(entity.id, {
        mode,
        targetSchema: schemaInput.value || '',
        targetTable: tableVal,
      });
      void refreshColumnWarn(entity).catch((err) => {
        log?.('Config', `Column mismatch check failed: ${err.message}`);
      });
    };

    schemaInput.addEventListener('change', () => {
      persist();
      const ctx = getContext() || {};
      if (modeSelect.value === 'existing' && ctx.pipelineId) {
        ensureTargetTables(ctx.pipelineId, schemaInput.value || '')
          .then((tables) => {
            fillTableSelect(tableSelect, tables, resolveTarget(entity, targetOverrides).targetTable);
            showMode(tables);
            persist();
          })
          .catch((err) => {
            log?.('Config', `Target table discovery failed: ${err.message}`);
            showMode([]);
          });
      }
    });
    schemaInput.addEventListener('input', persist);
    modeSelect.addEventListener('change', () => {
      showMode();
      persist();
      const ctx = getContext() || {};
      if (modeSelect.value === 'existing' && ctx.pipelineId) {
        ensureTargetTables(ctx.pipelineId, schemaInput.value || '')
          .then((tables) => {
            fillTableSelect(tableSelect, tables, resolveTarget(entity, targetOverrides).targetTable);
            showMode(tables);
            persist();
          })
          .catch((err) => {
            log?.('Config', `Target table discovery failed: ${err.message}`);
            showMode([]);
          });
      }
    });
    tableInput.addEventListener('input', persist);
    tableSelect.addEventListener('change', persist);

    if (resolved.mode === 'existing') {
      const ctx = getContext() || {};
      if (ctx.pipelineId) {
        ensureTargetTables(ctx.pipelineId, resolved.targetSchema)
          .then((tables) => {
            fillTableSelect(tableSelect, tables, resolved.targetTable);
            showMode(tables);
          })
          .catch((err) => {
            log?.('Config', `Target table discovery failed: ${err.message}`);
            showMode([]);
          });
      }
    }

    wrap.append(
      el('div', { className: 'entity-target-row' },
        el('span', { className: 'entity-target-arrow' }, `${entity.source || ''} → `),
        schemaInput,
        el('span', {}, '.'),
        modeSelect,
        tableInput,
        tableSelect,
      ),
      warn,
    );
    return wrap;
  }

  function fillTableSelect(select, tables, selected) {
    select.innerHTML = '';
    select.append(el('option', { value: '' }, tables.length ? '— pick table —' : '— none found —'));
    tables.forEach((name) => {
      select.append(el('option', {
        value: name,
        selected: name === selected,
      }, name));
    });
    if (selected && tables.includes(selected)) select.value = selected;
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

    // Shared datalist for target schemas
    let schemaList = panel.querySelector('#entity-target-schema-list');
    if (!schemaList) {
      schemaList = el('datalist', { id: 'entity-target-schema-list' });
      panel.append(schemaList);
    }
    schemaList.innerHTML = '';
    targetSchemas.forEach((s) => schemaList.append(el('option', { value: s })));

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
          el('td', {}, buildTargetControls(entity)),
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
      targetOverrides.clear();
      columnWarnById.clear();
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
      targetOverrides.clear();
      columnWarnById.clear();
      tablesBySchema.clear();
      targetSchemas = [];
      await ensureTargetSchemas(ctx.pipelineId);
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
    const err = selectionError(panel, loadedEntities, selectedIds, targetOverrides);
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
    const err = selectionError(panel, loadedEntities, selectedIds, targetOverrides);
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
      const colNotes = [...columnWarnById.entries()].map(([id, note]) => `  ${id}: ${note}`);
      if (colNotes.length) {
        lines.push('Column checks:');
        colNotes.forEach((n) => lines.push(n));
      }
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
    selectionError: () => selectionError(panel, loadedEntities, selectedIds, targetOverrides),
    planPayload() {
      const err = selectionError(panel, loadedEntities, selectedIds, targetOverrides);
      if (err) throw new Error(err);
      const plan = currentPlan();
      if (planIsEmpty(plan) && !isDryRun()) return {};
      return { plan: planIsEmpty(plan) ? {} : plan, dryRun: isDryRun() };
    },
  };
}
