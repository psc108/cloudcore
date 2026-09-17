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
  await _bmRenderVarForm(filename, tpl, data.vars || {});
  document.getElementById('bm-var-panel').style.display = 'block';
  document.getElementById('bm-var-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// A variable named (or ending in) peer_id gets a real picker instead of a
// plain text box — "in the template, be able to select from the agents
// found" is what this delivers. Everything else stays a generic input;
// this is the one deliberate special case.
const _BM_PEER_ID_RE = /(^|_)peer_id$/i;

// A peer-placed resource's own peer_vpc_id/peer_subnet_id/
// peer_security_group_id also get real pickers, cascading from their
// sibling peer_id field — "checking the current vpc id's on the
// localhost and connected peers" rather than expecting a user to
// already know an id that exists on a host they've never directly
// browsed. Same key-prefix convention as peer_id itself (bare or
// slot-prefixed, e.g. peer_vpc_id / web_02_peer_vpc_id) — matched by
// substituting the suffix onto each peer_id key's own prefix (below),
// not a separate standalone regex, since a vpc/subnet/sg field only
// ever makes sense paired with its own peer_id sibling.

// Groups each peer_id key with whichever of its own vpc/subnet/sg
// siblings this schema actually declares (04-load-balanced-web.yml's
// web_02 family, for instance, has no sg var at all — it never attaches
// one) — computed by substituting each suffix onto the peer_id key's own
// prefix, not a fixed name, so this works for both bare (peer_id) and
// slot-prefixed (web_02_peer_id) templates alike.
function _bmPeerFamilies(editable) {
  const keys = new Set(editable.map(([k]) => k));
  const families = {};
  for (const [key] of editable) {
    if (!_BM_PEER_ID_RE.test(key)) continue;
    const vpcKey = key.replace(/peer_id$/i, 'peer_vpc_id');
    const subnetKey = key.replace(/peer_id$/i, 'peer_subnet_id');
    const sgKey = key.replace(/peer_id$/i, 'peer_security_group_id');
    families[key] = {
      vpcKey: keys.has(vpcKey) ? vpcKey : null,
      subnetKey: keys.has(subnetKey) ? subnetKey : null,
      sgKey: keys.has(sgKey) ? sgKey : null,
    };
  }
  return families;
}

async function _bmRenderVarForm(filename, tpl, schema) {
  document.getElementById('bm-form-title').textContent = tpl ? tpl.title : filename;
  document.getElementById('bm-submit-filename').value = filename;

  const container = document.getElementById('bm-var-fields');
  const editable = Object.entries(schema).filter(([, v]) => !v.derived);

  if (!editable.length) {
    container.innerHTML = '<div class="bm-empty" style="padding:12px 0">No variables to configure — uses defaults.</div>';
    return;
  }

  let approvedPeers = [];
  if (editable.some(([key]) => _BM_PEER_ID_RE.test(key))) {
    try {
      const peerData = await api('GET', '/v1/peers?status=approved');
      approvedPeers = peerData.items;
    } catch (e) { /* fall through — the field just renders with no options */ }
  }

  const peerFamilies = _bmPeerFamilies(editable);
  const cascadeTargetKeys = new Set();
  Object.values(peerFamilies).forEach(f => {
    if (f.vpcKey) cascadeTargetKeys.add(f.vpcKey);
    if (f.subnetKey) cascadeTargetKeys.add(f.subnetKey);
    if (f.sgKey) cascadeTargetKeys.add(f.sgKey);
  });

  container.innerHTML = editable.map(([key, meta]) => {
    if (_BM_PEER_ID_RE.test(key)) {
      const options = approvedPeers.length
        ? approvedPeers.map(p => `<option value="${p.id}">${_esc(p.hostname)} (${badge(p.wg_tunnel_status)})</option>`).join('')
        : '';
      return `
        <div class="field">
          <label>${key.replace(/_/g, ' ')}</label>
          <select id="bm-var-${key}" data-key="${key}">
            <option value="">— local (this host) —</option>
            ${options}
          </select>
          ${!approvedPeers.length ? '<span class="bm-field-hint">No paired peers yet — see the Peers section.</span>' : ''}
        </div>`;
    }
    if (cascadeTargetKeys.has(key)) {
      return `
        <div class="field">
          <label>${key.replace(/_/g, ' ')}</label>
          <select id="bm-var-${key}" data-key="${key}" disabled>
            <option value="">— select a peer first —</option>
          </select>
        </div>`;
    }
    return `
    <div class="field">
      <label>${key.replace(/_/g, ' ')}</label>
      <input type="${key.includes('token') ? 'password' : 'text'}"
             id="bm-var-${key}"
             data-key="${key}"
             value="${_esc(String(meta.default ?? ''))}">
    </div>
  `;
  }).join('');

  _bmWirePeerCascades(peerFamilies);
}

function _bmWirePeerCascades(families) {
  Object.entries(families).forEach(([peerKey, f]) => {
    if (!f.vpcKey && !f.subnetKey && !f.sgKey) return;
    const peerSel = document.getElementById(`bm-var-${peerKey}`);
    if (peerSel) peerSel.addEventListener('change', () => _bmCascadeFromPeer(peerKey, f));
  });
}

// Fetches the selected peer's own vpcs (+ security groups, unfiltered —
// there's no server-side vpc filter for those, so they're filtered
// client-side per vpc below) and populates the sibling vpc_id select,
// auto-selecting its first entry (most labs have exactly one) rather
// than leaving the user to guess an id — then cascades into subnet/sg.
async function _bmCascadeFromPeer(peerKey, f) {
  const peerId = document.getElementById(`bm-var-${peerKey}`).value;
  const vpcSel = f.vpcKey && document.getElementById(`bm-var-${f.vpcKey}`);
  const subnetSel = f.subnetKey && document.getElementById(`bm-var-${f.subnetKey}`);
  const sgSel = f.sgKey && document.getElementById(`bm-var-${f.sgKey}`);

  if (!peerId) {
    [vpcSel, subnetSel, sgSel].forEach(sel => {
      if (!sel) return;
      sel.innerHTML = '<option value="">— select a peer first —</option>';
      sel.disabled = true;
    });
    return;
  }

  if (vpcSel) { vpcSel.innerHTML = '<option value="">Loading…</option>'; vpcSel.disabled = true; }
  if (subnetSel) { subnetSel.innerHTML = '<option value="">— select a VPC first —</option>'; subnetSel.disabled = true; }
  if (sgSel) { sgSel.innerHTML = '<option value="">Loading…</option>'; sgSel.disabled = true; }

  let vpcs = [], sgs = [];
  try {
    const [vpcData, sgData] = await Promise.all([
      api('GET', `/v1/peers/${peerId}/vpcs`),
      sgSel ? api('GET', `/v1/peers/${peerId}/security-groups`) : Promise.resolve({ items: [] }),
    ]);
    vpcs = vpcData.items || [];
    sgs = sgData.items || [];
  } catch (e) { /* fall through — fields render with a "not found" hint below */ }

  if (vpcSel) {
    vpcSel.innerHTML = vpcs.length
      ? vpcs.map(v => `<option value="${v.id}">${_esc(v.name)} (${_esc(v.cidr_block)})</option>`).join('')
      : '<option value="">No VPCs found on this peer</option>';
    vpcSel.disabled = !vpcs.length;
    vpcSel.dataset.sgs = JSON.stringify(sgs);
    vpcSel.onchange = () => _bmCascadeFromVpc(vpcSel, subnetSel, sgSel, peerId);
    if (vpcs.length) await _bmCascadeFromVpc(vpcSel, subnetSel, sgSel, peerId);
  } else if (sgSel) {
    // No vpc field in this family — populate sg directly, unfiltered.
    sgSel.innerHTML = sgs.length
      ? sgs.map(s => `<option value="${s.id}">${_esc(s.name)}</option>`).join('')
      : '<option value="">No security groups found on this peer</option>';
    sgSel.disabled = !sgs.length;
  }
}

async function _bmCascadeFromVpc(vpcSel, subnetSel, sgSel, peerId) {
  const vpcId = vpcSel.value;
  const sgs = JSON.parse(vpcSel.dataset.sgs || '[]');

  if (subnetSel) {
    subnetSel.innerHTML = '<option value="">Loading…</option>';
    subnetSel.disabled = true;
    try {
      const subnetData = await api('GET', `/v1/peers/${peerId}/subnets?vpc_id=${encodeURIComponent(vpcId)}`);
      const subnets = subnetData.items || [];
      subnetSel.innerHTML = subnets.length
        ? subnets.map(s => `<option value="${s.id}">${_esc(s.name)} (${_esc(s.cidr_block)})</option>`).join('')
        : '<option value="">No subnets found in this VPC</option>';
      subnetSel.disabled = !subnets.length;
    } catch (e) {
      subnetSel.innerHTML = '<option value="">Failed to load subnets</option>';
    }
  }

  if (sgSel) {
    const vpcSgs = sgs.filter(s => s.vpc_id === vpcId);
    sgSel.innerHTML = vpcSgs.length
      ? vpcSgs.map(s => `<option value="${s.id}">${_esc(s.name)}</option>`).join('')
      : '<option value="">No security groups found in this VPC</option>';
    sgSel.disabled = !vpcSgs.length;
  }
}

async function bmSubmitBuild() {
  const filename = document.getElementById('bm-submit-filename').value;
  if (!filename) { toast('Select a template first', 'error'); return; }

  const vars = {};
  document.querySelectorAll('#bm-var-fields input[data-key], #bm-var-fields select[data-key]').forEach(el => {
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
      <td style="display:flex;gap:4px">
        <button class="btn btn-ghost btn-sm" onclick="bmViewLog('${b.id}')">Log</button>
        ${canDestroy ? `<button class="btn btn-danger btn-sm" onclick="bmDestroySingle('${b.id}','${b.template}')">Destroy</button>` : ''}
      </td>
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

async function bmDestroySingle(buildId, template) {
  if (!confirm(`Destroy all resources from build ${buildId.slice(0,8)}… (${template})?\nThis cannot be undone.`)) return;
  try {
    await api('DELETE', `/v1/builds/${buildId}`);
    toast('Resources destroyed', 'success');
  } catch (e) {
    toast(`Destroy failed: ${e.message}`, 'error');
  }
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
