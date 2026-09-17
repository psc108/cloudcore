// ── Resource Placement — read-only inspection, not management ──────────────
// Where every instance, VPC, subnet, and security group actually lives
// (local or a named peer), when it was created, and that peer's own current
// status — per direct request: "a status page in the dashboard we can click
// to inspect that shows where all resources are located (local or peer),
// when they were created and the current peer status," later extended past
// instances-only per "do we have to limit resource placement to just
// instances?" — no, VPCs/subnets/security groups can be peer-placed too now
// — and again to show live CPU/memory/disk load per host, per "we need to
// be able to collect performance statistics from each peer in order to
// understand which is overloaded... and which can tolerate it." Pairing/
// approve/revoke stays on the Peers page; this page only ever reads.

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

// Reuses the existing status-badge CSS variants for a load percentage,
// rather than adding new classes just for this: badge-active (green)
// under the "comfortable" threshold, badge-pending (yellow/warn) under
// the "getting full" one, badge-error (red) at or past it — the same
// three-tier meaning every other badge on this dashboard already
// conveys, just keyed off a number instead of an enum value here.
function _placementLoadBadge(pct, warnAt, hotAt) {
  const cls = pct >= hotAt ? 'error' : pct >= warnAt ? 'pending' : 'active';
  return `<span class="badge badge-${cls}">${pct.toFixed(1)}%</span>`;
}

function _placementStatsRow(hostname, stats) {
  if (!stats) {
    return `
      <tr>
        <td><strong>${_esc(hostname)}</strong></td>
        <td colspan="4" class="text-muted">Unreachable — stats unavailable</td>
      </tr>`;
  }
  return `
    <tr>
      <td><strong>${_esc(hostname)}</strong></td>
      <td>${_placementLoadBadge(stats.cpu.load_pct_1m, 70, 100)} <span class="text-muted">(${stats.cpu.cores} cores, load ${stats.cpu.load_1m})</span></td>
      <td>${_placementLoadBadge(stats.memory.used_pct, 70, 90)} <span class="text-muted">(${stats.memory.available_mb} MB free)</span></td>
      <td>${_placementLoadBadge(stats.disk.used_pct, 70, 90)} <span class="text-muted">(${stats.disk.free_gb} GB free)</span></td>
      <td>${stats.instances.running} running / ${stats.instances.count} total</td>
    </tr>`;
}

function _renderPeerStatusTable(peersTbody, activePeers, resources) {
  if (!activePeers.length) {
    peersTbody.innerHTML = '<tr class="empty-row"><td colspan="5">No paired peers yet — see the Peers section.</td></tr>';
    return;
  }
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

function _renderPlacementTable(tbody, resources, peerById) {
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
}

function loadResourcePlacement() {
  const capacityTbody = document.getElementById('capacity-tbody');
  const peersTbody = document.getElementById('placement-peers-tbody');
  const tbody = document.getElementById('placement-tbody');
  capacityTbody.innerHTML = '<tr class="empty-row"><td colspan="5">Loading…</td></tr>';
  peersTbody.innerHTML = '<tr class="empty-row"><td colspan="5">Loading…</td></tr>';
  tbody.innerHTML = '<tr class="empty-row"><td colspan="6">Loading…</td></tr>';

  // Peers first — the capacity fetch below needs to know which peer
  // stats endpoints to call, a dynamic list not knowable up front the
  // way the four fixed resource-type endpoints are.
  api('GET', '/v1/peers').then(peerData => {
    const peers = peerData.items || [];
    const peerById = {};
    peers.forEach(p => { peerById[p.id] = p; });
    const activePeers = peers.filter(p => p.status !== 'revoked');

    return Promise.all([
      ..._PLACEMENT_TYPES.map(t => api('GET', t.path)),
      api('GET', '/v1/system/stats').catch(() => null),
      ...activePeers.map(p => api('GET', `/v1/peers/${p.id}/stats`).catch(() => null)),
    ]).then(results => {
      const resourceResults = results.slice(0, _PLACEMENT_TYPES.length);
      const localStats = results[_PLACEMENT_TYPES.length];
      const peerStatsResults = results.slice(_PLACEMENT_TYPES.length + 1);

      capacityTbody.innerHTML = [
        _placementStatsRow('This host (local)', localStats),
        ...activePeers.map((p, i) => _placementStatsRow(p.hostname, peerStatsResults[i])),
      ].join('');

      const resources = [];
      _PLACEMENT_TYPES.forEach((t, i) => {
        (resourceResults[i].items || []).forEach(r => resources.push({ type: t.type, ...r }));
      });

      _renderPeerStatusTable(peersTbody, activePeers, resources);
      _renderPlacementTable(tbody, resources, peerById);
    });
  }).catch(e => {
    const peerMsg = `<tr class="empty-row"><td colspan="5">Error: ${_esc(e.message)}</td></tr>`;
    const resMsg = `<tr class="empty-row"><td colspan="6">Error: ${_esc(e.message)}</td></tr>`;
    capacityTbody.innerHTML = peerMsg;
    peersTbody.innerHTML = peerMsg;
    tbody.innerHTML = resMsg;
  });
}
