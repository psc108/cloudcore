// ── Navigation ───────────────────────────────────────────────────────────────
const _sectionGroup = {
  vpcs: 'infrastructure', instances: 'infrastructure',
  lbs: 'infrastructure',  terminal: 'infrastructure',
  dns: 'networking',
  builds: 'builds', tofu: 'builds',
};

// Click-toggle dropdowns; close when clicking outside
document.addEventListener('click', e => {
  const grp = e.target.closest('.nav-group');
  document.querySelectorAll('.nav-group').forEach(g => {
    if (g !== grp) g.classList.remove('open');
  });
  if (grp && e.target.classList.contains('nav-group-btn')) {
    grp.classList.toggle('open');
  }
});

function showSection(name, btn) {
  document.querySelectorAll('.nav-group').forEach(g => g.classList.remove('open'));
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-dropdown button, nav > button').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.nav-group-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('section-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
  const grp = _sectionGroup[name];
  if (grp) document.getElementById('nav-grp-' + grp).classList.add('active');
  if (name !== 'instances') stopInstancePoll();
  if (name !== 'dashboard') stopDashPoll();
  if (name === 'dashboard')     { loadDashboard(); startDashPoll(); }
  if (name === 'vpcs')          loadVPCs();
  if (name === 'instances')     { loadInstances(); startInstancePoll(); }
  if (name === 'lbs')           loadLBs();
  if (name === 'terminal')      loadTerminalInstances();
  if (name === 'dns')           loadDNS();
  if (name === 'builds')        loadBuildManager();
  if (name === 'tofu')           loadTofuManager();
  if (name === 'editor')         loadEditor();
  if (name === 'about')          loadAbout();
}

function showSectionByName(name) {
  showSection(name, document.getElementById('nav-' + name));
}
