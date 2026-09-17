// ── Resource Placement — read-only inspection, not management ──────────────
// Where every instance, VPC, subnet, and security group actually lives
// (local or a named peer), when it was created, and that peer's own current
// status — per direct request: "a status page in the dashboard we can click
// to inspect that shows where all resources are located (local or peer),
// when they were created and the current peer status," later extended past
// instances-only per "do we have to limit resource placement to just
// instances?" — no, VPCs/subnets/security groups can be peer-placed too now.
// Pairing/approve/revoke stays on the Peers page; this page only ever reads.

// Load balancers and NFS servers are deliberately NOT fetched here — neither
// resource type supports peer_id (an LB is always this build's own stable
// front door regardless of where its backends live; an NFS server's
// placement question is really "which instances mount it," not something
// independently worth moving) — see haFullStack-LLD.md §13 for the full
// reasoning. Listing them here would just be a Location column of "Local"
// on every single row, forever.
const _PLACEMENT_TYPES = [
  { type: 'Instance',       path: '/v1/instances' },
  { type: 'VPC',            path: '/v1/vpcs' },
  { type: 'Subnet',         path: '/v1/subnets' },
  { type: 'Security Group', path: '/v1/security-groups' },
];

function loadResourcePlacement() {
  const peersTbody = document.getElementById('placement-peers-tbody');
  const tbody = document.getElementById('placement-tbody');
  peersTbody.innerHTML = '<tr class="empty-row"><td colspan="5">Loading…</td></tr>';
  tbody.innerHTML = '<tr class="empty-row"><td colspan="6">Loading…</td></tr>';

  Promise.all([
    ..._PLACEMENT_TYPES.map(t => api('GET', t.path)),
    api('GET', '/v1/peers'),
  ]).then(results => {
    const peerData = results[results.length - 1];
    const peers = peerData.items || [];
    const resources = [];
    _PLACEMENT_TYPES.forEach((t, i) => {
      (results[i].items || []).forEach(r => resources.push({ type: t.type, ...r }));
    });

    // All peers (including revoked) feed the by-id lookup below, so a
    // peer-placed resource's row can still say *which* peer and that
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
        const count = resources.filter(r => r.host_id === p.id).length;
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

    if (!resources.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No resources yet.</td></tr>';
      return;
    }
    tbody.innerHTML = resources.map(r => {
      let location, peerStatus;
      if (r.host_id) {
        const peer = peerById[r.host_id];
        if (peer) {
          location = `<span class="mono">${_esc(r.host_hostname || peer.hostname)}</span>`;
          peerStatus = `${badge(peer.status)} ${badge(peer.wg_tunnel_status || 'unknown')}`;
        } else {
          // The peer this resource was placed on has since been revoked
          // (or its row is otherwise gone) — the local wrapper record
          // still exists, but there's no live peer to report a status
          // for. Surfaced honestly rather than hidden.
          location = `<span class="mono text-muted">${_esc(r.host_hostname || 'unknown peer')}</span>`;
          peerStatus = '<span class="badge badge-error">peer no longer paired</span>';
        }
      } else {
        location = 'Local (this host)';
        peerStatus = '—';
      }
      return `
        <tr>
          <td><strong>${_esc(r.name)}</strong></td>
          <td>${_esc(r.type)}</td>
          <td>${location}</td>
          <td>${new Date(r.created_at).toLocaleString()}</td>
          <td>${badge(r.status)}</td>
          <td>${peerStatus}</td>
        </tr>`;
    }).join('');
  }).catch(e => {
    const peerMsg = `<tr class="empty-row"><td colspan="5">Error: ${_esc(e.message)}</td></tr>`;
    const resMsg = `<tr class="empty-row"><td colspan="6">Error: ${_esc(e.message)}</td></tr>`;
    peersTbody.innerHTML = peerMsg;
    tbody.innerHTML = resMsg;
  });
}
