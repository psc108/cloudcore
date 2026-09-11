// ── Subnets ──────────────────────────────────────────────────────────────────
async function loadSubnets() {
  const tbody = document.getElementById('subnet-tbody');
  tbody.innerHTML = '<tr class="empty-row"><td colspan="9">Loading…</td></tr>';
  try {
    const [subnetData, vpcData] = await Promise.all([
      api('GET', '/v1/subnets'),
      api('GET', '/v1/vpcs'),
    ]);
    const vpcById = {};
    vpcData.items.forEach(v => vpcById[v.id] = v);

    const items = subnetData.items.filter(s => s.status !== 'deleted');
    if (!items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="9">No subnets found.</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(s => {
      const vpc = vpcById[s.vpc_id];
      const vpcCell = vpc
        ? `${vpc.name} <span class="text-muted">(${shortId(vpc.id)})</span>`
        : `<span style="color:var(--danger)" title="No VPC with this ID exists — this subnet is orphaned, left behind by a failed or partial build">orphaned <span class="mono">(${s.vpc_id})</span></span>`;
      return `
      <tr>
        <td class="cb-col"><input type="checkbox" class="row-cb" data-type="subnet" data-id="${s.id}" data-name="${s.name}" onchange="_onRowCbChange('subnet')"></td>
        <td><strong>${s.name}</strong></td>
        <td>${shortId(s.id)}</td>
        <td>${vpcCell}</td>
        <td class="mono">${s.cidr_block}</td>
        <td>${s.zone}</td>
        <td>${s.public ? '✓' : '—'}</td>
        <td>${badge(s.status)}</td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteSubnet('${s.id}','${s.name}')">Delete</button></td>
      </tr>`;
    }).join('');
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="9">Error: ${e.message}</td></tr>`;
  }
}

async function _populateSubnetVpcSelect() {
  const sel = document.getElementById('subnet-vpc');
  try {
    const data = await api('GET', '/v1/vpcs');
    const items = data.items.filter(v => v.status !== 'deleted');
    sel.innerHTML = items.length
      ? items.map(v => `<option value="${v.id}">${v.name} (${v.cidr_block})</option>`).join('')
      : '<option value="">No VPCs available — create one first</option>';
  } catch (e) {
    sel.innerHTML = '<option value="">Failed to load VPCs</option>';
  }
}

async function createSubnet() {
  const name = document.getElementById('subnet-name').value.trim();
  const vpc_id = document.getElementById('subnet-vpc').value;
  const cidr_block = document.getElementById('subnet-cidr').value.trim();
  if (!name || !vpc_id || !cidr_block) { toast('Name, VPC, and CIDR are required', 'error'); return; }
  try {
    await api('POST', '/v1/subnets', {
      name, vpc_id, cidr_block,
      zone:   document.getElementById('subnet-zone').value.trim() || 'a',
      public: document.getElementById('subnet-public').value === 'true',
    });
    toast(`Subnet "${name}" created`, 'success');
    toggleForm('subnet-form');
    document.getElementById('subnet-name').value = '';
    document.getElementById('subnet-cidr').value = '';
    loadSubnets();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function deleteSubnet(id, name) {
  if (!confirm(`Delete subnet "${name}"?`)) return;
  try {
    await api('DELETE', `/v1/subnets/${id}`);
    toast(`Subnet "${name}" deleted`, 'success');
    loadSubnets();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}
