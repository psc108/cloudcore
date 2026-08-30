// ── Load Balancers ────────────────────────────────────────────────────────────
function renderBackendRows(lb) {
  if (!lb.backends.length) return `<tr><td colspan="4" style="color:var(--text-muted);padding:8px">No backends yet.</td></tr>`;
  return lb.backends.map(b => `
    <tr>
      <td>${b.name}</td>
      <td class="mono">${b.address}</td>
      <td class="mono">${b.port}</td>
      <td><button class="btn btn-danger btn-sm" onclick="removeBackend('${lb.id}','${b.name}')">Remove</button></td>
    </tr>`).join('');
}

function renderListenerRows(lb) {
  if (!lb.listeners || !lb.listeners.length) return `<tr><td colspan="4" style="color:var(--text-muted);padding:8px">No listeners yet.</td></tr>`;
  return lb.listeners.map(l => `
    <tr>
      <td class="mono">${l.port}</td>
      <td>${l.protocol}</td>
      <td>${l.default_action}</td>
      <td><button class="btn btn-danger btn-sm" onclick="removeListener('${lb.id}','${l.id}')">Remove</button></td>
    </tr>`).join('');
}

function renderHealthCheck(lb) {
  const hc = lb.health_check;
  if (!hc || !hc.protocol) return `<span style="color:var(--text-muted);font-size:13px">No health check configured.</span>`;
  return `<div style="font-size:13px;display:flex;gap:16px;flex-wrap:wrap;align-items:center">
    <span><strong>Protocol:</strong> ${hc.protocol}</span>
    ${hc.path ? `<span><strong>Path:</strong> <code>${hc.path}</code></span>` : ''}
    <span><strong>Interval:</strong> ${hc.interval}s</span>
    <span><strong>Healthy:</strong> ${hc.healthy_threshold} checks</span>
    <span><strong>Unhealthy:</strong> ${hc.unhealthy_threshold} checks</span>
    <button class="btn btn-danger btn-sm" onclick="deleteHealthCheck('${lb.id}')">Remove</button>
  </div>`;
}

function toggleBackends(lbId) {
  const row = document.getElementById(`lb-backends-${lbId}`);
  row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}

function toggleListeners(lbId) {
  const row = document.getElementById(`lb-listeners-${lbId}`);
  row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}

