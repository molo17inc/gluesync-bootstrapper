// GS1 (Gluesync 1.x) configuration import: convert and check with Core Hub, then save the chosen drafts once the
// operator confirms. Nothing is written to Core Hub before the confirmation.

const SEVERITY_CLASS = { ERROR: 'gs1-issue-error', WARNING: 'gs1-issue-warning' };

/** One line of an issue: severity, code, where, message. */
export function issueText(issue) {
  const where = [issue.level, issue.location].filter(Boolean).join('/');
  return `${issue.severity} ${issue.code}${where ? ` [${where}]` : ''}: ${issue.message}`;
}

/** The rows of the report table: one per GS1 entity, a draft only for a converted one. */
export function reportRows(plan) {
  const drafts = new Set(plan.drafts || []);
  return (plan.perEntity || []).map((entry) => {
    const check = (plan.checks || {})[entry.gs1Name] || {};
    const validate = check.validate;
    return {
      name: entry.gs1Name,
      bucket: entry.bucket,
      hasDraft: drafts.has(entry.gs1Name),
      issues: entry.issues || [],
      checkError: check.error || null,
      verdict: check.describe?.verdict || null,
      proposedKeys: check.describe?.proposedKeys || {},
      valid: validate ? validate.valid : null,
      validationIssues: validate?.issues || [],
    };
  });
}

/** The drafts the operator can save by default: converted and not refused by Core Hub's validation. */
export function defaultSelection(rows) {
  return rows.filter((row) => row.hasDraft && row.valid !== false).map((row) => row.name);
}

export function confirmationText(names, pipelineLabel) {
  return `Save ${names.length} entities (${names.join(', ')}) to pipeline ${pipelineLabel}? Core Hub stores them now.`;
}

function cell(text, className) {
  const td = document.createElement('td');
  if (className) td.className = className;
  td.textContent = text;
  return td;
}

function issueList(issues) {
  const list = document.createElement('ul');
  list.className = 'gs1-issues';
  issues.forEach((issue) => {
    const item = document.createElement('li');
    item.className = SEVERITY_CLASS[issue.severity] || '';
    item.textContent = issueText(issue);
    if (issue.hint) item.title = issue.hint;
    list.appendChild(item);
  });
  return list;
}

function checksCell(row) {
  const td = document.createElement('td');
  const lines = [];
  if (row.checkError) lines.push(`Core Hub could not check it: ${row.checkError}`);
  if (row.verdict) {
    const keys = Object.entries(row.proposedKeys).map(([level, key]) => `${level} [${(key || []).join(', ')}]`);
    lines.push(`describe: ${row.verdict}${keys.length ? `; proposed keys ${keys.join('; ')}` : ''}`);
  }
  if (row.valid !== null) lines.push(`validate: ${row.valid ? 'valid' : 'refused'}`);
  lines.forEach((line) => {
    const div = document.createElement('div');
    div.textContent = line;
    td.appendChild(div);
  });
  if (row.validationIssues.length) td.appendChild(issueList(row.validationIssues));
  return td;
}

function renderReport(container, plan, selected) {
  container.replaceChildren();
  const rows = reportRows(plan);
  if (!plan.structureChecked) {
    const note = document.createElement('p');
    note.className = 'hint-text';
    note.textContent = 'Core Hub did not check the drafts: structure errors show up only when you save them.';
    container.appendChild(note);
  }
  const table = document.createElement('table');
  table.className = 'gs1-report';
  const head = document.createElement('tr');
  ['Save', 'GS1 entity', 'Bucket', 'Converter report', 'Core Hub checks'].forEach((title) => {
    const th = document.createElement('th');
    th.scope = 'col';
    th.textContent = title;
    head.appendChild(th);
  });
  const thead = document.createElement('thead');
  thead.appendChild(head);
  table.appendChild(thead);
  const body = document.createElement('tbody');
  rows.forEach((row) => {
    const tr = document.createElement('tr');
    const pick = document.createElement('td');
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.disabled = !row.hasDraft;
    box.checked = selected.has(row.name);
    box.setAttribute('aria-label', `Save ${row.name}`);
    box.addEventListener('change', () => {
      if (box.checked) selected.add(row.name);
      else selected.delete(row.name);
      container.dispatchEvent(new CustomEvent('gs1-selection'));
    });
    pick.appendChild(box);
    tr.appendChild(pick);
    tr.appendChild(cell(row.name));
    tr.appendChild(cell(row.bucket, row.bucket === 'UNSUPPORTED' ? 'gs1-unsupported' : ''));
    const issues = document.createElement('td');
    if (row.issues.length) issues.appendChild(issueList(row.issues));
    tr.appendChild(issues);
    tr.appendChild(checksCell(row));
    body.appendChild(tr);
  });
  table.appendChild(body);
  container.appendChild(table);
  const stripped = document.createElement('p');
  stripped.className = 'hint-text';
  const keys = plan.strippedKeys || [];
  stripped.textContent = `Secret keys removed unread: ${keys.length ? keys.join(', ') : 'none'}`;
  container.appendChild(stripped);
}

