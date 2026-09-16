// ── Cross-host peering ──────────────────────────────────────────────────────
// var, not let: ui/build.sh concatenates js/*.js in filename order, and
// 15-init.js's own top-level showSection('dashboard', ...) call (which
// reaches stopPeersPoll() via showSection's own unconditional cleanup)
// executes before the parser reaches this file's own declarations — a
// `let` here hits the temporal-dead-zone (confirmed live: "Uncaught
// ReferenceError: can't access lexical declaration '_peersPollTimer'
// before initialization", breaking every nav click, not just Peers,
// since it aborted the rest of showSection() for every section). `var`
// is hoisted with an initial `undefined` regardless of source position.
var _peersPollTimer = null;
var _lastDiscovered = [];

function startPeersPoll() {
  if (_peersPollTimer) return;
  // Pending requests are the one thing worth polling for on this page —
  // another host's pairing request can arrive at any time, not just when
  // this tab happens to be freshly opened.
  _peersPollTimer = setInterval(loadPendingRequests, 15000);
}

function stopPeersPoll() {
  clearInterval(_peersPollTimer);
  _peersPollTimer = null;
}

function loadPeers() {
  const tbody = document.getElementById('peers-tbody');
  tbody.innerHTML = '<tr class="empty-row"><td colspan="6">Loading…</td></tr>';
  api('GET', '/v1/peers').then(data => {
    const items = data.items.filter(p => p.status !== 'revoked');
    if (!items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No peers yet — discover or pair with one below.</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(p => `
      <tr>
        <td><strong>${p.hostname}</strong> <span class="mono text-muted" title="${p.pubkey_fpr}">${(p.pubkey_fpr || '').slice(0, 20)}…</span></td>
        <td>${badge(p.status)}</td>
        <td>${badge(p.wg_tunnel_status)}</td>
        <td class="mono">${p.wg_bridge_subnet || '—'}</td>
        <td>${p.direction}</td>
        <td><button class="btn btn-danger btn-sm" onclick="revokePeer('${p.id}','${p.hostname}')">Revoke</button></td>
      </tr>`).join('');
  }).catch(e => {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="6">Error: ${e.message}</td></tr>`;
  });
}

function loadPendingRequests() {
  const tbody = document.getElementById('pending-requests-tbody');
  api('GET', '/v1/peers/pairing-requests?status=pending').then(data => {
    if (!data.items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="5">No pending requests.</td></tr>';
      return;
    }
    tbody.innerHTML = data.items.map(r => `
      <tr>
        <td><strong>${r.hostname}</strong></td>
        <td class="mono text-muted" title="${r.pubkey_fpr}">${(r.pubkey_fpr || '').slice(0, 20)}…</td>
        <td>${new Date(r.created_at).toLocaleString()}</td>
        <td>${new Date(r.expires_at).toLocaleString()}</td>
        <td style="display:flex;gap:6px">
          <button class="btn btn-primary btn-sm" onclick="approveRequest('${r.id}','${r.hostname}')">Approve</button>
          <button class="btn btn-danger btn-sm" onclick="rejectRequest('${r.id}','${r.hostname}')">Reject</button>
        </td>
      </tr>`).join('');
  }).catch(() => {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="5">Error loading pending requests.</td></tr>';
  });
}

function approveRequest(id, hostname) {
  if (!confirm(`Approve pairing with "${hostname}"? This establishes trust — only approve a host you recognize.`)) return;
  api('PUT', `/v1/peers/pairing-requests/${id}/approve`).then(result => {
    if (result.callback_delivered === false) {
      toast(`Paired with "${hostname}", but couldn't reach it to confirm — it may need to retry from its own side.`, 'error');
    } else {
      toast(`Paired with "${hostname}"`, 'success');
    }
    loadPendingRequests();
    loadPeers();
  }).catch(e => toast(`Failed to approve: ${e.message}`, 'error'));
}

function rejectRequest(id, hostname) {
  if (!confirm(`Reject pairing request from "${hostname}"?`)) return;
  api('PUT', `/v1/peers/pairing-requests/${id}/reject`).then(() => {
    toast(`Rejected request from "${hostname}"`, 'success');
    loadPendingRequests();
  }).catch(e => toast(`Failed: ${e.message}`, 'error'));
}

function revokePeer(id, hostname) {
  if (!confirm(`Revoke pairing with "${hostname}"? This tears down its tunnel and cross-host builds targeting it will stop working.`)) return;
  api('DELETE', `/v1/peers/${id}`).then(() => {
    toast(`Revoked "${hostname}"`, 'success');
    loadPeers();
  }).catch(e => toast(`Failed: ${e.message}`, 'error'));
}

function scanForPeers() {
  const tbody = document.getElementById('discovered-tbody');
  tbody.innerHTML = '<tr class="empty-row"><td colspan="4">Scanning…</td></tr>';
  api('GET', '/v1/peers/discovered').then(data => {
    _lastDiscovered = data.items;
    if (!data.items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="4">Nothing found — the other host may not have discovery turned on.</td></tr>';
      return;
    }
    tbody.innerHTML = data.items.map((d, i) => `
      <tr>
        <td><strong>${d.hostname}</strong></td>
        <td class="mono">${d.address}:${d.port}</td>
        <td class="mono text-muted" title="${d.pubkey_fpr}">${(d.pubkey_fpr || '').slice(0, 20)}…</td>
        <td><button class="btn btn-primary btn-sm" onclick="pairWithDiscovered(${i})">Pair</button></td>
      </tr>`).join('');
  }).catch(e => {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="4">Error: ${e.message}</td></tr>`;
  });
}

function pairWithDiscovered(index) {
  const d = _lastDiscovered[index];
  if (!d) return;
  _requestPairing(d.hostname, d.address, d.port);
}

function pairManual() {
  const hostname = document.getElementById('peer-manual-hostname').value.trim();
  const address = document.getElementById('peer-manual-address').value.trim();
  const port = Number(document.getElementById('peer-manual-port').value);
  if (!hostname || !address || !port) { toast('Hostname, address, and port are required', 'error'); return; }
  _requestPairing(hostname, address, port);
}

function _requestPairing(hostname, address, port) {
  api('POST', '/v1/peers', { hostname, address, port }).then(() => {
    toast(`Pairing request sent to "${hostname}" — it needs to approve on its own dashboard before this is usable.`, 'success');
    loadPeers();
  }).catch(e => toast(`Failed to reach "${hostname}": ${e.message}`, 'error'));
}
