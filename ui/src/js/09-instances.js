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
  if (i.status !== 'running' || !i.ssh_port) {
    return `<div class="ssh-panel"><span class="text-muted" style="font-size:13px">SSH available once instance is running.</span></div>`;
  }
  const user    = i.ssh_user || 'ubuntu';
  const host    = '127.0.0.1';
  const port    = i.ssh_port;
  const keyPath = '~/.local/share/cloudcore/cloudcore_ed25519';
  const sshCmd  = `ssh -i ${keyPath} -p ${port} ${user}@${host}`;
  const scpTo   = `scp -i ${keyPath} -P ${port} <local-file> ${user}@${host}:<remote-path>`;
  const scpFrom = `scp -i ${keyPath} -P ${port} ${user}@${host}:<remote-path> <local-dest>`;
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
      <td class="mono">${i.ssh_port ? '127.0.0.1:' + i.ssh_port : '—'}</td>
      <td><button class="expand-btn" onclick="toggleSSH('${i.id}')">SSH ▾</button></td>
      <td>
        <button class="expand-btn" onclick="toggleUsers('${i.id}')">
          ${(i.users||[]).length} user${(i.users||[]).length !== 1 ? 's' : ''} ▾
        </button>
      </td>
      <td>${badge(i.status)}</td>
      <td>${fmtDate(i.created_at)}</td>
      <td><button class="btn btn-danger btn-sm" onclick="deleteInstance('${i.id}','${i.name}')">Terminate</button></td>
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
