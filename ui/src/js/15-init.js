// ── Init ──────────────────────────────────────────────────────────────────────
checkHealth();
showSection('dashboard', document.getElementById('nav-dashboard'));
setInterval(checkHealth, 30000);
