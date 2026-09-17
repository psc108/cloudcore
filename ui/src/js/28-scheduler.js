// ── Scheduler ────────────────────────────────────────────────────────────────
// var, not let — see 26-peers.js's own comment on why (ui/build.sh
// concatenation order + 15-init.js's early showSection() call means a
// `let` here would hit the same temporal-dead-zone bug found live in
// F-089).
var _schedPollTimer = null;
var _schedTemplates = [];
var _schedCurrentSchema = null;

function startSchedulerPoll() {
  if (_schedPollTimer) return;
  _schedPollTimer = setInterval(loadScheduler, 15000);
}
function stopSchedulerPoll() {
  clearInterval(_schedPollTimer);
  _schedPollTimer = null;
}

async function loadScheduler() {
  const tbody = document.getElementById('sched-tbody');
  try {
    const data = await api('GET', '/v1/schedules');
    if (!data.items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No schedules yet.</td></tr>';
    } else {
      tbody.innerHTML = data.items.map(s => `
        <tr>
          <td><strong>${_esc(s.name)}</strong></td>
          <td>${s.kind === 'llm_ingest' ? '7B LLM Ingest' : `Build (${_esc(s.engine)}: ${_esc(s.template)})`}</td>
          <td class="mono">${s.next_run_at ? new Date(s.next_run_at).toLocaleString() : '—'}</td>
          <td>${s.last_run_at ? new Date(s.last_run_at).toLocaleString() + ' ' + badge(s.last_status || 'pending') : '—'}</td>
          <td>${s.enabled ? '✅' : '⏸️'}</td>
          <td>
            <button class="btn btn-sm" onclick="schedRunNow('${s.id}')">Run Now</button>
            <button class="btn btn-sm" onclick="schedShowRuns('${s.id}','${_esc(s.name)}')">History</button>
            <button class="btn btn-danger btn-sm" onclick="schedDelete('${s.id}','${_esc(s.name)}')">Delete</button>
          </td>
        </tr>`).join('');
    }
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="6">Error: ${e.message}</td></tr>`;
  }
  if (!_schedTemplates.length) await _schedLoadTemplates();
}

function _schedOnKindChange() {
  const kind = document.getElementById('sched-kind').value;
  document.getElementById('sched-build-fields').style.display = kind === 'build' ? '' : 'none';
  document.getElementById('sched-llm-fields').style.display = kind === 'llm_ingest' ? '' : 'none';
  if (kind === 'llm_ingest') _schedLoadPeerPicker();
}

// Auto-populates the worker-peer candidate pool from the same
// traffic-light verdict the Resource Placement page's Capacity card
// and the Build Manager's peer_id auto-fill already use — per direct
// request: "when setting the schedule for llm wakeup we should expect
// the peers list to auto populate based on the traffic lights." Every
// approved peer is listed and selectable (never hidden — "leave alone"
// is a default, not a lockout), only green ones start checked.
async function _schedLoadPeerPicker() {
  const tbody = document.getElementById('sched-peer-picker-tbody');
  tbody.innerHTML = '<tr class="empty-row"><td colspan="3">Loading…</td></tr>';
  try {
    const [peersData, rec] = await Promise.all([
      api('GET', '/v1/peers?status=approved'),
      api('GET', '/v1/peers/recommend-placement'),
    ]);
    const verdictByPeer = {};
    (rec.hosts || []).forEach(h => { if (h.peer_id) verdictByPeer[h.peer_id] = h.verdict; });
    const peers = (peersData.items || []).filter(p => p.status === 'approved');
    if (!peers.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="3">No approved peers — pair with one on the Peers page first.</td></tr>';
      return;
    }
    tbody.innerHTML = peers.map(p => {
      const verdict = verdictByPeer[p.id];
      const checked = verdict === 'active' ? 'checked' : '';
      const badgeHtml = verdict && _PLACEMENT_VERDICTS[verdict]
        ? `<span class="badge badge-${verdict}">${_PLACEMENT_VERDICTS[verdict].dot} ${_PLACEMENT_VERDICTS[verdict].label}</span>`
        : '<span class="badge">⚪ Unknown — can\'t assess right now</span>';
      return `<tr>
        <td><input type="checkbox" class="sched-peer-cb" value="${p.id}" ${checked}></td>
        <td>${_esc(p.hostname)}</td>
        <td>${badgeHtml}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="3">Error: ${e.message}</td></tr>`;
  }
}

// Resolves one peer's own vpc/subnet/security-group (first match, same
// "auto-select the first/only entry" convention the Build Manager's own
// peer cascade already uses — see _bmCascadeFromPeer/_bmCascadeFromVpc
// in 16-build-manager.js) — done once at pool-creation time, since
// which *network scaffolding* a peer has doesn't change with load the
// way whether it's actually USED each cycle does (that part is decided
// fresh every wakeup, server-side, by api/scheduler.py).
async function _schedResolvePeerPlacement(peerId) {
  const [vpcData, sgData] = await Promise.all([
    api('GET', `/v1/peers/${peerId}/vpcs`),
    api('GET', `/v1/peers/${peerId}/security-groups`),
  ]);
  const vpc = (vpcData.items || [])[0];
  if (!vpc) return null;
  const subnetData = await api('GET', `/v1/peers/${peerId}/subnets?vpc_id=${encodeURIComponent(vpc.id)}`);
  const subnet = (subnetData.items || [])[0];
  const sg = (sgData.items || []).find(s => s.vpc_id === vpc.id);
  if (!subnet || !sg) return null;
  return { peer_id: peerId, peer_vpc_id: vpc.id, peer_subnet_id: subnet.id, peer_security_group_id: sg.id };
}

function _schedOnModeChange() {
  const mode = document.getElementById('sched-mode').value;
  document.getElementById('sched-field-date').style.display = mode === 'once' ? '' : 'none';
  document.getElementById('sched-field-weekday').style.display = mode === 'weekly' ? '' : 'none';
  const isTimeOfDay = mode === 'once' || mode === 'daily' || mode === 'weekly';
  document.getElementById('sched-field-hour').style.display = isTimeOfDay ? '' : 'none';
  document.getElementById('sched-field-minute').style.display = isTimeOfDay ? '' : 'none';
  const isInterval = mode === 'interval_minutes' || mode === 'interval_hours';
  document.getElementById('sched-field-n').style.display = isInterval ? '' : 'none';
  document.getElementById('sched-n-label').textContent = mode === 'interval_minutes' ? 'Minutes' : 'Hours';
}

async function _schedLoadTemplates() {
  const sel = document.getElementById('sched-template');
  const engine = document.getElementById('sched-engine').value;
  try {
    if (!_schedTemplates.length) {
      const data = await api('GET', '/v1/schedules/templates');
      _schedTemplates = data.items;
    }
    const items = _schedTemplates.filter(t => t.engine === engine);
    sel.innerHTML = items.map(t => `<option value="${t.filename}">${_esc(t.title)}</option>`).join('');
    await _schedOnTemplateChange();
  } catch (e) {
    sel.innerHTML = `<option>Error: ${e.message}</option>`;
  }
}

async function _schedOnTemplateChange() {
  const engine = document.getElementById('sched-engine').value;
  const filename = document.getElementById('sched-template').value;
  const form = document.getElementById('sched-var-form');
  if (!filename) { form.innerHTML = ''; return; }
  try {
    const path = engine === 'tofu'
      ? `/v1/tofu/templates/${filename}/vars`
      : `/v1/builds/templates/${filename}/vars`;
    const schema = await api('GET', path);
    _schedCurrentSchema = schema;
    form.innerHTML = Object.entries(schema).map(([key, meta]) => `
      <div class="field">
        <label>${_esc(key)}${meta.derived ? ' <span class="text-muted">(derived)</span>' : ''}</label>
        <input id="sched-var-${_esc(key)}" type="text" value="${_esc(String(meta.default ?? ''))}">
      </div>`).join('');
  } catch (e) {
    form.innerHTML = `<p class="text-muted">Error loading variables: ${e.message}</p>`;
  }
}

function _schedBuildRecurrence() {
  const mode = document.getElementById('sched-mode').value;
  if (mode === 'once') {
    const date = document.getElementById('sched-date').value;
    const hour = document.getElementById('sched-hour').value;
    const minute = document.getElementById('sched-minute').value;
    if (!date) throw new Error('Pick a date for a one-off schedule.');
    const run_at = `${date}T${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:00`;
    return { mode: 'once', run_at };
  }
  if (mode === 'daily') {
    return { mode: 'daily', hour: Number(document.getElementById('sched-hour').value),
              minute: Number(document.getElementById('sched-minute').value) };
  }
  if (mode === 'weekly') {
    return { mode: 'weekly', hour: Number(document.getElementById('sched-hour').value),
              minute: Number(document.getElementById('sched-minute').value),
              weekday: Number(document.getElementById('sched-weekday').value) };
  }
  if (mode === 'interval_minutes') {
    return { mode: 'interval_minutes', minutes: Number(document.getElementById('sched-n').value) };
  }
  return { mode: 'interval_hours', hours: Number(document.getElementById('sched-n').value) };
}

async function schedCreate() {
  const name = document.getElementById('sched-name').value.trim();
  const kind = document.getElementById('sched-kind').value;
  if (!name) { toast('Name is required', 'error'); return; }

  let recurrence;
  try {
    recurrence = _schedBuildRecurrence();
  } catch (e) {
    toast(e.message, 'error');
    return;
  }

  const payload = { name, kind, recurrence };

  if (kind === 'build') {
    payload.engine = document.getElementById('sched-engine').value;
    payload.template = document.getElementById('sched-template').value;
    const var_overrides = {};
    if (_schedCurrentSchema) {
      for (const key of Object.keys(_schedCurrentSchema)) {
        const el = document.getElementById(`sched-var-${key}`);
        if (el && el.value !== '') var_overrides[key] = el.value;
      }
    }
    payload.var_overrides = var_overrides;
  } else {
    const checked = Array.from(document.querySelectorAll('.sched-peer-cb:checked')).map(cb => cb.value);
    if (!checked.length) {
      toast('Select at least one peer for the worker pool (green ones are pre-checked).', 'error');
      return;
    }
    let pool;
    try {
      pool = (await Promise.all(checked.map(_schedResolvePeerPlacement))).filter(Boolean);
    } catch (e) {
      toast('Failed to resolve peer network details: ' + e.message, 'error');
      return;
    }
    if (!pool.length) {
      toast('Could not resolve a vpc/subnet/security-group on any selected peer — check that peer\'s own catalogue on the Resource Placement page.', 'error');
      return;
    }
    payload.var_overrides = { worker_peer_pool: pool };
  }

  try {
    await api('POST', '/v1/schedules', payload);
    toast('Schedule created', 'success');
    document.getElementById('sched-name').value = '';
    loadScheduler();
  } catch (e) {
    toast('Failed to create schedule: ' + e.message, 'error');
  }
}

async function schedRunNow(id) {
  try {
    await api('POST', `/v1/schedules/${id}/run-now`);
    toast('Triggered', 'success');
    loadScheduler();
  } catch (e) {
    toast('Failed: ' + e.message, 'error');
  }
}

async function schedDelete(id, name) {
  if (!confirm(`Delete schedule "${name}"? This does not affect anything it already built.`)) return;
  try {
    await api('DELETE', `/v1/schedules/${id}`);
    toast('Schedule deleted', 'success');
    loadScheduler();
  } catch (e) {
    toast('Failed: ' + e.message, 'error');
  }
}

async function schedShowRuns(id, name) {
  const card = document.getElementById('sched-runs-card');
  const tbody = document.getElementById('sched-runs-tbody');
  document.getElementById('sched-runs-title').textContent = `Run History — ${name}`;
  card.style.display = '';
  tbody.innerHTML = '<tr class="empty-row"><td colspan="3">Loading…</td></tr>';
  try {
    const data = await api('GET', `/v1/schedules/${id}/runs`);
    if (!data.items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="3">No runs yet.</td></tr>';
      return;
    }
    tbody.innerHTML = data.items.map(r => {
      let extra = '';
      if (r.llm_ingestion) {
        const ing = r.llm_ingestion;
        const peers = (ing.peers_synced || []).map(p => `${_esc(p.hostname)}: ${_esc(p.status)}`).join(', ');
        extra = `<div class="text-muted" style="font-size:12px;margin-top:4px">
          Events: ${ing.events_seen} · Findings: ${ing.findings_created} · Suggestions: ${ing.suggestions_created}
          ${peers ? ' · Peers: ' + peers : ''}
        </div>`;
      }
      return `<tr>
        <td class="mono">${new Date(r.started_at).toLocaleString()}</td>
        <td>${badge(r.status)}</td>
        <td>${_esc(r.summary)}${extra}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="3">Error: ${e.message}</td></tr>`;
  }
}