async function addBackend(lbId) {
  const name = document.getElementById(`bn-name-${lbId}`).value.trim();
  const addr = document.getElementById(`bn-addr-${lbId}`).value.trim();
  const port = document.getElementById(`bn-port-${lbId}`).value.trim();
  if (!name || !addr || !port) { toast('Name, address and port are required', 'error'); return; }
  try {
    await api('POST', `/v1/load-balancers/${lbId}/backends`, { name, address: addr, port: parseInt(port) });
    toast(`Backend "${name}" added`, 'success');
    loadLBs();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function removeBackend(lbId, name) {
  if (!confirm(`Remove backend "${name}"?`)) return;
  try {
    await api('DELETE', `/v1/load-balancers/${lbId}/backends/${name}`);
    toast(`Backend "${name}" removed`, 'success');
    loadLBs();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function addListener(lbId) {
  const port     = document.getElementById(`lst-port-${lbId}`).value.trim();
  const protocol = document.getElementById(`lst-proto-${lbId}`).value;
  const action   = document.getElementById(`lst-action-${lbId}`).value.trim() || 'forward';
  if (!port) { toast('Port is required', 'error'); return; }
  try {
    await api('POST', `/v1/load-balancers/${lbId}/listeners`, { port: parseInt(port), protocol, default_action: action });
    toast(`Listener on port ${port} added`, 'success');
    loadLBs();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function removeListener(lbId, listenerId) {
  if (!confirm('Remove this listener?')) return;
  try {
    await api('DELETE', `/v1/load-balancers/${lbId}/listeners/${listenerId}`);
    toast('Listener removed', 'success');
    loadLBs();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function saveHealthCheck(lbId) {
  const protocol  = document.getElementById(`hc-proto-${lbId}`).value;
  const path      = document.getElementById(`hc-path-${lbId}`).value.trim() || '/';
  const interval  = parseInt(document.getElementById(`hc-interval-${lbId}`).value) || 30;
  const healthy   = parseInt(document.getElementById(`hc-healthy-${lbId}`).value) || 2;
  const unhealthy = parseInt(document.getElementById(`hc-unhealthy-${lbId}`).value) || 3;
  try {
    await api('PUT', `/v1/load-balancers/${lbId}/health-check`, { protocol, path, interval, healthy_threshold: healthy, unhealthy_threshold: unhealthy });
    toast('Health check saved', 'success');
    loadLBs();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function deleteHealthCheck(lbId) {
  if (!confirm('Remove health check?')) return;
  try {
    await api('DELETE', `/v1/load-balancers/${lbId}/health-check`);
    toast('Health check removed', 'success');
    loadLBs();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

function _hcProtoChange(lbId) {
  const proto = document.getElementById(`hc-proto-${lbId}`).value;
  document.getElementById(`hc-path-wrap-${lbId}`).style.display = proto === 'HTTP' ? '' : 'none';
}

async function loadLBs() {
  const tbody = document.getElementById('lb-tbody');
  tbody.innerHTML = '<tr class="empty-row"><td colspan="10">Loading…</td></tr>';
  try {
    const data  = await api('GET', '/v1/load-balancers');
    const items = data.items.filter(lb => lb.status !== 'deleted');
    if (!items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="11">No load balancers found. Create one above.</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(lb => `
      <tr id="lb-row-${lb.id}">
        <td class="cb-col"><input type="checkbox" class="row-cb" data-type="lb" data-id="${lb.id}" data-name="${lb.name}" onchange="_onRowCbChange('lb')"></td>
        <td><strong>${lb.name}</strong></td>
        <td>${shortId(lb.id)}</td>
        <td>${lb.type}</td>
        <td class="mono text-muted">${lb.vpc_id ? lb.vpc_id.slice(0,8) + '…' : '—'}</td>
        <td class="mono">${lb.listen_port ? '127.0.0.1:' + lb.listen_port : '—'}</td>
        <td>
          <button class="expand-btn" onclick="toggleBackends('${lb.id}')">
            ${lb.backends.length} backend${lb.backends.length !== 1 ? 's' : ''} ▾
          </button>
          <button class="expand-btn" style="margin-left:4px" onclick="toggleListeners('${lb.id}')">
            ${(lb.listeners||[]).length} listener${(lb.listeners||[]).length !== 1 ? 's' : ''} ▾
          </button>
        </td>
        <td>${lb.internal ? 'Internal' : 'Internet-facing'}</td>
        <td>${badge(lb.status)}</td>
        <td>${fmtDate(lb.created_at)}</td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteLB('${lb.id}','${lb.name}')">Delete</button></td>
      </tr>
      <tr id="lb-backends-${lb.id}" class="backends-row" style="display:none">
        <td colspan="11">
          <div class="backends-panel">
            <h4 style="margin:0 0 8px;font-size:13px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em">Backends</h4>
            <table>
              <thead><tr><th>Name</th><th>Address</th><th>Port</th><th></th></tr></thead>
              <tbody>${renderBackendRows(lb)}</tbody>
            </table>
            <div class="backends-add">
              <div class="field"><label>Name</label><input id="bn-name-${lb.id}" placeholder="server-01"></div>
              <div class="field"><label>Address</label><input id="bn-addr-${lb.id}" placeholder="192.168.100.10"></div>
              <div class="field narrow"><label>Port</label><input id="bn-port-${lb.id}" placeholder="80" type="number"></div>
              <button class="btn btn-primary btn-sm" onclick="addBackend('${lb.id}')">Add</button>
            </div>
          </div>
        </td>
      </tr>
      <tr id="lb-listeners-${lb.id}" class="backends-row" style="display:none">
        <td colspan="11">
          <div class="backends-panel">
            <h4 style="margin:0 0 8px;font-size:13px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em">Listeners</h4>
            <table>
              <thead><tr><th>Port</th><th>Protocol</th><th>Default Action</th><th></th></tr></thead>
              <tbody>${renderListenerRows(lb)}</tbody>
            </table>
            <div class="backends-add">
              <div class="field narrow"><label>Port</label><input id="lst-port-${lb.id}" placeholder="80" type="number"></div>
              <div class="field"><label>Protocol</label>
                <select id="lst-proto-${lb.id}">
                  <option value="HTTP">HTTP</option>
                  <option value="HTTPS">HTTPS</option>
                  <option value="TCP">TCP</option>
                </select>
              </div>
              <div class="field"><label>Default Action</label><input id="lst-action-${lb.id}" placeholder="forward" value="forward"></div>
              <button class="btn btn-primary btn-sm" onclick="addListener('${lb.id}')">Add</button>
            </div>
            <div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border)">
              <h4 style="margin:0 0 8px;font-size:13px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em">Health Check</h4>
              <div style="margin-bottom:10px">${renderHealthCheck(lb)}</div>
              <div class="backends-add" style="flex-wrap:wrap">
                <div class="field"><label>Protocol</label>
                  <select id="hc-proto-${lb.id}" onchange="_hcProtoChange('${lb.id}')">
                    <option value="HTTP">HTTP</option>
                    <option value="TCP">TCP</option>
                  </select>
                </div>
                <div class="field" id="hc-path-wrap-${lb.id}"><label>Path</label><input id="hc-path-${lb.id}" placeholder="/" value="${(lb.health_check||{}).path||'/'}"></div>
                <div class="field narrow"><label>Interval (s)</label><input id="hc-interval-${lb.id}" type="number" value="${(lb.health_check||{}).interval||30}"></div>
                <div class="field narrow"><label>Healthy</label><input id="hc-healthy-${lb.id}" type="number" value="${(lb.health_check||{}).healthy_threshold||2}"></div>
                <div class="field narrow"><label>Unhealthy</label><input id="hc-unhealthy-${lb.id}" type="number" value="${(lb.health_check||{}).unhealthy_threshold||3}"></div>
                <button class="btn btn-primary btn-sm" onclick="saveHealthCheck('${lb.id}')">Save</button>
              </div>
            </div>
          </div>
        </td>
      </tr>`).join('');
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="11">Error: ${e.message}</td></tr>`;
  }
}

async function populateLBForm() {
  const vpcSel = document.getElementById('lb-vpc');
  vpcSel.innerHTML = '<option value="">Loading…</option>';
  try {
    const data   = await api('GET', '/v1/vpcs');
    const active = data.items.filter(v => v.status !== 'deleted');
    vpcSel.innerHTML = active.length
      ? active.map(v => `<option value="${v.id}">${v.name}</option>`).join('')
      : '<option value="">No VPCs — create one first</option>';
  } catch {
    vpcSel.innerHTML = '<option value="">Error loading VPCs</option>';
  }
}

function toggleLBForm() {
  const form    = document.getElementById('lb-form');
  const visible = form.style.display !== 'none';
  form.style.display = visible ? 'none' : 'block';
  if (!visible) populateLBForm();
}

async function createLB() {
  const name  = document.getElementById('lb-name').value.trim();
  const vpcId = document.getElementById('lb-vpc').value;
  if (!name)  { toast('Name is required', 'error'); return; }
  if (!vpcId) { toast('VPC is required', 'error'); return; }
  try {
    await api('POST', '/v1/load-balancers', {
      name,
      type:     document.getElementById('lb-type').value,
      vpc_id:   vpcId,
      internal: document.getElementById('lb-internal').value === 'true',
      tags:     parseTags(document.getElementById('lb-tags').value),
    });
    toast(`Load balancer "${name}" created`, 'success');
    document.getElementById('lb-form').style.display = 'none';
    document.getElementById('lb-name').value = '';
    document.getElementById('lb-tags').value = '';
    loadLBs();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function deleteLB(id, name) {
  if (!confirm(`Delete load balancer "${name}"?`)) return;
  try {
    await api('DELETE', `/v1/load-balancers/${id}`);
    toast(`Load balancer "${name}" deleted`, 'success');
    loadLBs();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}
