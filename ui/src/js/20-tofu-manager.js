// ── OpenTofu Build Manager ────────────────────────────────────────────────────

let _tfTemplates = [];
let _tfActiveBuildId = null;
let _tfLogEs = null;

async function loadTofuManager() {
  try {
    await _tfLoadTemplates();
    await _tfLoadHistory();
  } catch (e) {
    document.getElementById('tf-template-grid').innerHTML = `<div class="bm-empty">Error: ${e.message}</div>`;
  }
}

// ── Templates ────────────────────────────────────────────────────────────────

async function _tfLoadTemplates() {
  const data = await api('GET', '/v1/tofu/templates');
  _tfTemplates = data.items || [];
  const grid = document.getElementById('tf-template-grid');
  if (!_tfTemplates.length) {
    grid.innerHTML = '<div class="bm-empty">No OpenTofu examples found.</div>';
    return;
  }
  grid.innerHTML = _tfTemplates.map(t => `
    <div class="bm-template-card" onclick="tfSelectTemplate('${t.filename}')">
      <div class="bm-tpl-title">${t.title}</div>
      <div class="bm-tpl-desc">${t.description}</div>
      <div class="bm-tpl-tags">${(t.resources || []).map(r =>
        `<span class="bm-tag">${r.replace('_', ' ')}</span>`).join('')}</div>
    </div>
  `).join('');
}

async function tfSelectTemplate(dirName) {
  document.querySelectorAll('#tf-template-grid .bm-template-card').forEach(c => c.classList.remove('selected'));
  const tpl = _tfTemplates.find(t => t.filename === dirName);
  if (tpl) {
    const idx = _tfTemplates.indexOf(tpl);
    const cards = document.querySelectorAll('#tf-template-grid .bm-template-card');
    if (cards[idx]) cards[idx].classList.add('selected');
  }

  const data = await api('GET', `/v1/tofu/templates/${dirName}/vars`);
  _tfRenderVarForm(dirName, tpl, data.vars || {});
  const panel = document.getElementById('tf-var-panel');
  panel.style.display = 'block';
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function _tfRenderVarForm(dirName, tpl, schema) {
  document.getElementById('tf-form-title').textContent = tpl ? tpl.title : dirName;
  document.getElementById('tf-submit-dirname').value = dirName;

  const container = document.getElementById('tf-var-fields');
  const editable = Object.entries(schema).filter(([, v]) => !v.derived);

  if (!editable.length) {
    container.innerHTML = '<div class="bm-empty" style="padding:12px 0">No variables to configure.</div>';
    return;
  }

  container.innerHTML = editable.map(([key, meta]) => `
    <div class="field">
      <label>${key.replace(/_/g, ' ')}</label>
      <input type="${key.includes('token') ? 'password' : 'text'}"
             id="tf-var-${key}"
             data-key="${key}"
             value="${_esc(String(meta.default ?? ''))}">
    </div>
  `).join('');
}

async function tfSubmitBuild() {
  const dirName = document.getElementById('tf-submit-dirname').value;
  if (!dirName) { toast('Select a template first', 'error'); return; }

  const vars = {};
  document.querySelectorAll('#tf-var-fields input[data-key]').forEach(el => {
    if (el.value.trim()) vars[el.dataset.key] = el.value.trim();
  });

  const btn = document.getElementById('tf-submit-btn');
  btn.disabled = true;
  btn.textContent = 'Submitting…';

  let data;
  try {
    data = await api('POST', '/v1/tofu/builds', { template: dirName, vars });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Run Apply';
    toast(e.message || 'Submit failed', 'error');
    return;
  }
  btn.disabled = false;
  btn.textContent = 'Run Apply';
  toast(`Build started: ${data.id.slice(0, 8)}…`, 'success');
  document.getElementById('tf-var-panel').style.display = 'none';
  document.querySelectorAll('#tf-template-grid .bm-template-card').forEach(c => c.classList.remove('selected'));
  _tfOpenLog(data.id);
  await _tfLoadHistory();
}

// ── Log viewer ───────────────────────────────────────────────────────────────

function _tfOpenLog(buildId) {
  _tfActiveBuildId = buildId;
  if (_tfLogEs) { _tfLogEs.close(); _tfLogEs = null; }

  const panel = document.getElementById('tf-log-panel');
  const pre   = document.getElementById('tf-log-pre');
  panel.style.display = 'block';
  pre.textContent = '';
  document.getElementById('tf-log-title').textContent = `Apply log — ${buildId.slice(0, 8)}…`;
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  _tfLogEs = new EventSource(`/v1/tofu/builds/${buildId}/log?token=${API_TOKEN}`);
  _tfLogEs.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.__done__) {
      _tfLogEs.close(); _tfLogEs = null;
      _tfUpdateLogStatus(buildId, msg.status);
      _tfLoadHistory();
      loadDashboard();
      return;
    }
    pre.textContent += msg + '\n';
    pre.scrollTop = pre.scrollHeight;
  };
  _tfLogEs.onerror = () => { if (_tfLogEs) { _tfLogEs.close(); _tfLogEs = null; } };
}

