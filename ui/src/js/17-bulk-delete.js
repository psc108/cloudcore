// ── Bulk delete ───────────────────────────────────────────────────────────────

const _bulkMeta = {
  vpc:      { btn: 'vpc-delete-sel',  selAll: 'vpc-sel-all',  path: id => `/v1/vpcs/${id}`,           reload: loadVPCs },
  instance: { btn: 'inst-delete-sel', selAll: 'inst-sel-all', path: id => `/v1/instances/${id}`,      reload: loadInstances },
  lb:       { btn: 'lb-delete-sel',   selAll: 'lb-sel-all',   path: id => `/v1/load-balancers/${id}`, reload: loadLBs },
};

function _checkedRows(type) {
  return Array.from(document.querySelectorAll(`.row-cb[data-type="${type}"]:checked`));
}

function _onRowCbChange(type) {
  const m = _bulkMeta[type];
  const count = _checkedRows(type).length;
  document.getElementById(m.btn).style.display = count ? 'inline-flex' : 'none';
  // Sync select-all state
  const all = document.querySelectorAll(`.row-cb[data-type="${type}"]`);
  const selAll = document.getElementById(m.selAll);
  if (selAll) selAll.checked = all.length > 0 && count === all.length;
}

function toggleSelectAll(type, cb) {
  document.querySelectorAll(`.row-cb[data-type="${type}"]`).forEach(el => el.checked = cb.checked);
  _onRowCbChange(type);
}

async function deleteSelected(type) {
  const rows = _checkedRows(type);
  if (!rows.length) return;
  const names = rows.map(r => r.dataset.name).join(', ');
  const label = type === 'instance' ? 'instance' : type === 'lb' ? 'load balancer' : 'VPC';
  if (!confirm(`Delete ${rows.length} ${label}${rows.length > 1 ? 's' : ''}?\n\n${names}`)) return;

  const m = _bulkMeta[type];
  await Promise.allSettled(rows.map(r => api('DELETE', m.path(r.dataset.id))));
  document.getElementById(m.btn).style.display = 'none';
  const selAll = document.getElementById(m.selAll);
  if (selAll) selAll.checked = false;
  m.reload();
}
