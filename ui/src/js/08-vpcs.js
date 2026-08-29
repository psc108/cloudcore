// ── VPCs ─────────────────────────────────────────────────────────────────────
async function loadVPCs() {
  const tbody = document.getElementById('vpc-tbody');
  tbody.innerHTML = '<tr class="empty-row"><td colspan="7">Loading…</td></tr>';
  try {
    const data  = await api('GET', '/v1/vpcs');
    const items = data.items.filter(v => v.status !== 'deleted');
    if (!items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="8">No VPCs found. Create one above.</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(v => `
      <tr>
        <td class="cb-col"><input type="checkbox" class="row-cb" data-type="vpc" data-id="${v.id}" data-name="${v.name}" onchange="_onRowCbChange('vpc')"></td>
        <td><strong>${v.name}</strong></td>
        <td>${shortId(v.id)}</td>
        <td class="mono">${v.cidr_block}</td>
        <td>${v.dns_support ? '✓' : '—'}</td>
        <td>${badge(v.status)}</td>
        <td>${fmtDate(v.created_at)}</td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteVPC('${v.id}','${v.name}')">Delete</button></td>
      </tr>`).join('');
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="8">Error: ${e.message}</td></tr>`;
  }
}

async function createVPC() {
  const name = document.getElementById('vpc-name').value.trim();
  if (!name) { toast('Name is required', 'error'); return; }
  try {
    await api('POST', '/v1/vpcs', {
      name,
      cidr_block:  document.getElementById('vpc-cidr').value.trim() || '10.0.0.0/16',
      dns_support: document.getElementById('vpc-dns').value === 'true',
      tags:        parseTags(document.getElementById('vpc-tags').value),
    });
    toast(`VPC "${name}" created`, 'success');
    toggleForm('vpc-form');
    document.getElementById('vpc-name').value = '';
    document.getElementById('vpc-tags').value = '';
    loadVPCs();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function deleteVPC(id, name) {
  if (!confirm(`Delete VPC "${name}"?`)) return;
  try {
    await api('DELETE', `/v1/vpcs/${id}`);
    toast(`VPC "${name}" deleted`, 'success');
    loadVPCs();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}
