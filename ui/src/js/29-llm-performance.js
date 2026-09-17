// ── LLM Performance ──────────────────────────────────────────────────────────
// Per direct request: "we now need a performance page to show how the 7b
// (or any other llm we introduce) is performing." The cluster itself is
// ephemeral (built fresh each llm_ingest wakeup, destroyed after — see
// api/scheduler.py), so there's rarely a live model to watch; this is a
// history of past runs' own timing/token/resource-usage numbers instead,
// captured by the scheduler itself as each cycle actually happened.

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
