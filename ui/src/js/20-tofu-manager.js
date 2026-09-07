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
  await _tfRenderVarForm(dirName, tpl, data.vars || {});
  const panel = document.getElementById('tf-var-panel');
  panel.style.display = 'block';
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// Variables named exactly this get rendered as a <select> of host USB
// devices instead of a free-text field — trainees shouldn't have to look
// up a vendor:product ID by hand. Only this one exists today; a name
// match is simpler than adding a schema-level "kind" flag for a field
// type that only one template uses.
const _TF_USB_DEVICE_VAR = 'usb_device_id';

async function _tfRenderVarForm(dirName, tpl, schema) {
  document.getElementById('tf-form-title').textContent = tpl ? tpl.title : dirName;
  document.getElementById('tf-submit-dirname').value = dirName;

  const container = document.getElementById('tf-var-fields');
  const editable = Object.entries(schema).filter(([, v]) => !v.derived);

  if (!editable.length) {
    container.innerHTML = '<div class="bm-empty" style="padding:12px 0">No variables to configure.</div>';
    return;
  }

  let usbDevices = null;
  if (editable.some(([key]) => key === _TF_USB_DEVICE_VAR)) {
    try {
      const usbData = await api('GET', '/v1/usb-devices');
      usbDevices = usbData.items || [];
    } catch (e) {
      usbDevices = [];
    }
  }

  container.innerHTML = editable.map(([key, meta]) => {
    if (key === _TF_USB_DEVICE_VAR) return _tfRenderUsbField(key, meta, usbDevices);
    return `
      <div class="field">
        <label>${key.replace(/_/g, ' ')}${meta.required ? ' <span class="bm-required">*</span>' : ''}</label>
        <input type="${key.includes('token') ? 'password' : 'text'}"
               id="tf-var-${key}"
               data-key="${key}"
               data-required="${meta.required ? '1' : '0'}"
               placeholder="${meta.required ? 'Required — no default value' : ''}"
               value="${_esc(String(meta.default ?? ''))}">
      </div>
    `;
  }).join('');
}

function _tfRenderUsbField(key, meta, devices) {
  const label = `${key.replace(/_/g, ' ')}${meta.required ? ' <span class="bm-required">*</span>' : ''}`;

  if (devices === null || !devices.length) {
    return `
      <div class="field">
        <label>${label}</label>
        <select id="tf-var-${key}" data-key="${key}" data-required="${meta.required ? '1' : '0'}" disabled>
          <option value="">No USB devices detected</option>
        </select>
        <span class="bm-field-hint">Plug in the adapter into the CloudCore host, then reopen this template.</span>
      </div>
    `;
  }

  const toOption = d => {
    const id = `${d.vendor_id}:${d.product_id}`;
    let suffix = '';
    if (d.blocked) suffix = ` — blocked (${d.block_reason || 'unsafe device'})`;
    else if (d.attached_to) suffix = ` — already attached elsewhere`;
    const disabled = (d.blocked || d.attached_to) ? 'disabled' : '';
    return `<option value="${_esc(id)}" ${disabled}>${_esc(d.description || id)} (${_esc(id)})${_esc(suffix)}</option>`;
  };

  const likely = devices.filter(d => d.likely_wifi_adapter);
  const other  = devices.filter(d => !d.likely_wifi_adapter);
  const otherIds = other.map(d => `${d.vendor_id}:${d.product_id}`);

  const likelyGroup = likely.length
    ? `<optgroup label="Likely WiFi adapters">${likely.map(toOption).join('')}</optgroup>` : '';
  const otherGroup = other.length
    ? `<optgroup label="Other detected devices">${other.map(toOption).join('')}</optgroup>` : '';

  const anyEligible = devices.some(d => !d.blocked && !d.attached_to);
  const anyLikelyEligible = likely.some(d => !d.blocked && !d.attached_to);

  return `
    <div class="field">
      <label>${label}</label>
      <select id="tf-var-${key}" data-key="${key}" data-required="${meta.required ? '1' : '0'}"
              data-nonwifi-ids="${_esc(JSON.stringify(otherIds))}"
              onchange="_tfUsbFieldChanged(this)">
        <option value="">-- select a USB adapter --</option>
        ${likelyGroup}
        ${otherGroup}
      </select>
      <span class="bm-field-hint bm-required" id="tf-var-${key}-warn" style="display:none">
        This isn't recognized as a WiFi adapter — double check it's the right device before building.
      </span>
      ${anyEligible
        ? (anyLikelyEligible ? '' : '<span class="bm-field-hint">No obvious WiFi adapter detected — check "Other detected devices" if you know which one it is.</span>')
        : '<span class="bm-field-hint">No eligible device — every detected device is blocked or already in use.</span>'}
    </div>
  `;
}

function _tfUsbFieldChanged(select) {
  const warn = document.getElementById(`${select.id}-warn`);
  if (!warn) return;
  const nonWifiIds = JSON.parse(select.dataset.nonwifiIds || '[]');
  warn.style.display = nonWifiIds.includes(select.value) ? 'block' : 'none';
}

async function tfSubmitBuild() {
  const dirName = document.getElementById('tf-submit-dirname').value;
  if (!dirName) { toast('Select a template first', 'error'); return; }

  const vars = {};
  const missing = [];
  document.querySelectorAll('#tf-var-fields input[data-key], #tf-var-fields select[data-key]').forEach(el => {
    el.classList.remove('bm-field-error');
    const val = el.value.trim();
    if (val) {
      vars[el.dataset.key] = val;
    } else if (el.dataset.required === '1') {
      missing.push(el.dataset.key);
      el.classList.add('bm-field-error');
    }
  });

  if (missing.length) {
    toast(`Missing required value${missing.length > 1 ? 's' : ''}: ${missing.join(', ')}`, 'error');
    return;
  }

  // Selecting a device outside "Likely WiFi adapters" is easy to do by
  // mistake (e.g. picking the host's own Bluetooth chip) and the failure
  // mode isn't a clean error — it's a fully "successful" build with no
  // working radio. One extra confirmation catches that before it burns
  // several minutes of provisioning.
  for (const select of document.querySelectorAll('#tf-var-fields select[data-nonwifi-ids]')) {
    const nonWifiIds = JSON.parse(select.dataset.nonwifiIds || '[]');
    if (nonWifiIds.includes(select.value)) {
      const label = select.options[select.selectedIndex]?.textContent.trim() || select.value;
      if (!confirm(`"${label}" doesn't look like a WiFi adapter. Build anyway?`)) return;
    }
  }

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
      <td style="display:flex;gap:4px">
        <button class="btn btn-ghost btn-sm" onclick="tfViewLog('${b.id}')">Log</button>
        ${canDestroy ? `<button class="btn btn-danger btn-sm" onclick="tfDestroySingle('${b.id}','${b.template}')">Destroy</button>` : ''}
      </td>
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

async function tfDestroySingle(buildId, template) {
  if (!confirm(`Destroy all resources from build ${buildId.slice(0,8)}… (${template})?\nThis cannot be undone.`)) return;
  try {
    await api('DELETE', `/v1/tofu/builds/${buildId}`);
    toast('Resources destroyed', 'success');
  } catch (e) {
    toast(`Destroy failed: ${e.message}`, 'error');
  }
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
