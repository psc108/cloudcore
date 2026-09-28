// ── LLM Examples ──────────────────────────────────────────────────────────────
// Phase 3 of the llm-chat grounded-verification work — every real
// prompt -> code -> execution -> (fix) -> re-execution transaction
// captured by the coordinator's own verify-proxy.service
// (api/llm_examples_routes.py, api/llm_examples_store.py). Capture is
// unconditional (this table doubles as the exportable training
// corpus); `status` here controls only what the separate, unauthenticated
// student review page (served at GET /examples on the chat deployment's
// own URL) shows — publishing here is a curation decision, not a
// capture one.

let _llmExItems = [];
let _llmTokLabels = {};   // client_token_id -> label, for local-client rows

function _llmExPassBadge(passed) {
  return passed ? '<span class="badge badge-active">passed</span>' : '<span class="badge badge-error">failed</span>';
}

async function loadLlmExamples() {
  const tbody = document.getElementById('llmex-tbody');
  tbody.innerHTML = '<tr class="empty-row"><td colspan="7">Loading…</td></tr>';
  document.getElementById('llmex-detail-card').style.display = 'none';
  try {
    const [data, toks] = await Promise.all([
      api('GET', '/v1/llm-chat/examples'),
      api('GET', '/v1/llm-chat/client-tokens').catch(() => ({ items: [] })),
    ]);
    _llmTokLabels = Object.fromEntries((toks.items || []).map(t => [t.id, t.label]));
    const sourceFilter = document.getElementById('llmex-source-filter')?.value || '';
    _llmExItems = (data.items || []).filter(it => !sourceFilter || it.source === sourceFilter);
    if (!_llmExItems.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No examples captured yet — they appear here once a student\'s chat session produces a fenced code block that gets sandboxed-executed.</td></tr>';
      return;
    }
    tbody.innerHTML = _llmExItems.map((item, idx) => `
      <tr>
        <td class="mono">${fmtDate(item.created_at)}</td>
        <td><span class="mono">${_esc(item.language || 'python')}</span> · ${_esc(item.model_filename)}${_llmExSourceTag(item)}</td>
        <td>${_llmExPassBadge(item.passed)}</td>
        <td>${item.fix_explanation ? _llmExPassBadge(item.fix_passed) : '—'}</td>
        <td>${badge(item.status)}</td>
        <td class="mono" title="${_esc(item.prompt)}">${_esc((item.prompt || '').slice(0, 60))}${(item.prompt || '').length > 60 ? '…' : ''}</td>
        <td>
          <button class="btn btn-sm" onclick="_llmExShowDetail(${idx})">View</button>
          ${item.status !== 'published' ? `<button class="btn btn-sm" onclick="_llmExSetStatus('${item.id}','published')">Publish</button>` : ''}
          ${item.status !== 'hidden' ? `<button class="btn btn-sm" onclick="_llmExSetStatus('${item.id}','hidden')">Hide</button>` : ''}
        </td>
      </tr>`).join('');
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="7">Error: ${e.message}</td></tr>`;
  }
}

function _llmExSourceTag(item) {
  if (item.source !== 'local-client') return '';
  const who = _llmTokLabels[item.client_token_id] || 'unknown token';
  return `<br><span class="badge" title="Submitted by the local-capture client; re-run on a coordinator">local · ${_esc(who)}</span>`;
}

async function loadLlmClientTokens() {
  const tbody = document.getElementById('llmtok-tbody');
  try {
    const data = await api('GET', '/v1/llm-chat/client-tokens');
    const items = data.items || [];
    if (!items.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No capture tokens yet.</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(t => `
      <tr>
        <td>${_esc(t.label)}</td>
        <td class="mono">${fmtDate(t.created_at)}</td>
        <td class="mono">${t.last_used_at ? fmtDate(t.last_used_at) : '—'}</td>
        <td>${t.submissions}</td>
        <td>${t.revoked_at ? '<span class="badge badge-error">revoked</span>' : '<span class="badge badge-active">active</span>'}</td>
        <td>${t.revoked_at ? '' : `<button class="btn btn-sm" onclick="_llmTokRevoke('${t.id}')">Revoke</button>`}</td>
      </tr>`).join('');
  } catch (e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="6">Error: ${_esc(e.message)}</td></tr>`;
  }
}

