// ── Settings ─────────────────────────────────────────────────────────────────

// Direct request: "if a peer has elected to be a peer (via settings)
// should the pre-requisite infrastructure be built to support peer
// activities?" — deliberately NOT auto-created on toggling the
// checkbox itself (peering just means "findable"; it says nothing
// about whether this host wants a VPC with some CIDR/security posture
// picked for it, and a silently-created one could collide with an
// operator's own deliberate VPC plan). Instead: a real, recurring gap
// found live (F-151's own investigation) — a peer that used to have
// worker/coordinator-placeable infrastructure can legitimately lose
// it (an orphaned-resource cleanup that also removed the VPC), and
// nothing ever told the operator their "findable" host had quietly
// stopped being usable. This surfaces that state plainly and offers
// a one-click, clearly-labeled fix instead of a silent gap that only
// shows up as a confusing build-time error days later.
const _SETTINGS_PEER_VPC_NAME = 'peer-workloads';

async function _settingsRefreshPeerInfraStatus() {
  const box = document.getElementById('settings-discovery-enabled');
  const status = document.getElementById('settings-peer-infra-status');
  if (!box || !status) return;
  if (!box.checked) { status.style.display = 'none'; return; }

  status.style.display = 'block';
  status.innerHTML = '<span class="bm-field-hint">Checking for existing peer infrastructure…</span>';
  let hasVpc = false;
  try {
    const data = await api('GET', '/v1/vpcs');
    hasVpc = (data.items || []).some(v => v.name === _SETTINGS_PEER_VPC_NAME);
  } catch (e) { /* treat as "unknown" below, not "ready" */ }

  status.innerHTML = hasVpc
    ? `<span class="bm-field-hint">✓ Peer infrastructure ready ("${_SETTINGS_PEER_VPC_NAME}" VPC/subnet/security group exist) — this host can receive a peer-placed worker, coordinator, or similar today.</span>`
    : `<span class="bm-field-hint bm-required">⚠ No peer infrastructure yet — a build that tries to place something on this host will fail to find a VPC/subnet/security group for it.</span>
       <button type="button" class="btn btn-ghost btn-sm" style="margin-top:6px" onclick="settingsCreatePeerInfra()">Create infrastructure</button>`;
}

async function settingsCreatePeerInfra() {
  if (!confirm(
    `Create a default VPC/subnet/security group ("${_SETTINGS_PEER_VPC_NAME}") on this host so a peer can place workloads here?\n\n` +
    'VPC 10.10.0.0/16, subnet 10.10.1.0/24, and a security group allowing traffic within that VPC only ' +
    '(nothing opened to the wider network). You can adjust or add to this later under Cloud → VPCs.'
  )) return;

  try {
    const vpc = await api('POST', '/v1/vpcs', { name: _SETTINGS_PEER_VPC_NAME, cidr_block: '10.10.0.0/16' });
    const subnet = await api('POST', '/v1/subnets', {
      name: `${_SETTINGS_PEER_VPC_NAME}-a`, vpc_id: vpc.id, cidr_block: '10.10.1.0/24', public: false, zone: 'a',
    });
    await api('POST', '/v1/security-groups', {
      name: _SETTINGS_PEER_VPC_NAME, vpc_id: vpc.id,
      description: 'Default security group for peer-placed workloads — internal VPC traffic only.',
      ingress_rules: [{ protocol: '-1', cidr: vpc.cidr_block }],
      egress_rules: [{ protocol: '-1', cidr: '0.0.0.0/0' }],
    });
    toast('Peer infrastructure created', 'success');
  } catch (e) {
    toast(`Failed to create peer infrastructure: ${e.message}`, 'error');
  }
  await _settingsRefreshPeerInfraStatus();
}

function loadSettings() {
  Promise.all([
    api('GET', '/v1/settings/tofu'),
    api('GET', '/v1/settings/discovery'),
    api('GET', '/v1/settings/network'),
  ]).then(([tofu, discovery, network]) => {
    document.getElementById('settings-tofu-parallelism').value = tofu.parallelism ?? '';
    const discoveryBox = document.getElementById('settings-discovery-enabled');
    discoveryBox.checked = !!discovery.enabled;
    discoveryBox.onchange = _settingsRefreshPeerInfraStatus;
    document.getElementById('settings-bridge-octet').value = network.bridge_subnet_octet ?? '';
    _settingsRefreshPeerInfraStatus();
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
