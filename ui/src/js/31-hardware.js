// ── Hardware ─────────────────────────────────────────────────────────────────
function loadHardware() {
  api('GET', '/v1/hardware').then(data => {
    _hwKeyValueTable('hw-system', {
      hostname: data.os.hostname, product_name: data.bios.product_name,
      system_vendor: data.bios.system_vendor, chassis_type: data.bios.chassis_type,
      distro: data.os.distro, kernel: data.os.kernel, arch: data.os.arch, uptime: data.os.uptime,
    }, _HW_LABELS);

    _hwKeyValueTable('hw-bios', {
      bios_vendor: data.bios.bios_vendor, bios_version: data.bios.bios_version,
      bios_date: data.bios.bios_date, board_vendor: data.bios.board_vendor,
      board_name: data.bios.board_name, board_version: data.bios.board_version,
    }, _HW_LABELS);

    const cpu = data.cpu;
    _hwKeyValueTable('hw-cpu', {
      model: cpu.model, vendor: cpu.vendor, architecture: cpu.architecture,
      sockets: cpu.sockets, cores_per_socket: cpu.cores_per_socket,
      threads_per_core: cpu.threads_per_core, logical_cpus: cpu.logical_cpus,
      speed_range: (cpu.min_mhz && cpu.max_mhz) ? `${_hwMhz(cpu.min_mhz)} – ${_hwMhz(cpu.max_mhz)}` : (cpu.max_mhz ? _hwMhz(cpu.max_mhz) : ''),
      cache_l1: `${cpu.cache.l1d || '?'} data / ${cpu.cache.l1i || '?'} instr.`,
      cache_l2: cpu.cache.l2 || '—', cache_l3: cpu.cache.l3 || '—',
      virtualization: cpu.virtualization || 'Not supported / not exposed',
    }, _HW_LABELS);
    _hwCpuVulnerabilities(cpu.vulnerabilities || {});

    const mem = data.memory;
    _hwKeyValueTable('hw-memory', {
      total: `${(mem.total_mb / 1024).toFixed(1)} GiB`,
      swap_total: mem.swap_total_mb ? `${(mem.swap_total_mb / 1024).toFixed(1)} GiB` : 'None',
    }, _HW_LABELS);
    _hwDimms(mem.dimms || [], mem.dimm_detail_available);

    _hwListTable('hw-disks', data.disks || [], d =>
      `<tr><td>${_esc(d.name)}</td><td>${_esc(d.model) || '—'}</td><td>${_esc(d.vendor) || '—'}</td>` +
      `<td>${d.size_gb ? d.size_gb + ' GB' : '—'}</td><td>${_esc(d.media)}</td><td>${_esc(d.transport) || '—'}</td></tr>`,
      6);

    _hwListTable('hw-gpus', data.gpus || [], g =>
      `<tr><td>${_esc(g.vendor)}</td><td>${_esc(g.model)}</td></tr>`, 2);

    _hwListTable('hw-network', data.network || [], n => {
      const speedDuplex = n.speed_mbps
        ? `${n.speed_mbps} Mbps${n.duplex ? ' / ' + n.duplex : ''}`
        : (n.tx_bitrate_mbps ? `${n.tx_bitrate_mbps} Mbps (negotiated)` : '—');
      const wireless = n.wireless
        ? (n.ssid
            ? `${_esc(n.ssid)}${n.signal_dbm ? ` (${_esc(n.signal_dbm)})` : ''}${n.frequency_mhz ? `, ${_esc(n.frequency_mhz)} MHz` : ''}`
            : '<span class="bm-field-hint" style="margin:0">install <code>iw</code> for link detail</span>')
        : '—';
      const firmware = n.firmware_version ? `${_esc(n.firmware_version)}` : '—';
      const driver = n.driver ? `${_esc(n.driver)}${n.driver_version ? ' (' + _esc(n.driver_version) + ')' : ''}` : '—';
      return `<tr><td>${_esc(n.name)}</td><td>${_esc(n.mac) || '—'}</td>` +
        `<td>${driver}</td><td>${firmware}</td><td>${_esc(n.bus_info) || '—'}</td>` +
        `<td>${speedDuplex}</td><td>${wireless}</td>` +
        `<td><span class="badge badge-${n.operstate === 'up' ? 'active' : ''}">${_esc(n.operstate) || 'unknown'}</span></td></tr>`;
    }, 8);
  }).catch(() => {
    ['hw-system', 'hw-bios', 'hw-cpu', 'hw-memory'].forEach(id => {
      document.getElementById(id).innerHTML = '<tr><td colspan="2" class="empty-row">Failed to load</td></tr>';
    });
  });
}

