// ── Detail modal ─────────────────────────────────────────────────────────────
const _detailSectionMap = { vpc: 'vpcs', instance: 'instances', lb: 'lbs', nfs: 'nfs' };


function openDetailById(id) {
  const entry = _dashResources[id];
  if (!entry) return;
  openDetail(entry.type, entry.resource);
}

function openDetail(type, resource) {
  const titles = { vpc: 'VPC', instance: 'Instance', lb: 'Load Balancer', nfs: 'NFS Server' };
  document.getElementById('detail-title').textContent = titles[type] + ': ' + resource.name;
  const fieldMap = {
    vpc:      () => [['ID', resource.id], ['CIDR', resource.cidr_block], ['DNS Support', resource.dns_support ? 'Yes' : 'No'], ['Status', resource.status], ['Created', fmtDate(resource.created_at)]],
    instance: () => [['ID', resource.id], ['Image', resource.image_id], ['Flavor', resource.flavor], ['Private IP', resource.private_ip || '\u2014'], ['SSH Port', resource.ssh_port ? '127.0.0.1:' + resource.ssh_port : '\u2014'], ['Status', resource.status], ['Created', fmtDate(resource.created_at)]],
    lb:       () => [['ID', resource.id], ['Type', resource.type], ['Scheme', resource.internal ? 'Internal' : 'Internet-facing'], ['Listen Port', resource.listen_port ? '127.0.0.1:' + resource.listen_port : '\u2014'], ['Backends', (resource.backends || []).length], ['Status', resource.status], ['Created', fmtDate(resource.created_at)]],
    nfs:      () => [['ID', resource.id], ['Flavor', resource.flavor], ['Disk', resource.disk_gb + ' GB'], ['Private IP', resource.private_ip || '\u2014'], ['Shares', (resource.shares || []).map(s => s.name).join(', ') || '\u2014'], ['Status', resource.status], ['Created', fmtDate(resource.created_at)]],
  };
  document.getElementById('detail-kv').innerHTML = ((fieldMap[type] || (() => []))()).map(
    ([k, v]) => `<div class="dk">${k}</div><div class="dv">${v}</div>`
  ).join('');
  document.getElementById('detail-goto-btn').onclick = () => {
    closeDetailModal();
    showSectionByName(_detailSectionMap[type]);
  };
  document.getElementById('detail-modal-overlay').classList.add('open');
}

function closeDetailOverlay(e) {
  if (e.target === document.getElementById('detail-modal-overlay')) closeDetailModal();
}

function closeDetailModal() {
  document.getElementById('detail-modal-overlay').classList.remove('open');
}
