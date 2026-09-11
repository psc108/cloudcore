// ── Settings ─────────────────────────────────────────────────────────────────
function loadSettings() {
  api('GET', '/v1/settings/tofu').then(s => {
    document.getElementById('settings-tofu-parallelism').value = s.parallelism ?? '';
  }).catch(() => toast('Failed to load settings', 'error'));
}

function saveSettings() {
  const raw = document.getElementById('settings-tofu-parallelism').value.trim();
  const parallelism = raw === '' ? null : Number(raw);
  api('PUT', '/v1/settings/tofu', { parallelism }).then(s => {
    document.getElementById('settings-tofu-parallelism').value = s.parallelism ?? '';
    toast('Settings saved', 'success');
  }).catch(e => toast(`Failed to save settings: ${e.message}`, 'error'));
}
