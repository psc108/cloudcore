// ── Security Groups ───────────────────────────────────────────────────────────

async function loadSecurityGroups() {
  const tbody = document.getElementById('sg-tbody');
  tbody.innerHTML = '<tr class="empty-row"><td colspan="7">Loading…</td></tr>';
  try {
    const data  = await api('GET', '/v1/security-groups');
    const items = data.items || [];
    if (!items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No security groups found. Create one above.</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(sg => `
      <tr id="sg-row-${sg.id}">
        <td class="cb-col"><input type="checkbox" class="row-cb" data-type="sg" data-id="${sg.id}" data-name="${sg.name}" onchange="_onRowCbChange('sg')"></td>
        <td><strong>${sg.name}</strong></td>
        <td>${shortId(sg.id)}</td>
        <td class="text-muted" style="font-size:12px">${sg.description || '—'}</td>
        <td class="mono text-muted">${sg.vpc_id ? sg.vpc_id.slice(0,8) + '…' : '—'}</td>
        <td>
          <button class="expand-btn" onclick="toggleSGRules('${sg.id}')">
            ${sg.ingress_rules.length}↓ ${sg.egress_rules.length}↑ rules ▾
          </button>
        </td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteSG('${sg.id}','${sg.name}')">Delete</button></td>
      </tr>
      <tr id="sg-rules-${sg.id}" class="backends-row" style="display:none">
        <td colspan="7">
          <div class="backends-panel">
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
              <div>
                <div style="font-size:12px;font-weight:600;color:var(--text-muted);margin-bottom:8px">INBOUND RULES</div>
                ${_renderRuleTable(sg, 'ingress')}
              </div>
              <div>
                <div style="font-size:12px;font-weight:600;color:var(--text-muted);margin-bottom:8px">OUTBOUND RULES</div>
                ${_renderRuleTable(sg, 'egress')}
              </div>
            </div>
            <div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border)">
              <div style="font-size:12px;font-weight:600;color:var(--text-muted);margin-bottom:8px">ADD RULE</div>
              <div class="backends-add">
                <div class="field" style="flex:0 0 90px">
                  <label>Direction</label>
                  <select id="sr-dir-${sg.id}"><option value="ingress">Inbound</option><option value="egress">Outbound</option></select>
                </div>
                <div class="field" style="flex:0 0 90px">
                  <label>Protocol</label>
                  <select id="sr-proto-${sg.id}" onchange="_sgProtoChange('${sg.id}')">
                    <option value="tcp">TCP</option>
                    <option value="udp">UDP</option>
                    <option value="icmp">ICMP</option>
                    <option value="-1">All traffic</option>
                  </select>
                </div>
                <div class="field" style="flex:0 0 70px" id="sr-fp-wrap-${sg.id}">
                  <label>From port</label>
                  <input id="sr-fp-${sg.id}" type="number" placeholder="22" min="0" max="65535">
                </div>
                <div class="field" style="flex:0 0 70px" id="sr-tp-wrap-${sg.id}">
                  <label>To port</label>
                  <input id="sr-tp-${sg.id}" type="number" placeholder="22" min="0" max="65535">
                </div>
                <div class="field">
                  <label>CIDR</label>
                  <input id="sr-cidr-${sg.id}" placeholder="0.0.0.0/0" value="0.0.0.0/0">
                </div>
                <button class="btn btn-primary btn-sm" onclick="addSGRule('${sg.id}')">Add Rule</button>
              </div>
            </div>
          </div>
        </td>
      </tr>`).join('');
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="7">Error: ${e.message}</td></tr>`;
  }
}

function _renderRuleTable(sg, direction) {
  const rules = direction === 'ingress' ? sg.ingress_rules : sg.egress_rules;
  if (!rules.length) return '<div style="color:var(--text-muted);font-size:12px;padding:4px 0">No rules.</div>';
  return `<table style="width:100%;font-size:12px;border-collapse:collapse">
    <thead><tr>
      <th style="text-align:left;padding:4px 8px;color:var(--text-muted)">Protocol</th>
      <th style="text-align:left;padding:4px 8px;color:var(--text-muted)">Port range</th>
      <th style="text-align:left;padding:4px 8px;color:var(--text-muted)">CIDR</th>
      <th></th>
    </tr></thead>
    <tbody>${rules.map((r, i) => `
      <tr>
        <td style="padding:4px 8px"><span class="badge badge-active">${r.protocol === '-1' ? 'All' : r.protocol.toUpperCase()}</span></td>
        <td style="padding:4px 8px" class="mono">${r.protocol === '-1' ? 'All' : (r.from_port === r.to_port ? r.from_port : r.from_port + '–' + r.to_port)}</td>
        <td style="padding:4px 8px" class="mono">${r.cidr}</td>
        <td style="padding:4px 8px"><button class="btn btn-danger btn-sm" onclick="removeSGRule('${sg.id}','${direction}',${i})">✕</button></td>
      </tr>`).join('')}
    </tbody>
  </table>`;
}

function _sgProtoChange(sgId) {
  const proto = document.getElementById(`sr-proto-${sgId}`).value;
  const hide  = proto === '-1' || proto === 'icmp';
  document.getElementById(`sr-fp-wrap-${sgId}`).style.display = hide ? 'none' : '';
  document.getElementById(`sr-tp-wrap-${sgId}`).style.display = hide ? 'none' : '';
}

function toggleSGRules(sgId) {
  const row = document.getElementById(`sg-rules-${sgId}`);
  row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}

async function addSGRule(sgId) {
  const dir   = document.getElementById(`sr-dir-${sgId}`).value;
  const proto = document.getElementById(`sr-proto-${sgId}`).value;
  const cidr  = document.getElementById(`sr-cidr-${sgId}`).value.trim() || '0.0.0.0/0';
  const fp    = document.getElementById(`sr-fp-${sgId}`)?.value;
  const tp    = document.getElementById(`sr-tp-${sgId}`)?.value;

  const rule = { protocol: proto, cidr };
  if (proto !== '-1' && proto !== 'icmp') {
    if (!fp || !tp) { toast('From port and to port are required', 'error'); return; }
    rule.from_port = parseInt(fp);
    rule.to_port   = parseInt(tp);
  }

  try {
    const current = await api('GET', `/v1/security-groups/${sgId}`);
    const body = {};
    if (dir === 'ingress') {
      body.ingress_rules = [...current.ingress_rules, rule];
      body.egress_rules  = current.egress_rules;
    } else {
      body.ingress_rules = current.ingress_rules;
      body.egress_rules  = [...current.egress_rules, rule];
    }
    await api('PUT', `/v1/security-groups/${sgId}`, body);
    toast('Rule added', 'success');
    loadSecurityGroups();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function removeSGRule(sgId, direction, index) {
  try {
    const current = await api('GET', `/v1/security-groups/${sgId}`);
    const body = {
      ingress_rules: [...current.ingress_rules],
      egress_rules:  [...current.egress_rules],
    };
    body[direction === 'ingress' ? 'ingress_rules' : 'egress_rules'].splice(index, 1);
    await api('PUT', `/v1/security-groups/${sgId}`, body);
    toast('Rule removed', 'success');
    loadSecurityGroups();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function populateSGForm() {
  const vpcSel = document.getElementById('sg-vpc');
  vpcSel.innerHTML = '<option value="">Loading…</option>';
  try {
    const data   = await api('GET', '/v1/vpcs');
    const active = data.items.filter(v => v.status !== 'deleted');
    vpcSel.innerHTML = active.length
      ? active.map(v => `<option value="${v.id}">${v.name}</option>`).join('')
      : '<option value="">No VPCs — create one first</option>';
  } catch {
    vpcSel.innerHTML = '<option value="">Error loading VPCs</option>';
  }
}

function toggleSGForm() {
  const form    = document.getElementById('sg-form');
  const visible = form.style.display !== 'none';
  form.style.display = visible ? 'none' : 'block';
  if (!visible) populateSGForm();
}

async function createSG() {
  const name  = document.getElementById('sg-name').value.trim();
  const vpcId = document.getElementById('sg-vpc').value;
  const desc  = document.getElementById('sg-desc').value.trim();
  if (!name)  { toast('Name is required', 'error'); return; }
  if (!vpcId) { toast('VPC is required', 'error'); return; }
  try {
    await api('POST', '/v1/security-groups', {
      name, vpc_id: vpcId, description: desc,
      ingress_rules: [], egress_rules: [],
      tags: parseTags(document.getElementById('sg-tags').value),
    });
    toast(`Security group "${name}" created`, 'success');
    document.getElementById('sg-form').style.display = 'none';
    document.getElementById('sg-name').value = '';
    document.getElementById('sg-desc').value = '';
    document.getElementById('sg-tags').value = '';
    loadSecurityGroups();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function deleteSG(id, name) {
  if (!confirm(`Delete security group "${name}"?`)) return;
  try {
    await api('DELETE', `/v1/security-groups/${id}`);
    toast(`Security group "${name}" deleted`, 'success');
    loadSecurityGroups();
  } catch (e) { toast(`Failed: ${e.message}`, 'error'); }
}
