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

// Same suffix match as the Ansible build manager's own peer picker
// (16-build-manager.js's _BM_PEER_ID_RE) — kept as two separate
// constants since these are two independent renderers, not shared
// code, but deliberately the same matching rule so a template author
// gets identical behavior regardless of which build system they used.
const _TF_PEER_ID_RE = /(^|_)peer_id$/i;

// Same exact-name match as the Ansible build manager's own worker_peers
// picker (16-build-manager.js's _BM_WORKER_PEERS_VAR) — per direct
// request: "for llm chat set the worker peers to be a pick list as
// well as being able to add one free text." Kept as its own constant
// (not shared code) for the same reason _TF_PEER_ID_RE is duplicated
// above rather than imported.
const _TF_WORKER_PEERS_VAR = 'worker_peers';

// Per direct request to auto-select "any machine, not locked to one
// specific" for the coordinator, using real capacity (RAM, vCPU, disk)
// rather than just current load -- coordinator_flavor gets recommended
// together with coordinator_peer_id, both from the same
// recommend-placement call, now flavor-aware (api/peers_routes.py's
// own ?flavor_candidates param, api/capacity_gate.py's best_fit()).
// Largest first: the server tries each in order and returns whichever
// one some real candidate actually affords, which is what implements
// "use it, or as close to it as it can be" without hardcoding one
// fixed target. Name-specific (not a generic pattern like
// _TF_PEER_ID_RE) since only llm-chat's own coordinator needs this
// today -- same style as _TF_DYNAMIC_LAYERS_VAR/_TF_HTTP_PORT_VAR
// above, both equally one-off special cases.
const _TF_COORDINATOR_PEER_ID_VAR = 'coordinator_peer_id';
const _TF_FLAVOR_VAR = 'coordinator_flavor';
const _TF_FLAVOR_CANDIDATES_DESC = ['standard.2xlarge', 'standard.xlarge', 'standard.large'];

// Found live: this field's own static Terraform default (24, llm-chat's
// only consumer today) was being pre-filled into the input and
// submitted on every single build regardless of whether a real person
// ever touched it — silently defeating api/layer_split.py's own dynamic,
// safety-clamped computation, which only ever fills this in when the
// caller left it genuinely blank. Caused a real OOM crash loop: 24
// layers on a 4096MB standard.large coordinator is ~4.3GB of weights
// alone. Rendered specially (empty value, explanatory placeholder) so
// a normal "just build it" flow actually gets the safe, real-time
// computed split instead of every UI-submitted build silently reverting
// to the unsafe static fallback that variable's own description already
// warns is "only a static fallback for a direct tofu apply."
const _TF_DYNAMIC_LAYERS_VAR = 'rpc_offload_layers';

// Any template exposing http_port (today: distributed-llm, llm-chat —
// both real load-balanced HTTP services expensive to cold-start) also
// gets an idle-timeout auto-shutdown control, per direct request:
// "it takes ~4-8 minutes to get the llm ready... make it a 60 minute
// default but allow the user to choose in 30 minute increments." Not
// a template variable — a build-submission option sent alongside vars,
// so it's rendered and collected separately from the editable.map()
// loop below.
const _TF_HTTP_PORT_VAR = 'http_port';
const _TF_IDLE_OPTIONS_MIN = [30, 60, 90, 120, 180, 240];

function _tfIdleLabel(m) {
  if (m < 60) return `${m} minutes`;
  const h = m / 60;
  return Number.isInteger(h) ? `${h} hour${h > 1 ? 's' : ''}` : `${h} hours`;
}

