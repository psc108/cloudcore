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

function toggleBackends(lbId) {
  const row = document.getElementById(`lb-backends-${lbId}`);
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
        </td>
        <td>${lb.internal ? 'Internal' : 'Internet-facing'}</td>
        <td>${badge(lb.status)}</td>
        <td>${fmtDate(lb.created_at)}</td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteLB('${lb.id}','${lb.name}')">Delete</button></td>
      </tr>
      <tr id="lb-backends-${lb.id}" class="backends-row" style="display:none">
        <td colspan="11">
          <div class="backends-panel">
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
