// ── Detail modal ─────────────────────────────────────────────────────────────
const _detailSectionMap = { vpc: 'vpcs', instance: 'instances', lb: 'lbs', nfs: 'nfs' };
let _detailConsoleTimer = null;
let _detailConsoleInstanceId = null;

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
    instance: () => [['ID', resource.id], ['Image', resource.image_id], ['Flavor', resource.flavor], ['Private IP', resource.private_ip || '—'], ['SSH Port', resource.ssh_port ? '127.0.0.1:' + resource.ssh_port : '—'], ['Status', resource.status], ['Created', fmtDate(resource.created_at)]],
    lb:       () => [['ID', resource.id], ['Type', resource.type], ['Scheme', resource.internal ? 'Internal' : 'Internet-facing'], ['Listen Port', resource.listen_port ? '127.0.0.1:' + resource.listen_port : '—'], ['Backends', (resource.backends || []).length], ['Status', resource.status], ['Created', fmtDate(resource.created_at)]],
    nfs:      () => [['ID', resource.id], ['Flavor', resource.flavor], ['Disk', resource.disk_gb + ' GB'], ['Private IP', resource.private_ip || '—'], ['Shares', (resource.shares || []).map(s => s.name).join(', ') || '—'], ['Status', resource.status], ['Created', fmtDate(resource.created_at)]],
  };
  document.getElementById('detail-kv').innerHTML = ((fieldMap[type] || (() => []))()).map(
    ([k, v]) => `<div class="dk">${k}</div><div class="dv">${v}</div>`
  ).join('');
  document.getElementById('detail-goto-btn').onclick = () => {
    closeDetailModal();
    showSectionByName(_detailSectionMap[type]);
  };

  _stopDetailConsolePoll();
  const panel = document.getElementById('detail-console-panel');
  if (type === 'instance') {
    panel.style.display = '';
    _detailConsoleInstanceId = resource.id;
    _pollDetailConsole();
    _detailConsoleTimer = setInterval(_pollDetailConsole, 3000);
  } else {
    panel.style.display = 'none';
  }

  document.getElementById('detail-modal-overlay').classList.add('open');
}

// Serial console output includes raw ANSI color/cursor escape sequences
// (systemd's colored [ OK ] boot messages) — a plain <pre> has no terminal
// emulator behind it, so these render as literal garbage without this.
function _stripAnsi(s) {
  return s.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '');
}

function _pollDetailConsole() {
  const id = _detailConsoleInstanceId;
  if (!id) return;
  api('GET', `/v1/instances/${id}/console?lines=300`).then(d => {
    if (_detailConsoleInstanceId !== id) return;  // modal moved on to something else
    const pre = document.getElementById('detail-console-pre');
    const autoscroll = document.getElementById('detail-console-autoscroll').checked;
    pre.textContent = _stripAnsi(d.output || '') || '(no output yet)';
    if (autoscroll) pre.scrollTop = pre.scrollHeight;
  }).catch(() => {
    if (_detailConsoleInstanceId !== id) return;
    document.getElementById('detail-console-pre').textContent =
      'Console log not available yet — instance may still be starting, or was created before serial logging was added.';
  });
}

function _copyDetailConsole(btn) {
  const lines = document.getElementById('detail-console-pre').textContent.split('\n');
  copyText(lines.slice(-100).join('\n'), btn);
}

function _stopDetailConsolePoll() {
  clearInterval(_detailConsoleTimer);
  _detailConsoleTimer = null;
  _detailConsoleInstanceId = null;
}

function closeDetailOverlay(e) {
  if (e.target === document.getElementById('detail-modal-overlay')) closeDetailModal();
}

function closeDetailModal() {
  document.getElementById('detail-modal-overlay').classList.remove('open');
  _stopDetailConsolePoll();
}