function _hwMhz(mhzStr) {
  const n = parseFloat(mhzStr);
  return isNaN(n) ? mhzStr : `${(n / 1000).toFixed(2)} GHz`;
}

const _HW_LABELS = {
  hostname: 'Hostname', product_name: 'Product Name', system_vendor: 'System Vendor',
  chassis_type: 'Chassis Type', distro: 'Distribution', kernel: 'Kernel', arch: 'Architecture',
  uptime: 'Uptime', bios_vendor: 'BIOS Vendor', bios_version: 'BIOS Version', bios_date: 'BIOS Date',
  board_vendor: 'Board Vendor', board_name: 'Board Name', board_version: 'Board Version',
  model: 'Model', vendor: 'Vendor', architecture: 'Architecture', sockets: 'Socket(s)',
  cores_per_socket: 'Cores per Socket', threads_per_core: 'Threads per Core',
  logical_cpus: 'Logical CPUs', speed_range: 'Clock Speed', cache_l1: 'L1 Cache',
  cache_l2: 'L2 Cache', cache_l3: 'L3 Cache', virtualization: 'Virtualization',
  total: 'Total Installed', swap_total: 'Swap',
};

function _hwKeyValueTable(tbodyId, obj, labels) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  tbody.innerHTML = Object.entries(obj)
    .filter(([, v]) => v !== '' && v !== undefined && v !== null)
    .map(([k, v]) => `<tr><td class="about-key">${labels[k] || k}</td><td class="about-val">${_esc(String(v))}</td></tr>`)
    .join('') || '<tr><td colspan="2" class="empty-row">Not available</td></tr>';
}

function _hwListTable(tbodyId, items, rowFn, colspan) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  tbody.innerHTML = items.length
    ? items.map(rowFn).join('')
    : `<tr class="empty-row"><td colspan="${colspan}">None detected</td></tr>`;
}

function _hwCpuVulnerabilities(vulns) {
  const el = document.getElementById('hw-cpu-vulns');
  if (!el) return;
  const entries = Object.entries(vulns);
  if (!entries.length) { el.innerHTML = ''; return; }
  const rows = entries.map(([name, status]) => {
    const affected = /vulnerable/i.test(status) && !/not affected/i.test(status);
    return `<tr><td class="about-key">${_esc(name)}</td>` +
      `<td class="about-val"><span class="badge badge-${affected ? 'error' : 'active'}">${_esc(status)}</span></td></tr>`;
  }).join('');
  el.innerHTML = `
    <details>
      <summary style="cursor:pointer;font-size:13px;color:var(--text-muted)">Security mitigations (${entries.length} checked)</summary>
      <table class="about-table" style="margin-top:8px"><tbody>${rows}</tbody></table>
    </details>`;
}

function _hwDimms(dimms, available) {
  const el = document.getElementById('hw-dimms-wrap');
  if (!el) return;
  if (!available) {
    el.innerHTML = `<span class="bm-field-hint">Per-DIMM detail (size/speed/manufacturer/part number per stick) needs one extra grant on this host: <code>sudo bash api/setup-hwinfo.sh</code>. Everything else on this page already works without it.</span>`;
    return;
  }
  if (!dimms.length) {
    el.innerHTML = '<span class="bm-field-hint">No populated memory slots reported.</span>';
    return;
  }
  const rows = dimms.map(d => `<tr>
    <td>${_esc(d.locator) || '—'}</td><td>${_esc(d.size)}</td><td>${_esc(d.type) || '—'}</td>
    <td>${_esc(d.configured_speed) || _esc(d.speed) || '—'}</td>
    <td>${_esc(d.manufacturer) || '—'}</td><td>${_esc(d.part_number) || '—'}</td>
  </tr>`).join('');
  el.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Slot</th><th>Size</th><th>Type</th><th>Speed</th><th>Manufacturer</th><th>Part Number</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}
