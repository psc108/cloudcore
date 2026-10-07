// ── Instances — polling ───────────────────────────────────────────────────────
let _pollTimer = null;

function startInstancePoll() {
  if (_pollTimer) return;
  _pollTimer = setInterval(async () => {
    const data = await api('GET', '/v1/instances').catch(() => null);
    if (!data) return;
    const items = data.items.filter(i => i.status !== 'deleted');
    if (items.some(i => i.status === 'pending')) renderInstances(items);
    else stopInstancePoll();
  }, 5000);
}

function stopInstancePoll() {
  clearInterval(_pollTimer);
  _pollTimer = null;
}

// ── Instances — load ──────────────────────────────────────────────────────────
async function loadInstances() {
  const tbody = document.getElementById('instance-tbody');
  tbody.innerHTML = '<tr class="empty-row"><td colspan="11">Loading…</td></tr>';
  try {
    const data  = await api('GET', '/v1/instances');
    const items = data.items.filter(i => i.status !== 'deleted');
    renderInstances(items);
    if (items.some(i => i.status === 'pending')) startInstancePoll();
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="11">Error: ${e.message}</td></tr>`;
  }
}

// ── Instances — SSH panel ─────────────────────────────────────────────────────
function sshPanel(i) {
  if (i.status !== 'running' || !sshHasAccess(i)) {
    return `<div class="ssh-panel"><span class="text-muted" style="font-size:13px">SSH available once instance is running.</span></div>`;
  }
  const user    = i.ssh_user || 'ubuntu';
  const keyPath = '~/.local/share/cloudcore/cloudcore_ed25519';
  // Bridge-mode: connect directly to the instance's own IP, standard port
  // 22, no -p/-P flag. SLIRP: through the forwarded port on 127.0.0.1.
  const host    = i.ssh_port ? '127.0.0.1' : i.private_ip;
  const portFlag = i.ssh_port ? `-p ${i.ssh_port} ` : '';
  const scpPortFlag = i.ssh_port ? `-P ${i.ssh_port} ` : '';
  const sshCmd  = `ssh -i ${keyPath} ${portFlag}${user}@${host}`;
  const scpTo   = `scp -i ${keyPath} ${scpPortFlag}<local-file> ${user}@${host}:<remote-path>`;
  const scpFrom = `scp -i ${keyPath} ${scpPortFlag}${user}@${host}:<remote-path> <local-dest>`;
  const p2p     = `ssh -i ~/.ssh/cloudcore_ed25519 ${user}@<other-instance-ip>`;
  return `
    <div class="ssh-panel">
      <div class="ssh-block">
        <label>SSH into instance</label>
        <div class="ssh-cmd"><code>${sshCmd}</code><button class="copy-btn" onclick="copyText('${sshCmd}',this)" title="Copy">⎘</button></div>
      </div>
      <div class="ssh-block">
        <label>SCP — upload file</label>
        <div class="ssh-cmd"><code>${scpTo}</code><button class="copy-btn" onclick="copyText('${scpTo}',this)" title="Copy">⎘</button></div>
      </div>
      <div class="ssh-block">
        <label>SCP — download file</label>
        <div class="ssh-cmd"><code>${scpFrom}</code><button class="copy-btn" onclick="copyText('${scpFrom}',this)" title="Copy">⎘</button></div>
      </div>
      <div class="ssh-block">
        <label>SSH between instances (passwordless)</label>
        <div class="ssh-cmd"><code>${p2p}</code><button class="copy-btn" onclick="copyText('${p2p}',this)" title="Copy">⎘</button></div>
      </div>
      <p class="ssh-note">The CloudCore keypair is pre-installed on every instance at <code>~/.ssh/cloudcore_ed25519</code>. Instances can SSH to each other without a password.</p>
    </div>`;
}

function toggleSSH(instId) {
  const row = document.getElementById(`ssh-row-${instId}`);
  row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}

// ── Instances — Users panel ───────────────────────────────────────────────────
function usersPanel(i) {
  const users = i.users || [];
  const rows = users.length
    ? users.map(u => `
        <tr>
          <td>${u.username}</td>
          <td>${u.sudo ? '<span class="badge badge-running">yes</span>' : '—'}</td>
          <td class="mono text-muted">${u.ssh_keys && u.ssh_keys.length ? u.ssh_keys.length + ' key(s)' : '—'}</td>
          <td><button class="btn btn-danger btn-sm" onclick="removeUser('${i.id}','${u.username}')">Remove</button></td>
        </tr>`).join('')
    : `<tr><td colspan="4" style="color:var(--text-muted);padding:8px">No additional users.</td></tr>`;
  const note = i.status !== 'running'
    ? `<p class="ssh-note">Users added before launch are baked into cloud-init. Users added to a running instance are applied immediately via SSH.</p>`
    : '';
  return `
    <div class="backends-panel">
      <table>
        <thead><tr><th>Username</th><th>Sudo</th><th>SSH Keys</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="backends-add">
        <div class="field"><label>Username</label><input id="u-name-${i.id}" placeholder="alice"></div>
        <div class="field" style="flex:0 0 130px">
          <label>Sudo</label>
          <select id="u-sudo-${i.id}">
            <option value="false">No</option>
            <option value="true">Yes (NOPASSWD)</option>
          </select>
        </div>
        <div class="field">
          <label>SSH Public Key <span class="text-muted">(optional)</span></label>
          <input id="u-key-${i.id}" placeholder="ssh-ed25519 AAAA...">
        </div>
        <button class="btn btn-primary btn-sm" onclick="addUser('${i.id}')">Add User</button>
      </div>
      ${note}
    </div>`;
}

function toggleUsers(instId) {
  const row = document.getElementById(`users-row-${instId}`);
  row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}

async function addUser(instId) {
  const username = document.getElementById(`u-name-${instId}`).value.trim();
  const sudo     = document.getElementById(`u-sudo-${instId}`).value === 'true';
  const key      = document.getElementById(`u-key-${instId}`).value.trim();
  if (!username) { toast('Username is required', 'error'); return; }
  try {
    await api('POST', `/v1/instances/${instId}/users`, {
      username, sudo, ssh_keys: key ? [key] : [],
    });
    toast(`User "${username}" added`, 'success');
    loadInstances();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function removeUser(instId, username) {
  if (!confirm(`Remove user "${username}" from instance?`)) return;
  try {
    await api('DELETE', `/v1/instances/${instId}/users/${username}`);
    toast(`User "${username}" removed`, 'success');
    loadInstances();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

// ── Instances — render ────────────────────────────────────────────────────────
function renderInstances(items) {
  const tbody = document.getElementById('instance-tbody');
  if (!items.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="12">No instances found. Launch one above.</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(i => `
    <tr>
      <td class="cb-col"><input type="checkbox" class="row-cb" data-type="instance" data-id="${i.id}" data-name="${i.name}" onchange="_onRowCbChange('instance')"></td>
      <td><strong>${i.name}</strong></td>
      <td>${shortId(i.id)}</td>
      <td class="mono">${i.image_id}</td>
      <td>${i.flavor}</td>
      <td class="mono">${i.private_ip || '—'}</td>
      <td class="mono">${sshDisplay(i)}</td>
      <td><button class="expand-btn" onclick="toggleSSH('${i.id}')">SSH ▾</button></td>
      <td>
        <button class="expand-btn" onclick="toggleUsers('${i.id}')">
          ${(i.users||[]).length} user${(i.users||[]).length !== 1 ? 's' : ''} ▾
        </button>
      </td>
      <td>${badge(i.status)}</td>
      <td>${fmtDate(i.created_at)}</td>
      <td>
        <button class="expand-btn" onclick="toggleSnapshots('${i.id}')">Snapshots ▾</button>
        ${(i.tags || {}).display === 'vnc' ? `<button class="expand-btn" onclick="openConsole('${i.id}', '${_esc(i.name)}')">Console</button>
        <button class="expand-btn" onclick="openScreenshot('${i.id}')">Screenshot</button>` : ''}
        <button class="btn btn-danger btn-sm" onclick="deleteInstance('${i.id}','${i.name}')">Terminate</button>
      </td>
    </tr>
    <tr id="snap-row-${i.id}" class="backends-row" style="display:none">
      <td colspan="12" id="snap-panel-${i.id}"></td>
    </tr>
    <tr id="ssh-row-${i.id}" class="backends-row" style="display:none">
      <td colspan="12">${sshPanel(i)}</td>
    </tr>
    <tr id="users-row-${i.id}" class="backends-row" style="display:none">
      <td colspan="12">${usersPanel(i)}</td>
    </tr>`).join('');
}

// ── Instances — form ──────────────────────────────────────────────────────────
let _vpcList = [];

function _subnetsForVpc(vpcId) {
  const vpc = _vpcList.find(v => v.id === vpcId);
  if (!vpc || !vpc.cidr_block) return [];
  // Parse x.x.x.x/prefix — derive 4 /24 subnets from the /16 (or similar)
  const [base] = vpc.cidr_block.split('/');
  const parts  = base.split('.').map(Number);
  // Use first two octets, vary third octet 1–4
  return [1, 2, 3, 4].map(n => ({
    id:   `subnet-${parts[0]}-${parts[1]}-${n}-0`,
    label: `${parts[0]}.${parts[1]}.${n}.0/24 (${vpc.name})`,
  }));
}

function _populateSubnets(vpcId) {
  const sel     = document.getElementById('inst-subnet');
  const subnets = _subnetsForVpc(vpcId);
  sel.innerHTML = subnets.length
    ? subnets.map(s => `<option value="${s.id}">${s.label}</option>`).join('')
    : '<option value="">Select a VPC first</option>';
}

async function populateInstanceForm() {
  const imgSel = document.getElementById('inst-image');
  imgSel.innerHTML = '<option value="">Loading…</option>';
  try {
    const data = await api('GET', '/v1/images');
    const available = data.items.filter(img => img.available);
    imgSel.innerHTML = available.length
      ? available.map(img => `<option value="${img.id}">${img.name}</option>`).join('')
      : '<option value="">No images available — run fetch-image.sh</option>';
  } catch {
    imgSel.innerHTML = '<option value="ubuntu-22.04">ubuntu-22.04</option>';
  }
  const vpcSel = document.getElementById('inst-vpc');
  vpcSel.innerHTML = '<option value="">Loading…</option>';
  try {
    const data = await api('GET', '/v1/vpcs');
    _vpcList   = data.items.filter(v => v.status !== 'deleted');
    vpcSel.innerHTML = _vpcList.length
      ? _vpcList.map(v => `<option value="${v.id}">${v.name} (${v.cidr_block})</option>`).join('')
      : '<option value="">No VPCs — create one first</option>';
    vpcSel.onchange = () => _populateSubnets(vpcSel.value);
    _populateSubnets(vpcSel.value);
  } catch {
    vpcSel.innerHTML = '<option value="">Error loading VPCs</option>';
  }
}

function toggleInstanceForm() {
  const form    = document.getElementById('instance-form');
  const visible = form.style.display !== 'none';
  form.style.display = visible ? 'none' : 'block';
  if (!visible) populateInstanceForm();
}

async function createInstance() {
  const name    = document.getElementById('inst-name').value.trim();
  const imageId = document.getElementById('inst-image').value;
  const flavor  = document.getElementById('inst-flavor').value;
  const vpcId   = document.getElementById('inst-vpc').value;
  const subnet  = document.getElementById('inst-subnet').value;
  if (!name)    { toast('Name is required', 'error'); return; }
  if (!imageId) { toast('Image is required', 'error'); return; }
  if (!vpcId)   { toast('VPC is required', 'error'); return; }
  const userData = document.getElementById('inst-userdata').value.trim();
  try {
    await api('POST', '/v1/instances', {
      name, image_id: imageId, flavor, vpc_id: vpcId, subnet_id: subnet,
      user_data: userData || undefined,
      tags: parseTags(document.getElementById('inst-tags').value),
    });
    toast(`Instance "${name}" launching…`, 'success');
    document.getElementById('instance-form').style.display = 'none';
    ['inst-name','inst-tags','inst-userdata'].forEach(id =>
      document.getElementById(id).value = '');
    loadInstances();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function deleteInstance(id, name) {
  if (!confirm(`Terminate instance "${name}"? This cannot be undone.`)) return;
  try {
    await api('DELETE', `/v1/instances/${id}`);
    toast(`Instance "${name}" terminating…`, 'success');
    loadInstances();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

// ── Instances — disk snapshots (lfs-os-Phased-Implementation.md, B1) ─────────
// Checkpoints for long builds: every disk, taken with the VM shut down cleanly
// (and started again), restored by powering off and reverting every disk.
async function toggleSnapshots(id) {
  const row = document.getElementById(`snap-row-${id}`);
  const open = row.style.display === 'none';
  row.style.display = open ? '' : 'none';
  if (open) await loadSnapshots(id);
}

async function loadSnapshots(id) {
  const panel = document.getElementById(`snap-panel-${id}`);
  panel.innerHTML = '<span class="text-muted">Loading snapshots…</span>';
  let items = [];
  try {
    items = (await api('GET', `/v1/instances/${id}/snapshots`)).items || [];
  } catch (e) {
    panel.innerHTML = `<span class="text-muted">Snapshots unavailable: ${_esc(e.message)}</span>`;
    return;
  }
  const rows = items.length ? items.map(s => `
      <tr>
        <td class="mono">${_esc(s.name)}</td>
        <td>${_esc(s.description || '')}</td>
        <td>${fmtDate(s.created_at)}</td>
        <td class="mono">${(s.disks || []).map(_esc).join(', ')}</td>
        <td>
          <button class="btn btn-sm" onclick="restoreSnapshot('${id}', '${_esc(s.name)}')">Restore</button>
          <button class="btn btn-danger btn-sm" onclick="deleteSnapshot('${id}', '${_esc(s.name)}')">Delete</button>
        </td>
      </tr>`).join('') : '<tr class="empty-row"><td colspan="5">No snapshots yet.</td></tr>';
  panel.innerHTML = `
    <div class="table-wrap"><table>
      <thead><tr><th>Name</th><th>Description</th><th>Taken</th><th>Disks</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <div class="form-grid" style="margin-top:8px">
      <input type="text" id="snap-name-${id}" placeholder="name, e.g. chapter-5-done">
      <input type="text" id="snap-desc-${id}" placeholder="description (optional)">
      <button class="btn btn-primary btn-sm" onclick="createSnapshot('${id}')">Take snapshot</button>
    </div>
    <span class="bm-field-hint">A running instance is shut down cleanly for the snapshot and started again (about a minute at most). Deleting a snapshot needs the instance stopped.</span>`;
}

async function createSnapshot(id) {
  const name = document.getElementById(`snap-name-${id}`).value.trim();
  const description = document.getElementById(`snap-desc-${id}`).value.trim();
  if (!name) { toast('Give the snapshot a name', 'error'); return; }
  toast(`Taking snapshot "${name}" (the instance restarts)…`, 'info');
  try {
    const s = await api('POST', `/v1/instances/${id}/snapshots`, { name, description });
    toast(`Snapshot "${s.name}" taken in ${s.seconds}s`, 'success');
    loadSnapshots(id);
  } catch (e) { toast(`Snapshot failed: ${e.message}`, 'error'); }
}

async function restoreSnapshot(id, name) {
  if (!confirm(`Restore snapshot "${name}"? Everything on this instance since it was taken is lost.`)) return;
  try {
    const r = await api('POST', `/v1/instances/${id}/snapshots/${encodeURIComponent(name)}/restore`, {});
    toast(`Restored "${name}"${r.running ? '; the instance is starting' : ''}`, 'success');
    loadInstances();
  } catch (e) { toast(`Restore failed: ${e.message}`, 'error'); }
}

async function deleteSnapshot(id, name) {
  if (!confirm(`Delete snapshot "${name}"? This cannot be undone.`)) return;
  try {
    await api('DELETE', `/v1/instances/${id}/snapshots/${encodeURIComponent(name)}`);
    toast(`Snapshot "${name}" deleted`, 'success');
    loadSnapshots(id);
  } catch (e) { toast(`Delete failed: ${e.message}`, 'error'); }
}

// ── Instances — graphical console (lfs-os-Phased-Implementation.md, B3) ──────
// noVNC (vendored, MPL-2.0) talks RFB over a WebSocket the API bridges to the
// instance's VNC display on the host's loopback. Browsers can't send our auth
// header on a WebSocket, so a one-time, 60-second ticket stands in for it.
let _consoleRfb = null;

async function openConsole(id, name) {
  closeConsole();
  let ticket;
  try {
    ticket = await api('POST', `/v1/instances/${id}/vnc-ticket`);
  } catch (e) { toast(`Console unavailable: ${e.message}`, 'error'); return; }
  const overlay = document.createElement('div');
  overlay.id = 'console-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.85);display:flex;flex-direction:column';
  overlay.innerHTML = `
    <div style="display:flex;gap:8px;align-items:center;padding:8px 12px;color:#ddd">
      <strong>Console: ${_esc(name)}</strong>
      <span id="console-status" class="text-muted">connecting…</span>
      <span style="flex:1"></span>
      <button class="btn btn-sm" onclick="_consoleRfb && _consoleRfb.sendCtrlAltDel()">Ctrl-Alt-Del</button>
      <button class="btn btn-sm" onclick="openScreenshot('${id}')">Screenshot</button>
      <button class="btn btn-danger btn-sm" onclick="closeConsole()">Close</button>
    </div>
    <div id="console-screen" style="flex:1;min-height:0"></div>`;
  document.body.appendChild(overlay);
  try {
    const { default: RFB } = await import('/vendor/novnc-1.7.0/core/rfb.js');
    const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${ticket.path}`;
    _consoleRfb = new RFB(document.getElementById('console-screen'), url, { wsProtocols: ['binary'] });
    _consoleRfb.scaleViewport = true;
    _consoleRfb.addEventListener('connect', () => { document.getElementById('console-status').textContent = 'connected'; });
    _consoleRfb.addEventListener('disconnect', e => {
      const s = document.getElementById('console-status');
      if (s) s.textContent = e.detail.clean ? 'disconnected' : 'connection lost';
    });
  } catch (e) {
    document.getElementById('console-status').textContent = `failed: ${e.message}`;
  }
}

function closeConsole() {
  if (_consoleRfb) { try { _consoleRfb.disconnect(); } catch (e) { /* already closed */ } _consoleRfb = null; }
  const o = document.getElementById('console-overlay');
  if (o) o.remove();
}

async function openScreenshot(id) {
  try {
    const resp = await fetch(`/v1/instances/${id}/screenshot`, { headers: { Authorization: `Bearer ${API_TOKEN}` } });
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
    window.open(URL.createObjectURL(await resp.blob()), '_blank');
  } catch (e) { toast(`Screenshot failed: ${e.message}`, 'error'); }
}

