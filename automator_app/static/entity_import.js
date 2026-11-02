// Entity Customize (single-pipeline plan subset): picker, include/exclude, parts, onExisting, dryRun.
// Reuses the same plan JSON as backup Customize minus agents/globals/resumeInto/name. No passwords.
// Per-entity target: inline schema.table combobox; new/existing badge from discovery match.

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

/** Infer new vs existing from a typed table name against discovered target tables. */
export function inferTargetMode(targetTable, existingTables) {
  const name = String(targetTable || '').trim();
  if (!name) return 'new';
  const lower = name.toLowerCase();
  const hit = (existingTables || []).some((t) => String(t).toLowerCase() === lower);
  return hit ? 'existing' : 'new';
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
  } else if (
    loadedEntities?.length
    && selectedIds
    && selectedIds.size < loadedEntities.length
  ) {
    entry.entities = {
      include: loadedEntities.map((e) => e.id).filter((id) => selectedIds.has(id)),
    };
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

/** Muted id under the name only when it differs from the Source column (e.g. @@2). */
export function entityIdSubtitle(entity) {
  const id = String(entity?.id || '');
  if (!id) return '';
  const source = String(entity?.source || '').trim();
  if (source?.toLowerCase() === id.toLowerCase()) return '';
  // @@n (or any other id that is not the same as source) is useful; matching source is noise.
  if (!source) return id.includes('@@') ? id : '';
  return id;
}

function sourceDisplay(entity) {
  return entity.source || `${entity.schema || ''}.${(entity.id || '').split('@@')[0].split('.').slice(1).join('.')}` || entity.id || '';
}

function chainDisplayName(members) {
  const named = members.find((m) => m.entityName)?.entityName;
  if (named) return named;
  const chainId = members[0]?.chainId || '';
  return String(chainId).replace(/^multitable_/i, '') || 'MultiTable chain';
}

/** Group visible entities: schema headers, then chain blocks or singles (order preserved). */
export function groupPickerRows(entities) {
  const groups = new Map();
  entities.forEach((e) => {
    const key = e.schema || '(no schema)';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  });
  const rows = [];
  groups.forEach((list, schema) => {
    rows.push({ kind: 'schema', schema });
    const seenChain = new Set();
    list.forEach((entity) => {
      if (!entity.chainId) {
        rows.push({ kind: 'entity', entity });
        return;
      }
      const cid = String(entity.chainId);
      if (seenChain.has(cid)) return;
      seenChain.add(cid);
      const members = list.filter((e) => String(e.chainId) === cid);
      rows.push({ kind: 'chain', chainId: cid, members });
    });
  });
  return rows;
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

function onExistingPolicyLabel(panel) {
  const raw = panel.querySelector('[data-on-existing]')?.value || '';
  if (raw === 'skip') return 'will be skipped';
  if (raw === 'update') return 'will be updated';
  if (raw === 'fail') return 'import will fail';
  return 'will keep current behaviour';
}

function badge(text, kind) {
  return el('span', { className: `entity-badge entity-badge--${kind}` }, text);
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
    'Create missing tables is global. '
    + 'A “table exists” badge means data is routed there (no create). '
    + 'A “new table” badge still follows the Create missing tables checkbox.');

  const onExisting = el('label', { className: 'entity-on-existing' },
    'If the entity is already in the pipeline ',
    el('select', { dataset: { onExisting: '1' } },
      el('option', { value: '' }, 'keep current behaviour'),
      el('option', { value: 'skip' }, 'skip (leave as-is)'),
      el('option', { value: 'update' }, 'update in place'),
      el('option', { value: 'fail' }, 'fail if it already exists')),
    el('span', { className: 'hint-text entity-on-existing-help', title: 'Advanced: omit from the plan to keep current behaviour (including GS_IMPORT_ON_EXISTING when set).' },
      'Default keeps current behaviour. Hover for advanced detail.'));

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

  const previewOut = el('div', { className: 'entity-plan-preview', id: 'entity-plan-preview-out' });
  const selectedCount = el('span', { className: 'entity-selected-count', id: 'entity-selected-count' }, '');
  const btnRow = el('div', { className: 'button-group entity-sticky-actions sticky-action-bar', id: 'entity-sticky-actions' },
    selectedCount,
    el('button', { type: 'button', className: 'secondary', id: 'entity-plan-draft-btn' }, 'Download plan'),
    el('button', { type: 'button', className: 'secondary', id: 'entity-plan-preview-btn' }, 'Preview'),
    el('button', { type: 'button', className: 'secondary', id: 'entity-plan-reload-btn' }, 'Reload entities'),
  );

  panel.append(
    el('h3', {}, 'Customize entity creation'),
    el('p', { className: 'hint-text' },
      'Optional plan for this run only. Empty choices keep current behaviour. '
      + 'Pick entities below after upload; Agents, globals, resumeInto and pipeline rename are backup Customize only. '
      + 'Per row, set the target schema and table (or collection). A name that matches an existing target shows the existing badge.'),
    partsRow, createTablesHint, onExisting, picker, advanced, dryRun, btnRow, previewOut,
  );

  function currentPlan() {
    const ctx = getContext() || {};
    return buildEntityPlan(panel, ctx.pipelineId, loadedEntities, selectedIds, targetOverrides);
  }

  function isDryRun() {
    return Boolean(panel.querySelector('#entity-plan-dry-run')?.checked);
  }

  function clearPreview() {
    previewOut.replaceChildren();
  }

  function updateCount() {
    const total = loadedEntities.length;
    const n = selectedIds ? selectedIds.size : 0;
    pickerCount.textContent = total ? `(${n} of ${total} selected)` : '';
    if (selectedCount) {
      selectedCount.textContent = total ? `${n} selected` : '';
      selectedCount.hidden = !total;
    }
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
    targetOverrides.set(entityId, { ...prev, ...patch });
    clearPreview();
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

  async function fetchColumnNames(path, qs, label) {
    // Discovery may be unavailable on Core Hub <2.3 or NoSQL — caller treats null as "unverified".
    try {
      const res = await fetch(`${path}?${qs}`);
      const body = await jsonOrThrow(res, label);
      return body.columns || [];
    } catch (err) {
      log?.('Config', `${label} discovery failed: ${err.message}`);
      return null;
    }
  }

  function updateMismatchUi(entityId, note) {
    const host = panel.querySelector(`[data-col-warn-host="${CSS.escape(entityId)}"]`);
    if (!host) return;
    host.replaceChildren();
    if (!note) return;
    const isUnverified = note.startsWith('Column check unverified');
    const tip = el('details', { className: 'entity-mismatch' },
      el('summary', {
        className: `entity-mismatch-summary${isUnverified ? ' entity-mismatch-summary--muted' : ''}`,
        title: note,
      }, isUnverified ? '⚠︎ unverified' : '⚠︎ mismatch'),
      el('p', { className: 'entity-mismatch-detail' }, note),
    );
    host.append(tip);
  }

  async function refreshColumnWarn(entity) {
    const resolved = resolveTarget(entity, targetOverrides);
    if (resolved.mode !== 'existing' || !resolved.targetTable) {
      columnWarnById.delete(entity.id);
      updateMismatchUi(entity.id, '');
      return;
    }
    const ctx = getContext() || {};
    if (!ctx.pipelineId) return;
    const src = sourceTableParts(entity);
    const sourceCols = await fetchColumnNames(
      '/api/bulk/columns',
      new URLSearchParams({
        pipelineId: ctx.pipelineId, schema: src.schema, table: src.table,
      }),
      'Source column',
    );
    const targetCols = await fetchColumnNames(
      '/api/bulk/target-columns',
      new URLSearchParams({
        pipelineId: ctx.pipelineId,
        schema: resolved.targetSchema,
        table: resolved.targetTable,
      }),
      'Target column',
    );
    const note = columnMismatchNote(sourceCols, targetCols);
    if (note) columnWarnById.set(entity.id, note);
    else columnWarnById.delete(entity.id);
    updateMismatchUi(entity.id, note || '');
  }

  function resolveBoxTargets(entityOrMembers) {
    if (Array.isArray(entityOrMembers)) return entityOrMembers;
    if (entityOrMembers.chainId) return chainMembers(loadedEntities, entityOrMembers.chainId);
    return [entityOrMembers];
  }

  function onBoxChange(entityOrMembers, checked) {
    if (!selectedIds) selectedIds = new Set();
    const targets = resolveBoxTargets(entityOrMembers);
    targets.forEach((member) => {
      if (checked) selectedIds.add(member.id);
      else selectedIds.delete(member.id);
    });
    clearPreview();
    renderPicker();
  }

  function fillDatalist(listEl, values) {
    listEl.innerHTML = '';
    (values || []).forEach((name) => listEl.append(el('option', { value: name })));
  }

  function buildTargetControls(entity) {
    const resolved = resolveTarget(entity, targetOverrides);
    const listId = `entity-target-tables-${String(entity.id).replace(/[^a-zA-Z0-9_-]/g, '_')}`;
    const wrap = el('div', { className: 'entity-target-cell' });
    const schemaInput = el('input', {
      type: 'text',
      className: 'entity-target-schema',
      value: resolved.targetSchema,
      list: 'entity-target-schema-list',
      placeholder: 'schema',
      dataset: { targetSchemaFor: entity.id },
      title: resolved.targetSchema || 'Target schema (or NoSQL database)',
      autocomplete: 'off',
    });
    const tableList = el('datalist', { id: listId });
    const tableInput = el('input', {
      type: 'text',
      className: 'entity-target-table',
      value: resolved.targetTable,
      list: listId,
      placeholder: 'table or collection',
      dataset: { targetTableFor: entity.id },
      title: resolved.targetTable || 'Target table or collection',
      autocomplete: 'off',
    });
    const modeBadge = el('span', { className: 'entity-target-mode-badge', dataset: { modeBadgeFor: entity.id } });
    const mismatchHost = el('span', { dataset: { colWarnHost: entity.id } });

    const syncBadge = (tables) => {
      const mode = inferTargetMode(tableInput.value, tables);
      modeBadge.replaceChildren(badge(
        mode === 'existing' ? 'table exists' : 'new table',
        mode === 'existing' ? 'existing' : 'new',
      ));
      schemaInput.title = schemaInput.value || 'Target schema (or NoSQL database)';
      tableInput.title = tableInput.value || 'Target table or collection';
      setOverride(entity.id, {
        mode,
        targetSchema: schemaInput.value || '',
        targetTable: tableInput.value || '',
      });
      refreshColumnWarn(entity).catch((err) => {
        log?.('Config', `Column mismatch check failed: ${err.message}`);
      });
    };

    const loadTables = (preferred) => {
      const ctx = getContext() || {};
      if (!ctx.pipelineId) {
        syncBadge([]);
        return;
      }
      ensureTargetTables(ctx.pipelineId, schemaInput.value || '')
        .then((tables) => {
          fillDatalist(tableList, tables);
          syncBadge(tables);
          if (preferred && !tableInput.value) tableInput.value = preferred;
        })
        .catch((err) => {
          log?.('Config', `Target table discovery failed: ${err.message}`);
          syncBadge([]);
        });
    };

    schemaInput.addEventListener('change', () => loadTables());
    schemaInput.addEventListener('input', () => {
      const tables = tablesBySchema.get(schemaInput.value || '') || [];
      syncBadge(tables);
    });
    tableInput.addEventListener('input', () => {
      const tables = tablesBySchema.get(schemaInput.value || '') || [];
      syncBadge(tables);
    });
    tableInput.addEventListener('change', () => {
      const tables = tablesBySchema.get(schemaInput.value || '') || [];
      syncBadge(tables);
    });

    loadTables(resolved.targetTable);

    wrap.append(
      el('div', { className: 'entity-target-row' },
        schemaInput,
        el('span', { className: 'entity-target-dot' }, '.'),
        tableInput,
        modeBadge,
        mismatchHost,
      ),
      tableList,
    );
    if (columnWarnById.has(entity.id)) {
      updateMismatchUi(entity.id, columnWarnById.get(entity.id));
    }
    return wrap;
  }

  function entityNameCell(entity, { indent = false } = {}) {
    const sub = entityIdSubtitle(entity);
    const name = entity.entityName || sourceDisplay(entity).split('.').pop() || entity.id || '—';
    return el('td', { className: indent ? 'entity-picker-indent' : '' },
      el('div', { className: 'entity-name-cell' },
        el('span', { className: 'entity-name' }, name),
        sub ? el('span', { className: 'entity-id-sub muted' }, sub) : null,
      ),
    );
  }

  function statusCell(entity) {
    if (!entity.alreadyExists) return el('td', {});
    const policy = onExistingPolicyLabel(panel);
    return el('td', {},
      badge('already in pipeline', 'warn'),
      el('span', { className: 'entity-status-policy muted' }, ` ${policy}`),
    );
  }

  function appendEntityRow(tbody, entity, { indent = false, showCheckbox = true } = {}) {
    const isChecked = !selectedIds || selectedIds.has(entity.id);
    const box = showCheckbox
      ? el('input', {
        type: 'checkbox',
        checked: isChecked,
        dataset: { entityId: entity.id, chainId: entity.chainId || '' },
      })
      : el('span', { className: 'entity-picker-check-spacer' }, '');
    if (showCheckbox) {
      box.addEventListener('change', () => onBoxChange(entity, box.checked));
    }
    tbody.append(el('tr', { className: indent ? 'entity-picker-child' : '' },
      el('td', {}, box),
      entityNameCell(entity, { indent }),
      el('td', { className: 'mono entity-source' }, sourceDisplay(entity)),
      el('td', {}, buildTargetControls(entity)),
      el('td', {}, entity.type || ''),
      statusCell(entity),
    ));
  }

  function appendChainBlock(tbody, members) {
    const allChecked = members.every((m) => !selectedIds || selectedIds.has(m.id));
    const someChecked = members.some((m) => selectedIds?.has(m.id));
    const box = el('input', {
      type: 'checkbox',
      checked: allChecked,
      dataset: { chainId: members[0].chainId, entityId: members[0].id },
    });
    box.indeterminate = someChecked && !allChecked;
    box.addEventListener('change', () => onBoxChange(members, box.checked));
    const title = chainDisplayName(members);
    tbody.append(el('tr', { className: 'entity-picker-chain' },
      el('td', {}, box),
      el('td', { colSpan: 5 },
        el('div', { className: 'entity-name-cell' },
          el('span', { className: 'entity-name' }, title),
          badge('MultiTable', 'chain'),
          el('span', { className: 'muted entity-chain-count' }, ` ${members.length} tables`),
        ),
      ),
    ));
    members.forEach((entity) => appendEntityRow(tbody, entity, { indent: true, showCheckbox: false }));
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

    let schemaList = panel.querySelector('#entity-target-schema-list');
    if (!schemaList) {
      schemaList = el('datalist', { id: 'entity-target-schema-list' });
      panel.append(schemaList);
    }
    fillDatalist(schemaList, targetSchemas);

    const table = el('table', { className: 'entity-picker-table custom-import-tables' });
    table.append(el('thead', {}, el('tr', {},
      el('th', { className: 'entity-picker-check-col' }, ''),
      el('th', {}, 'Entity'),
      el('th', {}, 'Source'),
      el('th', {}, 'Target'),
      el('th', {}, 'Type'),
      el('th', {}, 'Status'),
    )));
    const tbody = el('tbody');
    groupPickerRows(visible).forEach((row) => {
      if (row.kind === 'schema') {
        tbody.append(el('tr', { className: 'entity-picker-schema' },
          el('td', { colSpan: 6 }, row.schema)));
        return;
      }
      if (row.kind === 'chain') {
        appendChainBlock(tbody, row.members);
        return;
      }
      appendEntityRow(tbody, row.entity);
    });
    table.append(tbody);
    pickerBody.append(table);
    updateCount();
  }

  pickerToolbar.querySelector('[data-select-all]').addEventListener('click', () => {
    selectedIds = new Set(loadedEntities.map((e) => e.id));
    clearPreview();
    renderPicker();
  });
  pickerToolbar.querySelector('[data-select-none]').addEventListener('click', () => {
    selectedIds = new Set();
    clearPreview();
    renderPicker();
  });
  pickerFilter.addEventListener('input', () => renderPicker());
  onExisting.querySelector('select')?.addEventListener('change', () => {
    clearPreview();
    renderPicker();
  });

  async function reloadEntities() {
    const ctx = getContext() || {};
    if (!ctx.pipelineId || !ctx.yamlFileId) {
      loadedEntities = [];
      selectedIds = null;
      targetOverrides.clear();
      columnWarnById.clear();
      clearPreview();
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
      clearPreview();
      renderPicker();
      log?.('Config', `Loaded ${loadedEntities.length} entities for picker`);
    } catch (err) {
      loadedEntities = [];
      selectedIds = null;
      clearPreview();
      renderPicker();
      setMessage?.(err.message, 'error');
    }
  }

  btnRow.querySelector('#entity-plan-reload-btn').addEventListener('click', () => reloadEntities());

  const requireImportReady = () => {
    const ctx = getContext() || {};
    if (!ctx.pipelineId || !ctx.yamlFileId) {
      setMessage?.('Select a pipeline and upload a YAML first', 'error');
      return null;
    }
    const err = selectionError(panel, loadedEntities, selectedIds, targetOverrides);
    if (err) {
      setMessage?.(err, 'error');
      return null;
    }
    return ctx;
  };

  btnRow.querySelector('#entity-plan-draft-btn').addEventListener('click', async () => {
    const ctx = requireImportReady();
    if (!ctx) return;
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

  function actionForEntity(entity) {
    const resolved = resolveTarget(entity, targetOverrides);
    if (resolved.mode === 'existing') return 'use existing (no create)';
    const createOn = panel.querySelector('[data-part="createTables"]')?.checked !== false;
    return createOn ? 'create if missing' : 'map only (create off)';
  }

  function renderPreviewTable(preview) {
    previewOut.replaceChildren();
    const summary = el('p', { className: 'hint-text' },
      `Pipeline ${preview.pipelineId}: ${preview.entityCount} entities · `
      + `onExistingEntity: ${preview.onExistingEntity || 'default'}`);
    const table = el('table', { className: 'entity-preview-table custom-import-tables' });
    table.append(el('thead', {}, el('tr', {},
      el('th', {}, 'Entity'),
      el('th', {}, 'Source'),
      el('th', {}, 'Target'),
      el('th', {}, 'Target table'),
      el('th', {}, 'Action'),
    )));
    const tbody = el('tbody');
    (preview.entities || []).forEach((e) => {
      const local = loadedEntities.find((x) => x.id === e.id) || e;
      const resolved = resolveTarget(local, targetOverrides);
      const mode = resolved.mode === 'existing' ? 'existing' : 'new';
      tbody.append(el('tr', {},
        el('td', {}, e.entityName || e.id),
        el('td', { className: 'mono' }, e.source || ''),
        el('td', { className: 'mono' }, `${e.targetSchema}.${e.targetTable}`),
        el('td', {}, badge(mode === 'existing' ? 'table exists' : 'new table', mode === 'existing' ? 'existing' : 'new')),
        el('td', {}, actionForEntity(local)),
      ));
    });
    table.append(tbody);
    const partsDetails = el('details', { className: 'entity-preview-parts' },
      el('summary', {}, 'Parts / plan JSON'),
      el('pre', { className: 'entity-preview-parts-pre' },
        `parts: ${JSON.stringify(preview.parts)}\n`
        + (preview.conflicts?.length ? `Already in pipeline: ${preview.conflicts.join(', ')}\n` : '')
        + ((preview.notes || []).join('\n') || '')),
    );
    const colNotes = [...columnWarnById.entries()];
    if (colNotes.length) {
      const colBody = colNotes.map(([id, note]) => `  ${id}: ${note}`).join('\n');
      partsDetails.append(el('pre', { className: 'entity-preview-parts-pre' },
        `Column checks:\n${colBody}`));
    }
    previewOut.append(summary, table, partsDetails);
  }

  btnRow.querySelector('#entity-plan-preview-btn').addEventListener('click', async () => {
    const ctx = requireImportReady();
    if (!ctx) return;
    const plan = currentPlan();
    try {
      const res = await fetch('/api/entities/plan/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pipelineId: ctx.pipelineId, yamlFileId: ctx.yamlFileId, plan }),
      });
      const preview = await jsonOrThrow(res, 'Preview failed');
      renderPreviewTable(preview);
      log?.('Config', `Entity plan preview: ${preview.entityCount} entities`);
    } catch (previewErr) {
      clearPreview();
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