async function postConvert(form) {
  const res = await fetch('/api/gs1/convert', { method: 'POST', body: form });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Conversion failed: ${res.status}`);
  return body;
}

async function postSave(planId, entities) {
  const res = await fetch('/api/gs1/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ planId, entities, confirmed: true }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Save failed: ${res.status}`);
  return body;
}

export function initGs1Import({ setMessage, log, confirm = (text) => window.confirm(text) }) {
  const file = document.getElementById('gs1-config-file');
  const pipeline = document.getElementById('gs1-pipeline-id');
  const repository = document.getElementById('gs1-repository');
  const sourceAgent = document.getElementById('gs1-source-agent');
  const targetAgent = document.getElementById('gs1-target-agent');
  const rootCounts = document.getElementById('gs1-root-counts');
  const offline = document.getElementById('gs1-offline');
  const convertButton = document.getElementById('gs1-convert-btn');
  const saveButton = document.getElementById('gs1-save-btn');
  const report = document.getElementById('gs1-report');
  if (!file || !convertButton) return;

  let plan = null;
  const selected = new Set();
  const refresh = () => {
    convertButton.disabled = !(file.files?.length && pipeline.value);
    saveButton.disabled = !plan || selected.size === 0;
  };
  file.addEventListener('change', () => { plan = null; selected.clear(); report.replaceChildren(); refresh(); });
  pipeline.addEventListener('change', () => { plan = null; selected.clear(); report.replaceChildren(); refresh(); });
  report.addEventListener('gs1-selection', refresh);

  convertButton.addEventListener('click', async () => {
    const form = new FormData();
    form.append('file', file.files[0]);
    form.append('pipelineId', pipeline.value);
    form.append('repository', repository.value);
    if (sourceAgent.value.trim()) form.append('sourceAgentId', sourceAgent.value.trim());
    if (targetAgent.value.trim()) form.append('targetAgentId', targetAgent.value.trim());
    if (rootCounts.value.trim()) form.append('rootCounts', rootCounts.value.trim());
    form.append('check', offline.checked ? 'false' : 'true');
    convertButton.disabled = true;
    saveButton.disabled = true;
    setMessage(offline.checked ? 'Converting…' : 'Converting and checking the drafts with Core Hub…');
    try {
      plan = await postConvert(form);
      selected.clear();
      defaultSelection(reportRows(plan)).forEach((name) => selected.add(name));
      renderReport(report, plan, selected);
      setMessage(`${plan.drafts.length} drafts out of ${plan.perEntity.length} GS1 entities. Nothing is saved yet.`, 'success');
      log(`Converted ${plan.perEntity.length} GS1 entities: ${plan.drafts.length} drafts`);
    } catch (err) {
      plan = null;
      setMessage(err.message, 'error');
      log(`GS1 conversion failed: ${err.message}`);
    } finally {
      refresh();
    }
  });

  saveButton.addEventListener('click', async () => {
    if (!plan) return;
    const names = (plan.drafts || []).filter((name) => selected.has(name));
    const label = pipeline.options[pipeline.selectedIndex]?.textContent || plan.pipelineId;
    if (!names.length || !confirm(confirmationText(names, label))) {
      setMessage('Nothing saved.');
      return;
    }
    saveButton.disabled = true;
    setMessage('Saving the chosen drafts…');
    try {
      const result = await postSave(plan.planId, names);
      setMessage(result.message, 'success');
      log(result.message);
    } catch (err) {
      setMessage(err.message, 'error');
      log(`GS1 save failed: ${err.message}`);
    } finally {
      refresh();
    }
  });
  refresh();
}
