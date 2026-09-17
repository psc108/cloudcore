// ── Resource Placement — read-only inspection, not management ──────────────
// Where every instance actually lives (local or a named peer), when it was
// created, and that peer's own current status — per direct request: "a
// status page in the dashboard we can click to inspect that shows where all
// resources are located (local or peer), when they were created and the
// current peer status." Pairing/approve/revoke stays on the Peers page;
// this page only ever reads.

function loadResourcePlacement() {
  const peersTbody = document.getElementById('placement-peers-tbody');
  const tbody = document.getElementById('placement-tbody');
  peersTbody.innerHTML = '<tr class="empty-row"><td colspan="5">Loading…</td></tr>';
  tbody.innerHTML = '<tr class="empty-row"><td colspan="5">Loading…</td></tr>';

  Promise.all([api('GET', '/v1/instances'), api('GET', '/v1/peers')]).then(([instData, peerData]) => {
    const instances = instData.items || [];
    const peers = peerData.items || [];
    // All peers (including revoked) feed the by-id lookup below, so a
    // peer-placed instance's row can still say *which* peer and that
    // it's since been revoked, rather than just "unknown" — but the
    // summary table only ever shows current, non-revoked peers, same
    // filter loadPeers() already applies on the Peers page itself.
    const peerById = {};
    peers.forEach(p => { peerById[p.id] = p; });
    const activePeers = peers.filter(p => p.status !== 'revoked');

    if (!activePeers.length) {
      peersTbody.innerHTML = '<tr class="empty-row"><td colspan="5">No paired peers yet — see the Peers section.</td></tr>';
    } else {
      peersTbody.innerHTML = activePeers.map(p => {
        const count = instances.filter(i => i.host_id === p.id).length;
        return `
          <tr>
            <td><strong>${_esc(p.hostname)}</strong></td>
            <td>${badge(p.status)}</td>
            <td>${badge(p.wg_tunnel_status || 'unknown')}</td>
            <td class="mono">${_esc(p.wg_bridge_subnet || '—')}</td>
            <td>${count}</td>
          </tr>`;
      }).join('');
    }

    if (!instances.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="5">No instances yet.</td></tr>';
      return;
    }
    tbody.innerHTML = instances.map(i => {
      let location, peerStatus;
      if (i.host_id) {
        const peer = peerById[i.host_id];
        if (peer) {
          location = `<span class="mono">${_esc(i.host_hostname || peer.hostname)}</span>`;
          peerStatus = `${badge(peer.status)} ${badge(peer.wg_tunnel_status || 'unknown')}`;
        } else {
          // The peer this instance was placed on has since been revoked
          // (or its row is otherwise gone) — the instance record itself
          // still exists locally, but there's no live peer to report a
          // status for. Surfaced honestly rather than hidden.
          location = `<span class="mono text-muted">${_esc(i.host_hostname || 'unknown peer')}</span>`;
          peerStatus = '<span class="badge badge-error">peer no longer paired</span>';
        }
      } else {
        location = 'Local (this host)';
        peerStatus = '—';
      }
      return `
        <tr>
          <td><strong>${_esc(i.name)}</strong></td>
          <td>${location}</td>
          <td>${new Date(i.created_at).toLocaleString()}</td>
          <td>${badge(i.status)}</td>
          <td>${peerStatus}</td>
        </tr>`;
    }).join('');
  }).catch(e => {
    const msg = `<tr class="empty-row"><td colspan="5">Error: ${_esc(e.message)}</td></tr>`;
    peersTbody.innerHTML = msg;
    tbody.innerHTML = msg;
  });
}
