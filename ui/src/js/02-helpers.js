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

// SLIRP instances/NFS servers are reachable at 127.0.0.1 via a forwarded
// ssh_port. Bridge-mode ones (ssh_port always 0 — this platform's default
// whenever the bridge is usable, and the only mode ha-frontend-lb and
// friends use) are reached directly at their own private_ip on the
// standard port 22 instead — every call site here used to only handle
// the SLIRP case, showing "—"/"No SSH port" for every bridge-mode
// instance regardless of it being perfectly SSH-reachable.
function sshHasAccess(r) {
  return !!(r.ssh_port || r.private_ip);
}
function sshDisplay(r) {
  if (r.ssh_port) return `127.0.0.1:${r.ssh_port}`;
  if (r.private_ip) return `${r.private_ip}:22`;
  return '—';
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = '✓';
    setTimeout(() => btn.textContent = orig, 1500);
  });
}
