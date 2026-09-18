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

// A variable named exactly worker_peers (examples/distributed-llm and
// examples/llm-chat's own list(object({peer_id, peer_vpc_id,
// peer_subnet_id, peer_security_group_id})) variable) gets the same
// checkbox-picker treatment the Scheduler's own worker-peer-pool field
// already has (28-scheduler.js) — per direct request: "for llm chat
// set the worker peers to be a pick list as well as being able to add
// one free text." Exact-name match, not a suffix pattern like
// peer_id — this is a different shape entirely (a JSON array, not a
// single string), so it needs its own special case rather than fitting
// the existing _BM_PEER_ID_RE family logic.
const _BM_WORKER_PEERS_VAR = 'worker_peers';

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
  let recommendation = null;
  const hasWorkerPeers = editable.some(([key]) => key === _BM_WORKER_PEERS_VAR);
  if (editable.some(([key]) => _BM_PEER_ID_RE.test(key)) || hasWorkerPeers) {
    try {
      const peerData = await api('GET', '/v1/peers?status=approved');
      approvedPeers = peerData.items;
    } catch (e) { /* fall through — the field just renders with no options */ }
    // Best-effort: a failed recommendation fetch just means every
    // peer_id field falls back to its old default of "local, blank" —
    // never blocks rendering the rest of the form over it.
    try {
      recommendation = await api('GET', '/v1/peers/recommend-placement');
    } catch (e) { /* fall through */ }
  }
  let workerPeersVerdicts = {};
  if (hasWorkerPeers && recommendation) {
    (recommendation.hosts || []).forEach(h => { if (h.peer_id) workerPeersVerdicts[h.peer_id] = h.verdict; });
  }

  const peerFamilies = _bmPeerFamilies(editable);
  const cascadeTargetKeys = new Set();
  Object.values(peerFamilies).forEach(f => {
    if (f.vpcKey) cascadeTargetKeys.add(f.vpcKey);
    if (f.subnetKey) cascadeTargetKeys.add(f.subnetKey);
    if (f.sgKey) cascadeTargetKeys.add(f.sgKey);
  });

  container.innerHTML = editable.map(([key, meta]) => {
    if (key === _BM_WORKER_PEERS_VAR) return _bmRenderWorkerPeersField(key, approvedPeers, workerPeersVerdicts);
    if (_BM_PEER_ID_RE.test(key)) {
      // Never auto-recommend a standalone _peer_id field when this same
      // form also has a worker_peers list (llm-chat's own
      // coordinator_peer_id today, and any future template shaped the
      // same way) — recommend-placement has no idea worker_peers exists
      // and would happily suggest the very peer a worker checkbox is
      // about to auto-check, which the server then rejects (same peer
      // hosting both roles defeats the reason this template splits
      // across hosts via RPC). Matches this field's own Terraform
      // variable comment: "Deliberately not auto-selected... a human
      // choosing via the dashboard is this pass's actual mechanism."
      const rec = (!hasWorkerPeers && recommendation) ? recommendation.recommended : null;
      const recId = rec ? (rec.peer_id || '') : null;
      const options = approvedPeers.length
        ? approvedPeers.map(p => `<option value="${p.id}"${p.id === recId ? ' selected' : ''}>${_esc(p.hostname)} (${badge(p.wg_tunnel_status)})</option>`).join('')
        : '';
      return `
        <div class="field">
          <label>${key.replace(/_/g, ' ')}</label>
          <select id="bm-var-${key}" data-key="${key}">
            <option value=""${recId === '' ? ' selected' : ''}>— local (this host) —</option>
            ${options}
          </select>
          ${rec && approvedPeers.length ? `<span class="bm-field-hint">Auto-selected: ${_esc(rec.hostname)} (${badge(rec.verdict)} on the Capacity traffic light) — change it if you'd rather place this yourself.</span>` : ''}
          ${hasWorkerPeers && approvedPeers.length ? `<span class="bm-field-hint">Not auto-selected — must be a different peer than any worker below, so this always starts local. Pick one deliberately if you want it peer-placed.</span>` : ''}
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

  await _bmWirePeerCascades(peerFamilies);
  if (hasWorkerPeers) await _bmWireWorkerPeersField(_BM_WORKER_PEERS_VAR);
}

// Checkbox picker (one row per approved peer, pre-checked by its
// current traffic-light verdict — green starts checked, amber/red
// shown but left unchecked, same convention 28-scheduler.js's own
// worker-peer-pool picker already established) plus one manual
// free-text entry for a peer that isn't in the approved list yet, or
// to override the auto-resolved vpc/subnet/sg. The real value lives in
// a hidden input (id bm-var-worker_peers) so bmSubmitBuild's existing
// generic input[data-key] collection loop picks it up unchanged — see
// that function's own JSON.parse() of this one field for why a hidden
// input holding JSON-as-a-string is the bridge back to a real array.
function _bmRenderWorkerPeersField(key, peers, verdicts) {
  const rows = peers.length ? peers.map(p => {
    const verdict = verdicts[p.id];
    const checked = verdict === 'active' ? 'checked' : '';
    const badgeHtml = verdict && typeof _PLACEMENT_VERDICTS !== 'undefined' && _PLACEMENT_VERDICTS[verdict]
      ? `<span class="badge badge-${verdict}">${_PLACEMENT_VERDICTS[verdict].dot} ${_PLACEMENT_VERDICTS[verdict].label}</span>`
      : '<span class="badge">⚪ Unknown</span>';
    return `<tr>
      <td><input type="checkbox" class="bm-worker-peer-cb" value="${p.id}" data-hostname="${_esc(p.hostname)}" ${checked}></td>
      <td>${_esc(p.hostname)}</td>
      <td>${badgeHtml}</td>
    </tr>`;
  }).join('') : '<tr class="empty-row"><td colspan="3">No approved peers — pair with one on the Peers page first.</td></tr>';

  return `
    <div class="field">
      <label>${key.replace(/_/g, ' ')} <span class="bm-required">*</span></label>
      <div class="table-wrap">
        <table>
          <thead><tr><th></th><th>Peer</th><th>Placement</th></tr></thead>
          <tbody id="bm-worker-peers-tbody">${rows}</tbody>
        </table>
      </div>
      <div class="bm-worker-manual" style="margin-top:8px">
        <label style="font-size:12px">Add one manual entry (a peer not listed above, or override its ids)</label>
        <div class="form-grid">
          <input type="text" id="bm-worker-manual-peer_id" placeholder="peer_id">
          <input type="text" id="bm-worker-manual-peer_vpc_id" placeholder="peer_vpc_id">
          <input type="text" id="bm-worker-manual-peer_subnet_id" placeholder="peer_subnet_id">
          <input type="text" id="bm-worker-manual-peer_security_group_id" placeholder="peer_security_group_id">
        </div>
      </div>
      <input type="hidden" id="bm-var-${key}" data-key="${key}" data-required="1" value="">
    </div>`;
}

async function _bmWireWorkerPeersField(key) {
  document.querySelectorAll('.bm-worker-peer-cb').forEach(cb => cb.addEventListener('change', () => _bmUpdateWorkerPeersValue(key)));
  ['peer_id', 'peer_vpc_id', 'peer_subnet_id', 'peer_security_group_id'].forEach(field => {
    const el = document.getElementById(`bm-worker-manual-${field}`);
    if (el) el.addEventListener('change', () => _bmUpdateWorkerPeersValue(key));
  });
  await _bmUpdateWorkerPeersValue(key);
}

// Resolves one peer's own vpc/subnet/security-group (first match — the
// same "auto-select the first/only entry" convention _bmCascadeFromPeer
// already uses below for a single peer_id field) so a checked worker
// peer needs no further manual lookup.
async function _bmResolvePeerPlacement(peerId) {
  const [vpcData, sgData] = await Promise.all([
    api('GET', `/v1/peers/${peerId}/vpcs`),
    api('GET', `/v1/peers/${peerId}/security-groups`),
  ]);
  const vpc = (vpcData.items || [])[0];
  if (!vpc) return null;
  const subnetData = await api('GET', `/v1/peers/${peerId}/subnets?vpc_id=${encodeURIComponent(vpc.id)}`);
  const subnet = (subnetData.items || [])[0];
  const sg = (sgData.items || []).find(s => s.vpc_id === vpc.id);
  if (!subnet || !sg) return null;
  return { peer_id: peerId, peer_vpc_id: vpc.id, peer_subnet_id: subnet.id, peer_security_group_id: sg.id };
}

async function _bmUpdateWorkerPeersValue(key) {
  const hidden = document.getElementById(`bm-var-${key}`);
  if (!hidden) return;
  const checked = Array.from(document.querySelectorAll('.bm-worker-peer-cb:checked')).map(cb => cb.value);
  let entries = (await Promise.all(checked.map(_bmResolvePeerPlacement))).filter(Boolean);

  const manual = {
    peer_id: (document.getElementById('bm-worker-manual-peer_id') || {}).value?.trim(),
    peer_vpc_id: (document.getElementById('bm-worker-manual-peer_vpc_id') || {}).value?.trim(),
    peer_subnet_id: (document.getElementById('bm-worker-manual-peer_subnet_id') || {}).value?.trim(),
    peer_security_group_id: (document.getElementById('bm-worker-manual-peer_security_group_id') || {}).value?.trim(),
  };
  const manualFilled = Object.values(manual).filter(Boolean).length;
  // All 4 or none — a partial manual row would otherwise be silently
  // dropped, which reads as "it worked" when it didn't.
  hidden.dataset.manualPartial = (manualFilled > 0 && manualFilled < 4) ? '1' : '0';
  if (manualFilled === 4) entries.push(manual);

  hidden.value = entries.length ? JSON.stringify(entries) : '';
}

async function _bmWirePeerCascades(families) {
  for (const [peerKey, f] of Object.entries(families)) {
    if (!f.vpcKey && !f.subnetKey && !f.sgKey) continue;
    const peerSel = document.getElementById(`bm-var-${peerKey}`);
    if (!peerSel) continue;
    peerSel.addEventListener('change', () => _bmCascadeFromPeer(peerKey, f));
    // A recommendation may have pre-selected a real peer above (not the
    // "local" blank default) — cascade its vpc/subnet/sg immediately,
    // same as a user's own manual selection would, rather than leaving
    // those fields stuck on "select a peer first" despite a peer
    // already being chosen.
    if (peerSel.value) await _bmCascadeFromPeer(peerKey, f);
  }
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

  const manualPartial = document.querySelector('#bm-var-fields [data-manual-partial="1"]');
  if (manualPartial) {
    toast('Fill in all four manual worker_peers fields, or none of them.', 'error');
    return;
  }

  const vars = {};
  document.querySelectorAll('#bm-var-fields input[data-key], #bm-var-fields select[data-key]').forEach(el => {
    if (el.value.trim()) vars[el.dataset.key] = el.value.trim();
  });
  if (vars[_BM_WORKER_PEERS_VAR]) vars[_BM_WORKER_PEERS_VAR] = JSON.parse(vars[_BM_WORKER_PEERS_VAR]);

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