function _tfUpdateLogStatus(buildId, status) {
  const badge = document.getElementById(`tf-hist-status-${buildId}`);
  if (badge) {
    badge.textContent = status;
    badge.className = `badge badge-${_tfStatusClass(status)}`;
  }
}

async function tfViewLog(buildId) {
  _tfActiveBuildId = buildId;
  if (_tfLogEs) { _tfLogEs.close(); _tfLogEs = null; }

  const data = await api('GET', `/v1/tofu/builds/${buildId}`);
  const panel = document.getElementById('tf-log-panel');
  const pre   = document.getElementById('tf-log-pre');
  document.getElementById('tf-log-title').textContent = `Apply log — ${buildId.slice(0, 8)}… (${data.template})`;
  pre.textContent = (data.log || []).join('\n');
  panel.style.display = 'block';
  pre.scrollTop = pre.scrollHeight;
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  if (data.status === 'running' || data.status === 'pending') {
    const offset = (data.log || []).length;
    let sent = offset;
    _tfLogEs = new EventSource(`/v1/tofu/builds/${buildId}/log?token=${API_TOKEN}`);
    _tfLogEs.onmessage = e => {
      const msg = JSON.parse(e.data);
      if (msg.__done__) {
        _tfLogEs.close(); _tfLogEs = null;
        _tfUpdateLogStatus(buildId, msg.status);
        _tfLoadHistory();
        loadDashboard();
        return;
      }
      if (sent > 0) { sent--; return; }
      pre.textContent += msg + '\n';
      pre.scrollTop = pre.scrollHeight;
    };
  }
}

// ── History ──────────────────────────────────────────────────────────────────

async function _tfLoadHistory() {
  const data = await api('GET', '/v1/tofu/builds');
  const tbody = document.getElementById('tf-history-tbody');
  const builds = data.items || [];
  document.getElementById('tf-sel-all').checked = false;
  document.getElementById('tf-destroy-btn').style.display = 'none';
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
      <td class="cb-col"><input type="checkbox" class="tf-row-cb" data-id="${b.id}" ${cbDisabled} onchange="_tfOnCbChange()"></td>
      <td class="mono">${b.id.slice(0, 8)}</td>
      <td>${b.template}</td>
      <td><span id="tf-hist-status-${b.id}" class="badge badge-${_tfStatusClass(b.status)}">${b.status}</span></td>
      <td>${resCell}</td>
      <td>${b.created_at ? fmtDate(b.created_at) : '—'}</td>
      <td>${_tfDuration(b)}</td>
      <td><button class="btn btn-ghost btn-sm" onclick="tfViewLog('${b.id}')">Log</button></td>
    </tr>`;
  }).join('');
}

function _tfOnCbChange() {
  const checked = document.querySelectorAll('.tf-row-cb:checked:not(:disabled)');
  document.getElementById('tf-destroy-btn').style.display = checked.length ? 'inline-flex' : 'none';
  const all = document.querySelectorAll('.tf-row-cb:not(:disabled)');
  document.getElementById('tf-sel-all').checked = all.length > 0 && checked.length === all.length;
}

function _tfToggleSelAll(cb) {
  document.querySelectorAll('.tf-row-cb:not(:disabled)').forEach(el => el.checked = cb.checked);
  _tfOnCbChange();
}

async function tfDestroySelected() {
  const checked = Array.from(document.querySelectorAll('.tf-row-cb:checked:not(:disabled)'));
  if (!checked.length) return;
  const ids = checked.map(el => el.dataset.id);
  if (!confirm(`Destroy all resources from ${ids.length} build${ids.length > 1 ? 's' : ''}?\nThis cannot be undone.`)) return;

  const btn = document.getElementById('tf-destroy-btn');
  btn.disabled = true;
  btn.textContent = 'Destroying…';

  await Promise.allSettled(ids.map(id => api('DELETE', `/v1/tofu/builds/${id}`)));

  btn.disabled = false;
  btn.textContent = '🗑 Destroy Resources';
  await _tfLoadHistory();
  loadDashboard();
}

function _tfStatusClass(s) {
  return s === 'success' ? 'running' : s === 'failed' ? 'error' : s === 'destroyed' ? 'stopped' : 'pending';
}

function _tfDuration(b) {
  if (!b.started_at) return '—';
  const end = b.finished_at ? new Date(b.finished_at) : new Date();
  const secs = Math.round((end - new Date(b.started_at)) / 1000);
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}
