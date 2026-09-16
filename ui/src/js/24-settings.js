// ── Settings ─────────────────────────────────────────────────────────────────
function loadSettings() {
  Promise.all([
    api('GET', '/v1/settings/tofu'),
    api('GET', '/v1/settings/discovery'),
    api('GET', '/v1/settings/network'),
  ]).then(([tofu, discovery, network]) => {
    document.getElementById('settings-tofu-parallelism').value = tofu.parallelism ?? '';
    document.getElementById('settings-discovery-enabled').checked = !!discovery.enabled;
    document.getElementById('settings-bridge-octet').value = network.bridge_subnet_octet ?? '';
  }).catch(() => toast('Failed to load settings', 'error'));
}

function saveSettings() {
  const raw = document.getElementById('settings-tofu-parallelism').value.trim();
  const parallelism = raw === '' ? null : Number(raw);
  const discoveryEnabled = document.getElementById('settings-discovery-enabled').checked;
  const octetRaw = document.getElementById('settings-bridge-octet').value.trim();

  const saves = [api('PUT', '/v1/settings/tofu', { parallelism })];
  saves.push(api('PUT', '/v1/settings/discovery', { enabled: discoveryEnabled }));
  if (octetRaw !== '') {
    saves.push(api('PUT', '/v1/settings/network', { bridge_subnet_octet: Number(octetRaw) }));
  }

  Promise.all(saves).then(([tofu]) => {
    document.getElementById('settings-tofu-parallelism').value = tofu.parallelism ?? '';
    toast('Settings saved', 'success');
  }).catch(e => toast(`Failed to save settings: ${e.message}`, 'error'));
}