// A peer-placed resource's own peer_vpc_id/peer_subnet_id/
// peer_security_group_id also get real pickers, cascading from their
// sibling peer_id field — "checking the current vpc id's on the
// localhost and connected peers" rather than expecting a user to
// already know an id that exists on a host they've never directly
// browsed. Matched by substituting each suffix onto a peer_id key's own
// prefix (below), the same convention 16-build-manager.js's own
// _bmPeerFamilies uses, not a shared function — see that file's own
// comment for why these two renderers stay independent.
function _tfPeerFamilies(editable) {
  const keys = new Set(editable.map(([k]) => k));
  const families = {};
  for (const [key] of editable) {
    if (!_TF_PEER_ID_RE.test(key)) continue;
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

  let approvedPeers = [];
  let recommendation = null;
  const hasWorkerPeers = editable.some(([key]) => key === _TF_WORKER_PEERS_VAR);
  const hasFlavorVar = editable.some(([key]) => key === _TF_FLAVOR_VAR);
  if (editable.some(([key]) => _TF_PEER_ID_RE.test(key)) || hasWorkerPeers) {
    try {
      const peerData = await api('GET', '/v1/peers?status=approved');
      approvedPeers = peerData.items || [];
    } catch (e) { /* field just renders with no options */ }
    try {
      const flavorQuery = hasFlavorVar
        ? `?flavor_candidates=${_TF_FLAVOR_CANDIDATES_DESC.join(',')}` : '';
      recommendation = await api('GET', `/v1/peers/recommend-placement${flavorQuery}`);
    } catch (e) { /* fall through — fields fall back to the old local default */ }
  }
  let workerPeersVerdicts = {};
  if (hasWorkerPeers && recommendation) {
    (recommendation.hosts || []).forEach(h => { if (h.peer_id) workerPeersVerdicts[h.peer_id] = h.verdict; });
  }

  // The coordinator can't land on a peer a worker checkbox is about to
  // auto-claim -- recommend-placement has no idea worker_peers exists
  // at all, it just ranks hosts in isolation. Re-rank locally among
  // whatever's left using the per-host best_flavor annotations the
  // server already computed (from real RAM/vCPU/disk affordability,
  // not just current load), rather than duplicating that math here.
  // One-shot, computed from this same initial fetch -- matches every
  // other recommendation on this form already being a prefill, not a
  // live binding that reacts to later checkbox changes.
  let coordinatorPick = null;
  if (hasFlavorVar && recommendation) {
    const claimedByWorker = new Set(
      Object.entries(workerPeersVerdicts).filter(([, v]) => v === 'active').map(([id]) => id)
    );
    const eligible = (recommendation.hosts || []).filter(h =>
      h.best_flavor && !(h.peer_id && claimedByWorker.has(h.peer_id))
    );
    const severity = { active: 0, pending: 1, error: 2 };
    coordinatorPick = eligible.reduce((best, h) => {
      if (!best) return h;
      const rank = _TF_FLAVOR_CANDIDATES_DESC.indexOf(h.best_flavor);
      const bestRank = _TF_FLAVOR_CANDIDATES_DESC.indexOf(best.best_flavor);
      if (rank !== bestRank) return rank < bestRank ? h : best;
      return (severity[h.verdict] ?? 9) < (severity[best.verdict] ?? 9) ? h : best;
    }, null);
  }

  const peerFamilies = _tfPeerFamilies(editable);
  const cascadeTargetKeys = new Set();
  Object.values(peerFamilies).forEach(f => {
    if (f.vpcKey) cascadeTargetKeys.add(f.vpcKey);
    if (f.subnetKey) cascadeTargetKeys.add(f.subnetKey);
    if (f.sgKey) cascadeTargetKeys.add(f.sgKey);
  });

  container.innerHTML = editable.map(([key, meta]) => {
    if (key === _TF_USB_DEVICE_VAR) return _tfRenderUsbField(key, meta, usbDevices);
    if (key === _TF_WORKER_PEERS_VAR) return _tfRenderWorkerPeersField(key, approvedPeers, workerPeersVerdicts);
    if (key === _TF_COORDINATOR_PEER_ID_VAR && hasFlavorVar) {
      // Bypasses _tfRenderPeerField's own !hasWorkerPeers guard
      // (passing hasWorkerPeers=false) -- that guard exists because
      // the plain recommend-placement response has no idea which peer
      // a worker checkbox is about to auto-claim, but coordinatorPick
      // above already excluded those, so it's always safe to show
      // here. Wrapped to match the {recommended: {...}} shape that
      // function already expects from the un-flavor-aware endpoint.
      return _tfRenderPeerField(key, meta, approvedPeers,
        coordinatorPick ? { recommended: coordinatorPick } : null, false);
    }
    if (_TF_PEER_ID_RE.test(key)) return _tfRenderPeerField(key, meta, approvedPeers, recommendation, hasWorkerPeers);
    if (key === _TF_FLAVOR_VAR) return _tfRenderFlavorField(key, meta, coordinatorPick);
    if (cascadeTargetKeys.has(key)) return _tfRenderPeerCascadeField(key, meta);
    if (key === _TF_DYNAMIC_LAYERS_VAR) {
      return `
        <div class="field">
          <label>${key.replace(/_/g, ' ')}</label>
          <input type="text" id="tf-var-${key}" data-key="${key}" data-required="0"
                 placeholder="Auto-computed from real-time peer/host capacity — leave blank">
          <span class="bm-field-hint">Leave blank (recommended) so this is computed fresh at build time from each participant's actual current CPU/RAM, safety-clamped to what really fits. Only set this yourself to override that — e.g. ${_esc(String(meta.default ?? ''))} for a direct <code>tofu apply</code> with no API/dynamic computation available.</span>
        </div>
      `;
    }
    if (meta.type === 'bool') return _tfRenderBoolField(key, meta);
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

  if (editable.some(([key]) => key === _TF_HTTP_PORT_VAR)) {
    container.innerHTML += `
      <div class="field">
        <label>Idle Timeout</label>
        <select id="tf-idle-timeout">
          <option value="">Off — destroy manually</option>
          ${_TF_IDLE_OPTIONS_MIN.map(m =>
            `<option value="${m}"${m === 60 ? ' selected' : ''}>${_tfIdleLabel(m)}</option>`).join('')}
        </select>
        <span class="bm-field-hint">Automatically destroys this build once nothing has actually talked to it for this long — the coordinator itself stays running, ready instantly, for as long as it's actually being used.</span>
      </div>`;
  }

  await _tfWirePeerCascades(peerFamilies);
  if (hasWorkerPeers) await _tfWireWorkerPeersField(_TF_WORKER_PEERS_VAR);
}

// Checkbox picker (one row per approved peer, pre-checked by its
// current traffic-light verdict) plus one manual free-text entry for a
// peer that isn't in the approved list yet, or to override the
// auto-resolved vpc/subnet/sg — same shape as
// 16-build-manager.js's own _bmRenderWorkerPeersField, independently
// implemented for the same reason every other peer-picker pair in
// these two files is. The real value lives in a hidden input (id
// tf-var-worker_peers) so tfSubmitBuild's existing generic
// input[data-key] collection loop picks it up unchanged — see that
// function's own JSON.parse() of this one field.
function _tfRenderWorkerPeersField(key, peers, verdicts) {
  const rows = peers.length ? peers.map(p => {
    const verdict = verdicts[p.id];
    const checked = verdict === 'active' ? 'checked' : '';
    const badgeHtml = verdict && typeof _PLACEMENT_VERDICTS !== 'undefined' && _PLACEMENT_VERDICTS[verdict]
      ? `<span class="badge badge-${verdict}">${_PLACEMENT_VERDICTS[verdict].dot} ${_PLACEMENT_VERDICTS[verdict].label}</span>`
      : '<span class="badge">⚪ Unknown</span>';
    return `<tr>
      <td><input type="checkbox" class="tf-worker-peer-cb" value="${p.id}" data-hostname="${_esc(p.hostname)}" ${checked}></td>
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
          <tbody id="tf-worker-peers-tbody">${rows}</tbody>
        </table>
      </div>
      <div class="bm-worker-manual" style="margin-top:8px">
        <label style="font-size:12px">Add one manual entry (a peer not listed above, or override its ids)</label>
        <div class="form-grid">
          <input type="text" id="tf-worker-manual-peer_id" placeholder="peer_id">
          <input type="text" id="tf-worker-manual-peer_vpc_id" placeholder="peer_vpc_id">
          <input type="text" id="tf-worker-manual-peer_subnet_id" placeholder="peer_subnet_id">
          <input type="text" id="tf-worker-manual-peer_security_group_id" placeholder="peer_security_group_id">
        </div>
      </div>
      <span class="bm-field-hint bm-required" id="tf-worker-peers-warn" style="display:none"></span>
      <input type="hidden" id="tf-var-${key}" data-key="${key}" data-required="1" value="">
    </div>`;
}

async function _tfWireWorkerPeersField(key) {
  document.querySelectorAll('.tf-worker-peer-cb').forEach(cb => cb.addEventListener('change', () => _tfUpdateWorkerPeersValue(key)));
  ['peer_id', 'peer_vpc_id', 'peer_subnet_id', 'peer_security_group_id'].forEach(field => {
    const el = document.getElementById(`tf-worker-manual-${field}`);
    if (el) el.addEventListener('change', () => _tfUpdateWorkerPeersValue(key));
  });
  await _tfUpdateWorkerPeersValue(key);
}

async function _tfResolvePeerPlacement(peerId) {
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

async function _tfUpdateWorkerPeersValue(key) {
  const hidden = document.getElementById(`tf-var-${key}`);
  if (!hidden) return;
  const checkedBoxes = Array.from(document.querySelectorAll('.tf-worker-peer-cb:checked'));
  const resolved = await Promise.all(checkedBoxes.map(cb => _tfResolvePeerPlacement(cb.value)));
  let entries = resolved.filter(Boolean);

  // A checked peer silently vanishing from the submitted value (no
  // VPC/subnet/SG found there -- _tfResolvePeerPlacement's own null
  // return) used to surface only as a generic "Missing required
  // value: worker_peers" toast at submit time, indistinguishable from
  // an actual code bug. Confirmed live as a real, recurring case, not
  // hypothetical: worker_peers/coordinator_peer_id/kiwix_peer_* all
  // require the peer's own VPC/subnet/SG to already exist (created
  // outside this template -- see the variable's own comment), and a
  // peer that had one can legitimately lose it (e.g. an earlier
  // orphaned-resource cleanup that also removed the peer's VPC).
  // Naming exactly which checked peer(s) can't resolve, and why, lets
  // a real "no VPC on that peer yet" case be told apart from a genuine
  // bug at a glance.
  const unresolved = checkedBoxes.filter((cb, i) => !resolved[i]).map(cb => cb.dataset.hostname);
  const warn = document.getElementById('tf-worker-peers-warn');
  if (warn) {
    if (unresolved.length) {
      warn.textContent = `${unresolved.join(', ')} ${unresolved.length > 1 ? 'have' : 'has'} no VPC/subnet/security group yet on this peer — create one on the Peers/VPCs page first, or uncheck it above.`;
      warn.style.display = 'block';
    } else {
      warn.style.display = 'none';
    }
  }

  const manual = {
    peer_id: (document.getElementById('tf-worker-manual-peer_id') || {}).value?.trim(),
    peer_vpc_id: (document.getElementById('tf-worker-manual-peer_vpc_id') || {}).value?.trim(),
    peer_subnet_id: (document.getElementById('tf-worker-manual-peer_subnet_id') || {}).value?.trim(),
    peer_security_group_id: (document.getElementById('tf-worker-manual-peer_security_group_id') || {}).value?.trim(),
  };
  const manualFilled = Object.values(manual).filter(Boolean).length;
  hidden.dataset.manualPartial = (manualFilled > 0 && manualFilled < 4) ? '1' : '0';
  if (manualFilled === 4) entries.push(manual);

  hidden.value = entries.length ? JSON.stringify(entries) : '';
}

function _tfRenderPeerCascadeField(key, meta) {
  return `
    <div class="field">
      <label>${key.replace(/_/g, ' ')}${meta.required ? ' <span class="bm-required">*</span>' : ''}</label>
      <select id="tf-var-${key}" data-key="${key}" data-required="${meta.required ? '1' : '0'}" disabled>
        <option value="">— select a peer first —</option>
      </select>
    </div>
  `;
}

async function _tfWirePeerCascades(families) {
  for (const [peerKey, f] of Object.entries(families)) {
    if (!f.vpcKey && !f.subnetKey && !f.sgKey) continue;
    const peerSel = document.getElementById(`tf-var-${peerKey}`);
    if (!peerSel) continue;
    peerSel.addEventListener('change', () => _tfCascadeFromPeer(peerKey, f));
    // A recommendation may have pre-selected a real peer above (not the
    // "local" blank default) — cascade its vpc/subnet/sg immediately,
    // same as a user's own manual selection would.
    if (peerSel.value) await _tfCascadeFromPeer(peerKey, f);
  }
}

// Fetches the selected peer's own vpcs (+ security groups, unfiltered —
// there's no server-side vpc filter for those, so they're filtered
// client-side per vpc below) and populates the sibling vpc_id select,
// auto-selecting its first entry (most labs have exactly one) rather
// than leaving the user to guess an id — then cascades into subnet/sg.
async function _tfCascadeFromPeer(peerKey, f) {
  const peerId = document.getElementById(`tf-var-${peerKey}`).value;
  const vpcSel = f.vpcKey && document.getElementById(`tf-var-${f.vpcKey}`);
  const subnetSel = f.subnetKey && document.getElementById(`tf-var-${f.subnetKey}`);
  const sgSel = f.sgKey && document.getElementById(`tf-var-${f.sgKey}`);

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
    vpcSel.onchange = () => _tfCascadeFromVpc(vpcSel, subnetSel, sgSel, peerId);
    if (vpcs.length) await _tfCascadeFromVpc(vpcSel, subnetSel, sgSel, peerId);
  } else if (sgSel) {
    // No vpc field in this family — populate sg directly, unfiltered.
    sgSel.innerHTML = sgs.length
      ? sgs.map(s => `<option value="${s.id}">${_esc(s.name)}</option>`).join('')
      : '<option value="">No security groups found on this peer</option>';
    sgSel.disabled = !sgs.length;
  }
}

async function _tfCascadeFromVpc(vpcSel, subnetSel, sgSel, peerId) {
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

function _tfRenderPeerField(key, meta, peers, recommendation, hasWorkerPeers) {
  const label = `${key.replace(/_/g, ' ')}${meta.required ? ' <span class="bm-required">*</span>' : ''}`;
  // Never auto-recommend a standalone _peer_id field when this same
  // form also has a worker_peers list (llm-chat's own
  // coordinator_peer_id today) — recommend-placement has no idea
  // worker_peers exists and would happily suggest the very peer a
  // worker checkbox is about to auto-check, which the server then
  // rejects (same peer hosting both roles defeats the reason this
  // template splits across hosts via RPC). Matches this field's own
  // Terraform variable comment: "Deliberately not auto-selected... a
  // human choosing via the dashboard is this pass's actual mechanism."
  const rec = (!hasWorkerPeers && recommendation) ? recommendation.recommended : null;
  const recId = rec ? (rec.peer_id || '') : null;
  const options = peers.length
    ? peers.map(p => `<option value="${_esc(p.id)}"${p.id === recId ? ' selected' : ''}>${_esc(p.hostname)} (${_esc(p.wg_tunnel_status)})</option>`).join('')
    : '';
  return `
    <div class="field">
      <label>${label}</label>
      <select id="tf-var-${key}" data-key="${key}" data-required="${meta.required ? '1' : '0'}">
        <option value=""${recId === '' ? ' selected' : ''}>— local (this host) —</option>
        ${options}
      </select>
      ${rec && peers.length ? `<span class="bm-field-hint">Auto-selected: ${_esc(rec.hostname)} (${badge(rec.verdict)} on the Capacity traffic light) — change it if you'd rather place this yourself.</span>` : ''}
      ${hasWorkerPeers && peers.length ? `<span class="bm-field-hint">Not auto-selected — must be a different peer than any worker below, so this always starts local. Pick one deliberately if you want it peer-placed.</span>` : ''}
      ${!peers.length ? '<span class="bm-field-hint">No paired peers yet — see the Peers section.</span>' : ''}
    </div>
  `;
}

// Dispatched by meta.type === 'bool' (extract_template_vars() now reads
// the real `type = ...` line, not just variable name) -- every other
// dropdown in this file is name/regex-dispatched because it needs
// async-fetched data (peers/USB devices) to build its own option list;
// a bool needs neither, so it's the one case simple enough to dispatch
// generically by type instead of a hardcoded variable name. Options are
// the literal strings "true"/"false" -- tfSubmitBuild's existing
// generic select[data-key] collection loop already reads .value as a
// plain string for every field, and OpenTofu's own -var mechanism
// already parses "true"/"false" text into the variable's real
// `type = bool` regardless of how that text was typed/selected, exactly
// like the old free-text box already worked for this same variable.
function _tfRenderBoolField(key, meta) {
  const label = `${key.replace(/_/g, ' ')}${meta.required ? ' <span class="bm-required">*</span>' : ''}`;
  const def = String(meta.default ?? '').trim().toLowerCase() === 'true';
  return `
    <div class="field">
      <label>${label}</label>
      <select id="tf-var-${key}" data-key="${key}" data-required="${meta.required ? '1' : '0'}">
        <option value="true"${def ? ' selected' : ''}>true</option>
        <option value="false"${!def ? ' selected' : ''}>false</option>
      </select>
    </div>
  `;
}

function _tfRenderFlavorField(key, meta, coordinatorPick) {
  const label = `${key.replace(/_/g, ' ')}${meta.required ? ' <span class="bm-required">*</span>' : ''}`;
  // coordinatorPick is the same {peer_id, hostname, verdict,
  // best_flavor} entry _tfRenderPeerField's own bypass path already
  // used for coordinator_peer_id -- reused here so the two fields
  // always agree on which host/flavor pair they're describing. Falls
  // back to the schema default (today's standard.large) whenever
  // best_fit() found nothing any real candidate could afford, or the
  // recommend-placement call itself failed -- never a broken or
  // empty-looking state.
  const value = coordinatorPick ? coordinatorPick.best_flavor : (meta.default ?? '');
  return `
    <div class="field">
      <label>${label}</label>
      <input type="text" id="tf-var-${key}" data-key="${key}" data-required="${meta.required ? '1' : '0'}"
             placeholder="${meta.required ? 'Required — no default value' : ''}"
             value="${_esc(String(value))}">
      ${coordinatorPick ? `<span class="bm-field-hint">Auto-selected: ${_esc(coordinatorPick.best_flavor)} to match ${_esc(coordinatorPick.hostname)}'s real available capacity (RAM, vCPU, and disk all checked) — change it if you'd rather choose a flavor yourself.</span>` : ''}
    </div>
  `;
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

  const manualPartial = document.querySelector('#tf-var-fields [data-manual-partial="1"]');
  if (manualPartial) {
    toast('Fill in all four manual worker_peers fields, or none of them.', 'error');
    return;
  }

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
  if (vars[_TF_WORKER_PEERS_VAR]) vars[_TF_WORKER_PEERS_VAR] = JSON.parse(vars[_TF_WORKER_PEERS_VAR]);

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

  const idleSel = document.getElementById('tf-idle-timeout');
  const idleTimeoutMinutes = idleSel && idleSel.value ? Number(idleSel.value) : undefined;

  const btn = document.getElementById('tf-submit-btn');
  btn.disabled = true;
  btn.textContent = 'Submitting…';

  let data;
  try {
    data = await api('POST', '/v1/tofu/builds',
      { template: dirName, vars, idle_timeout_minutes: idleTimeoutMinutes });
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
