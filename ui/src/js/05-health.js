// ── API health ────────────────────────────────────────────────────────────────
async function checkHealth() {
  const dot   = document.getElementById('api-dot');
  const label = document.getElementById('api-label');
  try {
    await api('GET', '/v1/vpcs');
    dot.className     = 'status-dot ok';
    label.textContent = 'API connected';
  } catch {
    dot.className     = 'status-dot err';
    label.textContent = 'API unreachable';
  }
}
