// ── LLM Performance ──────────────────────────────────────────────────────────
// Per direct request: "we now need a performance page to show how the 7b
// (or any other llm we introduce) is performing." The cluster itself is
// ephemeral (built fresh each llm_ingest wakeup, destroyed after — see
// api/scheduler.py), so there's rarely a live model to watch; this is a
// history of past runs' own timing/token/resource-usage numbers instead,
// captured by the scheduler itself as each cycle actually happened.
//
// Per direct follow-up request: "at the moment we only show ingestion
// performance. i'd like to see llm performance itself as well" — added
// the Live Deployments section below (api/llm_deployments_routes.py) —
// any example's own LLM server self-registers once at its own startup,
// polled live on every load of this page. Distinct data source and
// distinct concern from the ingestion history above; see db.py's own
// llm_deployments table comment for why they're separate tables.
// var, not let — see 28-scheduler.js's own comment on why (ui/build.sh
// concatenation order + 15-init.js's early showSection() call means a
// `let` here would hit the same temporal-dead-zone bug found live in
// F-089).
var _llmPerfPollTimer = null;

function startLlmPerfPoll() {
  if (_llmPerfPollTimer) return;
  // Every 3 minutes, per direct request — long enough that a handful of
  // stale/unreachable deployments polled server-side on every request
  // (see llm_deployments_routes.py's own _poll_live) never adds up to
  // meaningful load, short enough that "live" still means something.
  _llmPerfPollTimer = setInterval(loadLlmPerformance, 180000);
}
function stopLlmPerfPoll() {
  clearInterval(_llmPerfPollTimer);
  _llmPerfPollTimer = null;
}

