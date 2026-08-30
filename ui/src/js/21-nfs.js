// ── NFS Servers ───────────────────────────────────────────────────────────────

async function loadNFS() {
  const tbody = document.getElementById('nfs-tbody');
  tbody.innerHTML = '<tr class="empty-row"><td colspan="8">Loading…</td></tr>';
  try {
    const data  = await api('GET', '/v1/nfs-servers');
    const items = data.items || [];
    if (!items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="8">No NFS servers found. Create one above.</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(s => `
      <tr id="nfs-row-${s.id}">
        <td class="cb-col"><input type="checkbox" class="row-cb" data-type="nfs" data-id="${s.id}" data-name="${s.name}" onchange="_onRowCbChange('nfs')"></td>
        <td><strong>${s.name}</strong></td>
        <td>${shortId(s.id)}</td>
        <td class="mono text-muted">${s.vpc_id ? s.vpc_id.slice(0,8) + '…' : '—'}</td>
        <td class="mono">${s.private_ip || '—'}</td>
        <td>
          <button class="expand-btn" onclick="toggleNFSShares('${s.id}')">
            ${(s.shares || []).length} share${(s.shares || []).length !== 1 ? 's' : ''} ▾
          </button>
        </td>
        <td>${badge(s.status)}</td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteNFSServer('${s.id}','${s.name}')">Delete</button></td>
      </tr>
      <tr id="nfs-shares-${s.id}" class="backends-row" style="display:none">
        <td colspan="8">
          <div class="backends-panel">
            ${_nfsSSHPanel(s)}
            <table>
              <thead><tr><th>Name</th><th>Path</th><th>Clients</th><th></th></tr></thead>
              <tbody id="nfs-share-rows-${s.id}">${_renderShareRows(s)}</tbody>
            </table>
            <div class="backends-add">
              <div class="field"><label>Share name</label><input id="ns-name-${s.id}" placeholder="data"></div>
              <div class="field"><label>Clients</label><input id="ns-clients-${s.id}" placeholder="vpc" value="vpc"></div>
              <button class="btn btn-primary btn-sm" onclick="addNFSShare('${s.id}')">Add Share</button>
            </div>
          </div>
        </td>
      </tr>`).join('');
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="8">Error: ${e.message}</td></tr>`;
  }
}

function _renderShareRows(s) {
  if (!(s.shares || []).length) return '<tr><td colspan="4" style="color:var(--text-muted);padding:8px">No shares.</td></tr>';
  return s.shares.map(sh => `
    <tr>
      <td>${sh.name}</td>
      <td class="mono text-muted">${sh.path || '/exports/' + sh.name}</td>
      <td class="text-muted">${sh.clients || 'vpc'}</td>
      <td><button class="btn btn-danger btn-sm" onclick="removeNFSShare('${s.id}','${sh.name}')">Remove</button></td>
    </tr>`).join('');
}

function _nfsSSHPanel(s) {
  if (s.status !== 'running' || !s.ssh_port) {
    return `<div class="ssh-panel"><span class="text-muted" style="font-size:13px">SSH available once NFS server is running.</span></div>`;
  }
  const keyPath = '~/.ssh/cloudcore_ed25519';
  const cmd = `ssh -i ${keyPath} -p ${s.ssh_port} ubuntu@127.0.0.1`;
  return `<div class="ssh-panel">
    <div class="ssh-block">
      <label>SSH into NFS server</label>
      <div class="ssh-cmd"><code>${cmd}</code><button class="copy-btn" onclick="copyText('${cmd}',this)" title="Copy">⎘</button></div>
    </div>
  </div>`;
}

function toggleNFSShares(nfsId) {
  const row = document.getElementById(`nfs-shares-${nfsId}`);
  row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}

async function addNFSShare(nfsId) {
  const name    = document.getElementById(`ns-name-${nfsId}`).value.trim();
  const clients = document.getElementById(`ns-clients-${nfsId}`).value.trim() || 'vpc';
  if (!name) { toast('Share name is required', 'error'); return; }
  try {
    await api('POST', `/v1/nfs-servers/${nfsId}/shares`, { name, clients });
    toast(`Share "${name}" added`, 'success');
    loadNFS();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function removeNFSShare(nfsId, name) {
  if (!confirm(`Remove share "${name}"?`)) return;
  try {
    await api('DELETE', `/v1/nfs-servers/${nfsId}/shares/${name}`);
    toast(`Share "${name}" removed`, 'success');
    loadNFS();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function populateNFSForm() {
  const vpcSel = document.getElementById('nfs-vpc');
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

function toggleNFSForm() {
  const form    = document.getElementById('nfs-form');
  const visible = form.style.display !== 'none';
  form.style.display = visible ? 'none' : 'block';
  if (!visible) populateNFSForm();
}

async function createNFSServer() {
  const name   = document.getElementById('nfs-name').value.trim();
  const vpcId  = document.getElementById('nfs-vpc').value;
  const flavor = document.getElementById('nfs-flavor').value;
  const diskGb = parseInt(document.getElementById('nfs-disk').value) || 20;
  const sharesRaw = document.getElementById('nfs-shares-input').value.trim();
  if (!name)  { toast('Name is required', 'error'); return; }
  if (!vpcId) { toast('VPC is required', 'error'); return; }
  const shares = sharesRaw
    ? sharesRaw.split(',').map(s => ({ name: s.trim() })).filter(s => s.name)
    : [];
  try {
    await api('POST', '/v1/nfs-servers', { name, vpc_id: vpcId, flavor, disk_gb: diskGb, shares });
    toast(`NFS server "${name}" provisioning…`, 'success');
    document.getElementById('nfs-form').style.display = 'none';
    document.getElementById('nfs-name').value = '';
    document.getElementById('nfs-shares-input').value = '';
    loadNFS();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function deleteNFSServer(id, name) {
  if (!confirm(`Delete NFS server "${name}"? All shares will be removed.`)) return;
  try {
    await api('DELETE', `/v1/nfs-servers/${id}`);
    toast(`NFS server "${name}" deleted`, 'success');
    loadNFS();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}