async function _llmTokCreate() {
  const input = document.getElementById('llmtok-label');
  const label = input.value.trim();
  if (!label) { input.focus(); return; }
  try {
    const t = await api('POST', '/v1/llm-chat/client-tokens', { label });
    input.value = '';
    const box = document.getElementById('llmtok-new');
    box.style.display = '';
    box.innerHTML = `<div class="field"><label>Token for ${_esc(t.label)} — copy it now, it will not be shown again</label>
      <pre class="mono" style="white-space:pre-wrap;word-break:break-all;background:var(--bg-alt);padding:8px;border-radius:4px">${_esc(t.token)}</pre></div>`;
    await loadLlmClientTokens();
  } catch (e) {
    alert(`Failed to create token: ${e.message}`);
  }
}

async function _llmTokRevoke(id) {
  if (!confirm('Revoke this capture token? The student\'s client stops working immediately.')) return;
  try {
    await api('DELETE', `/v1/llm-chat/client-tokens/${id}`);
    await loadLlmClientTokens();
  } catch (e) {
    alert(`Failed to revoke: ${e.message}`);
  }
}

async function _llmExSetStatus(id, status) {
  try {
    await api('PUT', `/v1/llm-chat/examples/${id}`, { status });
    await loadLlmExamples();
  } catch (e) {
    alert(`Failed to update status: ${e.message}`);
  }
}

function _llmExBlock(label, text) {
  if (!text || !text.trim()) return '';
  return `<h4 style="margin:10px 0 4px">${_esc(label)}</h4><pre class="mono" style="white-space:pre-wrap;background:var(--bg-alt);padding:8px;border-radius:4px">${_esc(text)}</pre>`;
}

function _llmExShowDetail(idx) {
  const item = _llmExItems[idx];
  if (!item) return;
  const card = document.getElementById('llmex-detail-card');
  const body = document.getElementById('llmex-detail-body');
  card.style.display = '';
  body.innerHTML = `
    <div class="form-grid">
      <div class="field"><label>Model</label><div>${_esc(item.model_filename)}</div></div>
      <div class="field"><label>Status</label><div>${badge(item.status)}</div></div>
      <div class="field"><label>Initial result</label><div>${_llmExPassBadge(item.passed)}</div></div>
      ${item.fix_explanation ? `<div class="field"><label>Fix result</label><div>${_llmExPassBadge(item.fix_passed)}</div></div>` : ''}
    </div>
    ${_llmExBlock('Prompt', item.prompt)}
    ${_llmExBlock('Generated code', item.generated_code)}
    ${_llmExBlock('Actually executed — stdout', item.exec_stdout)}
    ${_llmExBlock('Actually executed — stderr', item.exec_stderr)}
    ${_llmExBlock('Grounded explanation + fix', item.fix_explanation)}
    ${_llmExBlock('Fixed code', item.fixed_code)}
    ${_llmExBlock('Re-execution — stdout', item.fix_exec_stdout)}
    ${_llmExBlock('Re-execution — stderr', item.fix_exec_stderr)}
  `;
}

async function _llmExExport() {
  // Not api() -- that parses JSON and this is an authenticated ndjson
  // file download; fetch + blob + a throwaway anchor is the standard
  // vanilla-JS way to carry the Authorization header through what
  // would otherwise be a plain, auth-less <a href> download.
  try {
    const res = await fetch(API_BASE + '/v1/llm-chat/examples/export', {
      headers: { 'Authorization': `Bearer ${API_TOKEN}` },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'llm-verification-examples.jsonl';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert(`Export failed: ${e.message}`);
  }
}
