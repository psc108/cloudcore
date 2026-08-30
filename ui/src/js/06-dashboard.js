// ── Dashboard ────────────────────────────────────────────────────────────────
let _dashTimer = null;
const _dashResources = {};   // id -> { type, resource } — avoids inline JSON in onclick

function _dashRow(type, r, meta) {
  _dashResources[r.id] = { type, resource: r };
  const statusStr = typeof r.status === 'object' ? r.status.value : r.status;
  return `<div class="dash-resource-row" onclick="openDetailById('${r.id}')">
    <div class="drr-name">${r.name}</div>
    <div class="drr-meta">${meta}</div>
    ${badge(statusStr)}
  </div>`;
}

async function loadDashboard() {
  try {
    const d = await api('GET', '/v1/dashboard');
    const s = d.summary;

    document.getElementById('ds-vpc-count').textContent  = s.vpcs;
    document.getElementById('ds-inst-count').textContent = s.instances;
    document.getElementById('ds-inst-sub').textContent   =
      s.instances_running + ' running' +
      (s.instances - s.instances_running > 0 ? ', ' + (s.instances - s.instances_running) + ' other' : '');
    document.getElementById('ds-lb-count').textContent  = s.load_balancers;
    document.getElementById('ds-dns-count').textContent = s.dns_zones;
    document.getElementById('ds-dns-sub').textContent   =
      s.dns_zones + ' zone' + (s.dns_zones !== 1 ? 's' : '') +
      ', ' + s.dns_records + ' record' + (s.dns_records !== 1 ? 's' : '');

    const nfsData = await api('GET', '/v1/nfs-servers');
    const nfsItems = nfsData.items || [];
    document.getElementById('ds-nfs-count').textContent = nfsItems.length;

    const vpcs = d.vpcs;
    document.getElementById('dp-vpc-count').textContent = vpcs.length + ' total';
    document.getElementById('dp-vpcs').innerHTML = vpcs.length
      ? vpcs.map(v => _dashRow('vpc', v, `<span class="mono">${v.cidr_block}</span>`)).join('')
      : '<div class="dash-empty">No VPCs</div>';

    const insts = d.instances;
    document.getElementById('dp-inst-count').textContent = insts.length + ' total';
    document.getElementById('dp-instances').innerHTML = insts.length
      ? insts.map(i => _dashRow('instance', i, `<span class="mono">${i.private_ip || '—'}</span>`)).join('')
      : '<div class="dash-empty">No instances</div>';

    const lbs = d.load_balancers;
    document.getElementById('dp-lb-count').textContent = lbs.length + ' total';
    document.getElementById('dp-lbs').innerHTML = lbs.length
      ? lbs.map(lb => _dashRow('lb', lb, `${lb.type} · ${lb.backends.length} backend${lb.backends.length !== 1 ? 's' : ''}`)).join('')
      : '<div class="dash-empty">No load balancers</div>';

    const zones = d.dns_zones;
    document.getElementById('dp-dns-count').textContent = zones.length + ' total';
    document.getElementById('dp-dns').innerHTML = zones.length
      ? zones.map(z => `<div class="dash-resource-row" onclick="showSectionByName('dns')">
          <div class="drr-name">${z.name}</div>
          <div class="drr-meta">${z.record_count} record${z.record_count !== 1 ? 's' : ''}</div>
          ${z.builtin ? '<span class="badge badge-active">built-in</span>' : ''}
        </div>`).join('')
      : '<div class="dash-empty">No DNS zones</div>';

    document.getElementById('dp-nfs-count').textContent = nfsItems.length + ' total';
    document.getElementById('dp-nfs').innerHTML = nfsItems.length
      ? nfsItems.map(s => _dashRow('nfs', s,
          `${(s.shares || []).length} share${(s.shares || []).length !== 1 ? 's' : ''}`)).join('')
      : '<div class="dash-empty">No NFS servers</div>';

    document.getElementById('dash-refresh-label').textContent =
      'Last refreshed ' + new Date().toLocaleTimeString();
  } catch(e) {
    toast('Dashboard refresh failed: ' + e.message, 'error');
  }
}

function startDashPoll() {
  if (_dashTimer) return;
  _dashTimer = setInterval(loadDashboard, 60000);
}

function stopDashPoll() {
  clearInterval(_dashTimer);
  _dashTimer = null;
}
