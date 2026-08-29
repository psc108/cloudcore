// ── About ─────────────────────────────────────────────────────────────────────
function loadAbout() {
  api('GET', '/v1/about').then(data => {
    _aboutSection('about-cloudcore', data.cloudcore);
    _aboutSection('about-runtime',   data.runtime);
    _aboutSection('about-system',    data.system);
  }).catch(() => {
    ['about-cloudcore','about-runtime','about-system'].forEach(id => {
      document.getElementById(id).innerHTML = '<tr><td colspan="2" class="empty-row">Failed to load</td></tr>';
    });
  });
}

function _aboutSection(tbodyId, obj) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  tbody.innerHTML = Object.entries(obj).map(([k, v]) =>
    `<tr><td class="about-key">${_aboutLabel(k)}</td><td class="about-val">${v}</td></tr>`
  ).join('');
}

function _aboutLabel(key) {
  const labels = {
    api: 'API', collection: 'Ansible Collection',
    python: 'Python', flask: 'Flask', libvirt: 'libvirt-python',
    paramiko: 'Paramiko', websockets: 'websockets', pyyaml: 'PyYAML',
    ansible: 'Ansible', qemu_img: 'QEMU / qemu-img',
    haproxy: 'HAProxy', dnsmasq: 'dnsmasq',
  };
  return labels[key] || key;
}
