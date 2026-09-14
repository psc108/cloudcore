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
      <td>
        <button class="btn btn-ghost btn-sm" onclick="toggleShareFiles('${s.id}','${sh.name}')">Files ▾</button>
        <button class="btn btn-danger btn-sm" onclick="removeNFSShare('${s.id}','${sh.name}')">Remove</button>
      </td>
    </tr>
    <tr id="nfs-files-row-${s.id}-${sh.name}" class="files-row" style="display:none">
      <td colspan="4">${_filesPanel(s.id, sh.name)}</td>
    </tr>`).join('');
}

// ── Share files — drag-and-drop upload/browse/delete ───────────────────────

function _filesPanel(nfsId, shareName) {
  return `<div class="files-panel">
    <div class="dropzone"
         ondragover="event.preventDefault(); this.classList.add('drag-over')"
         ondragleave="this.classList.remove('drag-over')"
         ondrop="handleFileDrop(event, '${nfsId}', '${shareName}')">
      <span>Drag &amp; drop files here to upload, or</span>
      <label class="btn btn-ghost btn-sm file-picker-btn">
        Browse…
        <input type="file" multiple style="display:none" onchange="handleFilePick(event, '${nfsId}', '${shareName}')">
      </label>
    </div>
    <div class="upload-list" id="upload-list-${nfsId}-${shareName}"></div>
    <table class="files-table">
      <thead><tr><th>Name</th><th>Size</th><th>Modified</th><th></th></tr></thead>
      <tbody id="files-rows-${nfsId}-${shareName}"><tr><td colspan="4" style="color:var(--text-muted);padding:8px">Loading…</td></tr></tbody>
    </table>
  </div>`;
}

function toggleShareFiles(nfsId, shareName) {
  const row = document.getElementById(`nfs-files-row-${nfsId}-${shareName}`);
  const nowVisible = row.style.display === 'none';
  row.style.display = nowVisible ? 'table-row' : 'none';
  if (nowVisible) loadShareFiles(nfsId, shareName);
}

async function loadShareFiles(nfsId, shareName) {
  const tbody = document.getElementById(`files-rows-${nfsId}-${shareName}`);
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="4" style="color:var(--text-muted);padding:8px">Loading…</td></tr>';
  try {
    const data  = await api('GET', `/v1/nfs-servers/${nfsId}/shares/${shareName}/files`);
    const items = data.items || [];
    tbody.innerHTML = items.length
      ? items.map(f => `
        <tr>
          <td class="mono">${f.name}</td>
          <td class="text-muted">${formatBytes(f.size)}</td>
          <td class="text-muted">${new Date(f.modified * 1000).toLocaleString()}</td>
          <td><button class="btn btn-danger btn-sm" onclick="deleteShareFile('${nfsId}','${shareName}','${f.name}')">Delete</button></td>
        </tr>`).join('')
      : '<tr><td colspan="4" style="color:var(--text-muted);padding:8px">No files yet — drag some in above.</td></tr>';
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4">Error: ${e.message}</td></tr>`;
  }
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return `${n.toFixed(1)} ${units[i]}`;
}

function handleFileDrop(event, nfsId, shareName) {
  event.preventDefault();
  event.currentTarget.classList.remove('drag-over');
  uploadFiles(event.dataTransfer.files, nfsId, shareName);
}

function handleFilePick(event, nfsId, shareName) {
  uploadFiles(event.target.files, nfsId, shareName);
  event.target.value = '';
}

function uploadFiles(fileList, nfsId, shareName) {
  Array.from(fileList).forEach(file => uploadOneFile(file, nfsId, shareName));
}

function uploadOneFile(file, nfsId, shareName) {
  const host = document.getElementById(`upload-list-${nfsId}-${shareName}`);
  if (!host) return;
  const rowId = `up-${nfsId}-${shareName}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  host.insertAdjacentHTML('beforeend', `
    <div class="upload-row" id="${rowId}">
      <span class="upload-name">${file.name}</span>
      <div class="upload-bar"><div class="upload-bar-fill" style="width:0%"></div></div>
      <span class="upload-pct">0%</span>
    </div>`);
  const row  = document.getElementById(rowId);
  const fill = row.querySelector('.upload-bar-fill');
  const pct  = row.querySelector('.upload-pct');

  const xhr = new XMLHttpRequest();
  xhr.open('PUT', `${API_BASE}/v1/nfs-servers/${nfsId}/shares/${shareName}/files/${encodeURIComponent(file.name)}`);
  xhr.setRequestHeader('Authorization', `Bearer ${API_TOKEN}`);
  xhr.setRequestHeader('Content-Type', 'application/octet-stream');
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const p = Math.round((e.loaded / e.total) * 100);
    fill.style.width = `${p}%`;
    pct.textContent = `${p}%`;
  };
  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      pct.textContent = 'Done';
      toast(`Uploaded "${file.name}"`, 'success');
      loadShareFiles(nfsId, shareName);
      setTimeout(() => row.remove(), 2000);
    } else {
      let detail = `HTTP ${xhr.status}`;
      try { detail = JSON.parse(xhr.responseText).detail || detail; } catch {}
      pct.textContent = 'Failed';
      row.classList.add('upload-error');
      toast(`Upload of "${file.name}" failed: ${detail}`, 'error');
    }
  };
  xhr.onerror = () => {
    pct.textContent = 'Failed';
    row.classList.add('upload-error');
    toast(`Upload of "${file.name}" failed: network error`, 'error');
  };
  xhr.send(file);
}

async function deleteShareFile(nfsId, shareName, filename) {
  if (!confirm(`Delete "${filename}"?`)) return;
  try {
    await api('DELETE', `/v1/nfs-servers/${nfsId}/shares/${shareName}/files/${encodeURIComponent(filename)}`);
    toast(`Deleted "${filename}"`, 'success');
    loadShareFiles(nfsId, shareName);
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

function _nfsSSHPanel(s) {
  if (s.status !== 'running' || !sshHasAccess(s)) {
    return `<div class="ssh-panel"><span class="text-muted" style="font-size:13px">SSH available once NFS server is running.</span></div>`;
  }
  const keyPath = '~/.ssh/cloudcore_ed25519';
  const cmd = s.ssh_port
    ? `ssh -i ${keyPath} -p ${s.ssh_port} ubuntu@127.0.0.1`
    : `ssh -i ${keyPath} ubuntu@${s.private_ip}`;
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