function _fmtUptime(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function _fmtTps(v) {
  return (v === null || v === undefined) ? '—' : `${v.toFixed(1)} tok/s`;
}

async function loadLlmDeployments() {
  const tbody = document.getElementById('llmdeploy-tbody');
  try {
    const data = await api('GET', '/v1/llm-deployments');
    const items = data.items || [];
    if (!items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="9">No LLM deployments registered yet — an example self-registers the first time its own server starts (e.g. llm-chat\'s coordinator).</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(d => {
      const stats = d.stats || {};
      const statusVal = !d.reachable ? 'down' : 'up';
      const statusLabel = !d.reachable ? 'offline' : 'up';
      return `
      <tr>
        <td>${_esc(d.name)}</td>
        <td>${_esc(d.example)}</td>
        <td><span class="badge badge-${statusVal}">${statusLabel}</span></td>
        <td>${d.reachable ? _esc((stats.model || '').split('/').pop() || '—') : '—'}</td>
        <td>${d.reachable ? (stats.requests_served ?? '—') : '—'}</td>
        <td>${d.reachable ? _fmtTps(stats.avg_tokens_per_second) : '—'}</td>
        <td>${d.reachable ? _fmtTps(stats.last_tokens_per_second) : '—'}</td>
        <td>${d.reachable ? _fmtUptime(stats.uptime_seconds) : '—'}</td>
        <td><button class="btn btn-sm" onclick="_deleteLlmDeployment('${d.id}')">Remove</button></td>
      </tr>`;
    }).join('');
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="9">Error: ${e.message}</td></tr>`;
  }
}

async function _deleteLlmDeployment(id) {
  if (!confirm('Remove this deployment from the registry? A running deployment will just re-register itself on its own next restart.')) return;
  try {
    await api('DELETE', `/v1/llm-deployments/${id}`);
    loadLlmDeployments();
  } catch (e) {
    toast('Remove failed: ' + e.message, 'error');
  }
}

function _perfDuration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

function _perfTokens(item) {
  if (item.completion_tokens === null || item.completion_tokens === undefined) return '—';
  const tps = item.tokens_per_second ? ` (${item.tokens_per_second.toFixed(1)} tok/s)` : '';
  return `${item.completion_tokens} completion${tps}`;
}

let _perfPeerHostnames = {};
async function _perfLoadPeerHostnames() {
  try {
    const data = await api('GET', '/v1/peers');
    _perfPeerHostnames = {};
    (data.items || []).forEach(p => { _perfPeerHostnames[p.id] = p.hostname; });
  } catch (e) { /* best effort — falls back to showing the raw peer id */ }
}

async function loadLlmPerformance() {
  // Two independent data sources (see this file's own top-of-file
  // comment) — run them concurrently and let each fail on its own, so
  // one being slow/erroring never blanks the other's section.
  loadLlmDeployments();
  await _loadIngestionHistory();
}

async function _loadIngestionHistory() {
  const tbody = document.getElementById('llmperf-tbody');
  tbody.innerHTML = '<tr class="empty-row"><td colspan="9">Loading…</td></tr>';
  document.getElementById('llmperf-detail-card').style.display = 'none';
  try {
    await _perfLoadPeerHostnames();
    const data = await api('GET', '/v1/llm-performance');
    const items = data.items || [];
    if (!items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="9">No LLM ingestion runs yet — they appear here once a 7B LLM Sentinel Ingest schedule has fired at least once.</td></tr>';
      document.getElementById('llmperf-latest-card').style.display = 'none';
      return;
    }
    tbody.innerHTML = items.map((item, idx) => `
      <tr>
        <td class="mono">${new Date(item.started_at).toLocaleString()}</td>
        <td>${_esc(item.schedule_name)}</td>
        <td>${badge(item.status)}</td>
        <td>${item.events_seen}</td>
        <td>${_perfDuration(item.cluster_build_seconds)}</td>
        <td>${_perfDuration(item.model_load_seconds)}</td>
        <td>${_perfDuration(item.inference_seconds)}</td>
        <td>${_perfTokens(item)}</td>
        <td><button class="btn btn-sm" onclick="_perfShowDetail(${idx})">Detail</button></td>
      </tr>`).join('');
    _perfLastItems = items;

    // Latest run actually worth reporting on (a build build, not a
    // skipped-nothing-new cycle) — the summary card above the table.
    const latest = items.find(i => i.cluster_build_seconds !== null) || items[0];
    _perfRenderLatest(latest);
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="9">Error: ${e.message}</td></tr>`;
  }
}

let _perfLastItems = [];

function _perfRenderLatest(item) {
  const card = document.getElementById('llmperf-latest-card');
  const body = document.getElementById('llmperf-latest-body');
  if (!item) { card.style.display = 'none'; return; }
  card.style.display = '';
  body.innerHTML = `
    <div class="form-grid">
      <div class="field"><label>Schedule</label><div>${_esc(item.schedule_name)}</div></div>
      <div class="field"><label>Status</label><div>${badge(item.status)}</div></div>
      <div class="field"><label>Cluster Build</label><div>${_perfDuration(item.cluster_build_seconds)}</div></div>
      <div class="field"><label>Model Load</label><div>${_perfDuration(item.model_load_seconds)}</div></div>
      <div class="field"><label>Inference</label><div>${_perfDuration(item.inference_seconds)}</div></div>
      <div class="field"><label>Tokens</label><div>${_perfTokens(item)}</div></div>
    </div>
    <p style="font-size:13px;color:var(--text-muted);margin-top:8px">${_esc(item.summary_text || '')}</p>
  `;
}

function _perfShowDetail(idx) {
  const item = _perfLastItems[idx];
  if (!item) return;
  const card = document.getElementById('llmperf-detail-card');
  const body = document.getElementById('llmperf-detail-body');
  card.style.display = '';

  const workerRows = (item.worker_stats || []).map(w =>
    _placementStatsRow(_perfPeerHostnames[w.peer_id] || w.peer_id, w.stats)).join('');
  const coordRow = Object.keys(item.coordinator_stats || {}).length
    ? _placementStatsRow('Coordinator (this host)', item.coordinator_stats)
    : '';

  const peersSyncedRows = (item.peers_synced || []).map(p =>
    `<tr><td>${_esc(p.hostname)}</td><td>${_esc(p.status)}</td></tr>`).join('')
    || '<tr class="empty-row"><td colspan="2">No peers to distribute to.</td></tr>';

  body.innerHTML = `
    <h3 class="about-heading">Resource Usage During This Run</h3>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Host</th><th>CPU</th><th>Memory</th><th>Disk</th><th>Instances</th><th>Placement</th></tr></thead>
        <tbody>${coordRow}${workerRows || '<tr class="empty-row"><td colspan="6">No worker snapshot recorded for this run.</td></tr>'}</tbody>
      </table>
    </div>
    <h3 class="about-heading" style="margin-top:20px">Findings/Suggestions Distributed to Peers</h3>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Peer</th><th>Result</th></tr></thead>
        <tbody>${peersSyncedRows}</tbody>
      </table>
    </div>
    <h3 class="about-heading" style="margin-top:20px">Model Summary</h3>
    <p style="font-size:13px">${_esc(item.summary_text || '—')}</p>
  `;
}
