// ── Helpers ──────────────────────────────────────────────────────────────────
function parseTags(str) {
  const tags = {};
  if (!str.trim()) return tags;
  str.split(',').forEach(pair => {
    const [k, ...v] = pair.split('=');
    if (k.trim()) tags[k.trim()] = v.join('=').trim();
  });
  return tags;
}

function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' });
}

function badge(status) {
  return `<span class="badge badge-${status}">${status}</span>`;
}

function shortId(id) {
  return `<span class="mono text-muted" title="${id}">${id.slice(0, 8)}…</span>`;
}

function toggleForm(id) {
  const el = document.getElementById(id);
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = '✓';
    setTimeout(() => btn.textContent = orig, 1500);
  });
}
