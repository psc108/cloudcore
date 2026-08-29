// ── Build Manager ────────────────────────────────────────────────────────────

let _bmTemplates = [];
let _bmActiveBuildId = null;
let _bmLogEs = null;

async function loadBuildManager() {
  try {
    await _bmLoadTemplates();
    await _bmLoadHistory();
  } catch (e) {
    document.getElementById('bm-template-grid').innerHTML = `<div class="bm-empty">Error: ${e.message}</div>`;
  }
}

// ── Templates ────────────────────────────────────────────────────────────────

async function _bmLoadTemplates() {
  const data = await api('GET', '/v1/builds/templates');
  _bmTemplates = data.items || [];
  const grid = document.getElementById('bm-template-grid');
  if (!_bmTemplates.length) {
    grid.innerHTML = '<div class="bm-empty">No templates found.</div>';
    return;
  }
  grid.innerHTML = _bmTemplates.map(t => `
    <div class="bm-template-card" onclick="bmSelectTemplate('${t.filename}')">
      <div class="bm-tpl-title">${t.title}</div>
      <div class="bm-tpl-desc">${t.description}</div>
      <div class="bm-tpl-tags">${(t.resources || []).map(r =>
        `<span class="bm-tag">${r.replace('_', ' ')}</span>`).join('')}</div>
    </div>
  `).join('');
}

async function bmSelectTemplate(filename) {
  // Highlight selected card
  document.querySelectorAll('.bm-template-card').forEach(c => c.classList.remove('selected'));
  const cards = document.querySelectorAll('.bm-template-card');
  const tpl = _bmTemplates.find(t => t.filename === filename);
  if (tpl) {
    const idx = _bmTemplates.indexOf(tpl);
    if (cards[idx]) cards[idx].classList.add('selected');
  }

  // Load vars schema
  const data = await api('GET', `/v1/builds/templates/${filename}/vars`);
  _bmRenderVarForm(filename, tpl, data.vars || {});
  document.getElementById('bm-var-panel').style.display = 'block';
  document.getElementById('bm-var-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function _bmRenderVarForm(filename, tpl, schema) {
  document.getElementById('bm-form-title').textContent = tpl ? tpl.title : filename;
  document.getElementById('bm-submit-filename').value = filename;

  const container = document.getElementById('bm-var-fields');
  const editable = Object.entries(schema).filter(([, v]) => !v.derived);

  if (!editable.length) {
    container.innerHTML = '<div class="bm-empty" style="padding:12px 0">No variables to configure — uses defaults.</div>';
    return;
  }

  container.innerHTML = editable.map(([key, meta]) => `
    <div class="field">
      <label>${key.replace(/_/g, ' ')}</label>
      <input type="${key.includes('token') ? 'password' : 'text'}"
             id="bm-var-${key}"
             data-key="${key}"
             value="${_esc(String(meta.default ?? ''))}">
    </div>
  `).join('');
}

async function bmSubmitBuild() {
  const filename = document.getElementById('bm-submit-filename').value;
  if (!filename) { toast('Select a template first', 'error'); return; }

  const vars = {};
  document.querySelectorAll('#bm-var-fields input[data-key]').forEach(el => {
    if (el.value.trim()) vars[el.dataset.key] = el.value.trim();
  });

  const btn = document.getElementById('bm-submit-btn');
  btn.disabled = true;
  btn.textContent = 'Submitting…';

  let data;
  try {
    data = await api('POST', '/v1/builds', { template: filename, vars });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Run Build';
    toast(e.message || 'Submit failed', 'error');
    return;
  }
  btn.disabled = false;
  btn.textContent = 'Run Build';
  toast(`Build started: ${data.id.slice(0, 8)}…`, 'success');
  document.getElementById('bm-var-panel').style.display = 'none';
  document.querySelectorAll('.bm-template-card').forEach(c => c.classList.remove('selected'));
  _bmOpenLog(data.id);
  await _bmLoadHistory();
}

// ── Log viewer ───────────────────────────────────────────────────────────────

function _bmOpenLog(buildId) {
  _bmActiveBuildId = buildId;
  if (_bmLogEs) { _bmLogEs.close(); _bmLogEs = null; }

  const panel = document.getElementById('bm-log-panel');
  const pre = document.getElementById('bm-log-pre');
  const title = document.getElementById('bm-log-title');
  panel.style.display = 'block';
  pre.textContent = '';
  title.textContent = `Build log — ${buildId.slice(0, 8)}…`;
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  _bmLogEs = new EventSource(`/v1/builds/${buildId}/log?token=${API_TOKEN}`);
  _bmLogEs.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.__done__) {
      _bmLogEs.close();
      _bmLogEs = null;
      _bmUpdateLogStatus(buildId, msg.status);
      _bmLoadHistory();
      // Refresh dashboard data so it reflects newly provisioned resources
      loadDashboard();
      return;
    }
    pre.textContent += msg + '\n';
    pre.scrollTop = pre.scrollHeight;
  };
  _bmLogEs.onerror = () => {
    if (_bmLogEs) { _bmLogEs.close(); _bmLogEs = null; }
  };
}

