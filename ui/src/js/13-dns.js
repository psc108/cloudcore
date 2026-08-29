async function loadDNS() {
  const container = document.getElementById('dns-zones-list');
  container.innerHTML = '<div style="color:var(--text-muted);font-size:13px">Loading…</div>';
  try {
    const data = await api('GET', '/v1/dns/zones');
    if (!data.items.length) {
      container.innerHTML = '<div style="color:var(--text-muted);font-size:13px">No zones found.</div>';
      return;
    }
    container.innerHTML = data.items.map(z => `
      <div class="card" style="margin-bottom:16px">
        <div class="card-header" style="margin-bottom:0">
          <div>
            <strong>${z.name}</strong>
            ${z.builtin ? '<span class="badge badge-active" style="margin-left:8px">built-in</span>' : ''}
            <span class="text-muted" style="font-size:12px;margin-left:10px">${z.record_count} record${z.record_count !== 1 ? 's' : ''}</span>
          </div>
          <div style="display:flex;gap:8px">
            <button class="btn btn-ghost btn-sm" onclick="toggleDNSZone('${z.name}')">Records ▾</button>
            ${z.builtin ? '' : `<button class="btn btn-danger btn-sm" onclick="deleteDNSZone('${z.name}')">Delete</button>`}
          </div>
        </div>
        <div id="dns-zone-${btoa(z.name).replace(/=/g,'_')}" style="display:none;margin-top:16px">
          <div id="dns-records-${btoa(z.name).replace(/=/g,'_')}"><span style="color:var(--text-muted);font-size:13px">Loading…</span></div>
          <div class="backends-add" style="margin-top:16px">
            <div class="field"><label>Name</label><input id="dr-name-${btoa(z.name).replace(/=/g,'_')}" placeholder="my-host"></div>
            <div class="field" style="flex:0 0 90px">
              <label>Type</label>
              <select id="dr-type-${btoa(z.name).replace(/=/g,'_')}">
                <option>A</option><option>CNAME</option><option>TXT</option>
              </select>
            </div>
            <div class="field"><label>Value</label><input id="dr-value-${btoa(z.name).replace(/=/g,'_')}" placeholder="192.168.100.10"></div>
            <div class="field" style="flex:0 0 80px"><label>TTL</label><input id="dr-ttl-${btoa(z.name).replace(/=/g,'_')}" type="number" value="300"></div>
            <button class="btn btn-primary btn-sm" onclick="createDNSRecord('${z.name}')">Add</button>
          </div>
        </div>
      </div>`).join('');
  } catch(e) {
    container.innerHTML = `<div style="color:var(--danger);font-size:13px">Error: ${e.message}</div>`;
  }
}

async function toggleDNSZone(zoneName) {
  const key = btoa(zoneName).replace(/=/g,'_');
  const panel = document.getElementById(`dns-zone-${key}`);
  if (panel.style.display === 'none') {
    panel.style.display = 'block';
    await loadDNSRecords(zoneName);
  } else {
    panel.style.display = 'none';
  }
}

async function loadDNSRecords(zoneName) {
  const key = btoa(zoneName).replace(/=/g,'_');
  const el  = document.getElementById(`dns-records-${key}`);
  try {
    const data = await api('GET', `/v1/dns/zones/${encodeURIComponent(zoneName)}/records`);
    if (!data.items.length) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:8px 0">No records.</div>';
      return;
    }
    el.innerHTML = `
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr>
          <th style="text-align:left;padding:6px 10px;color:var(--text-muted);font-weight:500">Name</th>
          <th style="text-align:left;padding:6px 10px;color:var(--text-muted);font-weight:500">FQDN</th>
          <th style="text-align:left;padding:6px 10px;color:var(--text-muted);font-weight:500">Type</th>
          <th style="text-align:left;padding:6px 10px;color:var(--text-muted);font-weight:500">Value</th>
          <th style="text-align:left;padding:6px 10px;color:var(--text-muted);font-weight:500">TTL</th>
          <th style="text-align:left;padding:6px 10px;color:var(--text-muted);font-weight:500">Source</th>
          <th></th>
        </tr></thead>
        <tbody>${data.items.map(r => `
          <tr>
            <td style="padding:7px 10px">${r.name}</td>
            <td style="padding:7px 10px" class="mono text-muted">${r.fqdn}</td>
            <td style="padding:7px 10px"><span class="badge badge-active">${r.type}</span></td>
            <td style="padding:7px 10px" class="mono">${r.value}</td>
            <td style="padding:7px 10px" class="text-muted">${r.ttl}s</td>
            <td style="padding:7px 10px">${r.resource_type !== 'manual'
              ? `<span class="badge badge-pending">${r.resource_type}</span>`
              : '<span class="text-muted">manual</span>'}</td>
            <td style="padding:7px 10px">
              ${r.resource_type === 'manual'
                ? `<button class="btn btn-danger btn-sm" onclick="deleteDNSRecord('${zoneName}','${r.name}','${r.type}')">Delete</button>`
                : '<span class="text-muted" style="font-size:11px">🔒 auto</span>'}
            </td>
          </tr>`).join('')}
        </tbody>
      </table>`;
  } catch(e) {
    el.innerHTML = `<div style="color:var(--danger);font-size:12px">Error: ${e.message}</div>`;
  }
}

async function createDNSZone() {
  const name = document.getElementById('dns-zone-name').value.trim();
  if (!name) { toast('Zone name is required', 'error'); return; }
  try {
    await api('POST', '/v1/dns/zones', { name });
    toast(`Zone "${name}" created`, 'success');
    toggleForm('dns-zone-form');
    document.getElementById('dns-zone-name').value = '';
    loadDNS();
  } catch(e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function deleteDNSZone(name) {
  if (!confirm(`Delete zone "${name}" and all its records?`)) return;
  try {
    await api('DELETE', `/v1/dns/zones/${encodeURIComponent(name)}`);
    toast(`Zone "${name}" deleted`, 'success');
    loadDNS();
  } catch(e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function createDNSRecord(zoneName) {
  const key   = btoa(zoneName).replace(/=/g,'_');
  const name  = document.getElementById(`dr-name-${key}`).value.trim();
  const rtype = document.getElementById(`dr-type-${key}`).value;
  const value = document.getElementById(`dr-value-${key}`).value.trim();
  const ttl   = parseInt(document.getElementById(`dr-ttl-${key}`).value) || 300;
  if (!name || !value) { toast('Name and value are required', 'error'); return; }
  try {
    await api('POST', `/v1/dns/zones/${encodeURIComponent(zoneName)}/records`,
              { name, type: rtype, value, ttl });
    toast(`Record "${name}" added`, 'success');
    document.getElementById(`dr-name-${key}`).value = '';
    document.getElementById(`dr-value-${key}`).value = '';
    loadDNSRecords(zoneName);
    loadDNS();
  } catch(e) { toast(`Failed: ${e.message}`, 'error'); }
}

async function deleteDNSRecord(zoneName, name, rtype) {
  if (!confirm(`Delete record "${name}" (${rtype}) from "${zoneName}"?`)) return;
  try {
    await api('DELETE', `/v1/dns/zones/${encodeURIComponent(zoneName)}/records/${name}/${rtype}`);
    toast('Record deleted', 'success');
    loadDNSRecords(zoneName);
    loadDNS();
  } catch(e) { toast(`Failed: ${e.message}`, 'error'); }
}