function _bmUpdateLogStatus(buildId, status) {
  const badge = document.getElementById(`bm-hist-status-${buildId}`);
  if (badge) {
    badge.textContent = status;
    badge.className = `badge badge-${status === 'success' ? 'running' : status === 'failed' ? 'error' : 'pending'}`;
  }
}

async function bmViewLog(buildId) {
  _bmActiveBuildId = buildId;
  if (_bmLogEs) { _bmLogEs.close(); _bmLogEs = null; }

  const data = await api('GET', `/v1/builds/${buildId}`);
  const panel = document.getElementById('bm-log-panel');
  const pre = document.getElementById('bm-log-pre');
  document.getElementById('bm-log-title').textContent = `Build log — ${buildId.slice(0, 8)}… (${data.template})`;
  pre.textContent = (data.log || []).join('\n');
  panel.style.display = 'block';
  pre.scrollTop = pre.scrollHeight;
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  // If still running, attach SSE
  if (data.status === 'running' || data.status === 'pending') {
    const offset = (data.log || []).length;
    _bmAttachSseFromOffset(buildId, offset, pre);
  }
}

function _bmAttachSseFromOffset(buildId, offset, pre) {
  let sent = offset;
  _bmLogEs = new EventSource(`/v1/builds/${buildId}/log?token=${API_TOKEN}`);
  _bmLogEs.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.__done__) {
      _bmLogEs.close(); _bmLogEs = null;
      _bmUpdateLogStatus(buildId, msg.status);
      _bmLoadHistory();
      loadDashboard();
      return;
    }
    if (sent > 0) { sent--; return; } // skip already-shown lines
    pre.textContent += msg + '\n';
    pre.scrollTop = pre.scrollHeight;
  };
}

// ── History ──────────────────────────────────────────────────────────────────

async function _bmLoadHistory() {
  const data = await api('GET', '/v1/builds');
  const tbody = document.getElementById('bm-history-tbody');
  const builds = data.items || [];
  document.getElementById('bm-sel-all').checked = false;
  document.getElementById('bm-destroy-btn').style.display = 'none';
  if (!builds.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="8">No builds yet.</td></tr>';
    return;
  }
  tbody.innerHTML = builds.map(b => {
    const canDestroy = b.status === 'success' && b.provisioned_count > 0;
    const destroyed  = b.status === 'destroyed';
    const cbDisabled = !canDestroy ? 'disabled' : '';
    const resCell    = canDestroy
      ? `<span class="bm-res-badge">${b.provisioned_count} resource${b.provisioned_count !== 1 ? 's' : ''}</span>`
      : destroyed
        ? `<span class="bm-res-badge bm-res-gone">destroyed</span>`
        : '—';
    return `
    <tr>
      <td class="cb-col"><input type="checkbox" class="bm-row-cb" data-id="${b.id}" data-can-destroy="${canDestroy}" ${cbDisabled} onchange="_bmOnCbChange()"></td>
      <td class="mono">${b.id.slice(0, 8)}</td>
      <td>${b.template}</td>
      <td><span id="bm-hist-status-${b.id}" class="badge badge-${_bmStatusClass(b.status)}">${b.status}</span></td>
      <td>${resCell}</td>
      <td>${b.created_at ? fmtDate(b.created_at) : '—'}</td>
      <td>${_bmDuration(b)}</td>
      <td><button class="btn btn-ghost btn-sm" onclick="bmViewLog('${b.id}')">Log</button></td>
    </tr>`;
  }).join('');
}

function _bmOnCbChange() {
  const checked = document.querySelectorAll('.bm-row-cb:checked:not(:disabled)');
  document.getElementById('bm-destroy-btn').style.display = checked.length ? 'inline-flex' : 'none';
  const all = document.querySelectorAll('.bm-row-cb:not(:disabled)');
  document.getElementById('bm-sel-all').checked = all.length > 0 && checked.length === all.length;
}

function _bmToggleSelAll(cb) {
  document.querySelectorAll('.bm-row-cb:not(:disabled)').forEach(el => el.checked = cb.checked);
  _bmOnCbChange();
}

async function bmDestroySelected() {
  const checked = Array.from(document.querySelectorAll('.bm-row-cb:checked:not(:disabled)'));
  if (!checked.length) return;
  const ids = checked.map(el => el.dataset.id);
  if (!confirm(`Destroy all resources from ${ids.length} build${ids.length > 1 ? 's' : ''}?\nThis cannot be undone.`)) return;

  const btn = document.getElementById('bm-destroy-btn');
  btn.disabled = true;
  btn.textContent = 'Destroying…';

  await Promise.allSettled(ids.map(id => api('DELETE', `/v1/builds/${id}`)));

  btn.disabled = false;
  btn.textContent = '🗑 Destroy Resources';
  await _bmLoadHistory();
  loadDashboard();
}

function _bmStatusClass(s) {
  return s === 'success' ? 'running' : s === 'failed' ? 'error' : s === 'destroyed' ? 'stopped' : 'pending';
}

function _bmDuration(b) {
  if (!b.started_at) return '—';
  const end = b.finished_at ? new Date(b.finished_at) : new Date();
  const secs = Math.round((end - new Date(b.started_at)) / 1000);
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

function _esc(s) {
  return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
}
