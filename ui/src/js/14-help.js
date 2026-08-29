// ── Help ─────────────────────────────────────────────────────────────────────
let _helpLoaded = false;

async function openHelp() {
  document.getElementById('help-modal').style.display = 'block';
  if (_helpLoaded) return;
  try {
    const res = await fetch(API_BASE + '/help', { headers: { 'Authorization': `Bearer ${API_TOKEN}` } });
    const md  = await res.text();
    document.getElementById('help-content').innerHTML = mdToHtml(md);
    _helpLoaded = true;
  } catch (e) {
    document.getElementById('help-content').textContent = 'Failed to load help: ' + e.message;
  }
}

function closeHelp() {
  document.getElementById('help-modal').style.display = 'none';
}

function mdToHtml(md) {
  return md
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/^#### (.+)$/gm, '<h4 style="margin:16px 0 6px;font-size:14px;color:var(--text)">$1</h4>')
    .replace(/^### (.+)$/gm, '<h3 style="margin:20px 0 8px;font-size:15px;color:var(--accent)">$1</h3>')
    .replace(/^## (.+)$/gm, '<h2 style="margin:28px 0 10px;font-size:17px;color:var(--text);border-bottom:1px solid var(--border);padding-bottom:6px">$1</h2>')
    .replace(/^# (.+)$/gm, '<h1 style="margin:0 0 20px;font-size:22px;color:var(--accent)">$1</h1>')
    .replace(/^---$/gm, '<hr style="border:none;border-top:1px solid var(--border);margin:20px 0">')
    .replace(/```([\s\S]*?)```/g, '<pre style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:12px 16px;overflow-x:auto;font-family:monospace;font-size:12px;margin:10px 0"><code>$1</code></pre>')
    .replace(/`([^`]+)`/g, '<code style="background:var(--surface2);padding:1px 5px;border-radius:3px;font-family:monospace;font-size:12px;color:var(--text)">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/^\| (.+) \|$/gm, (_, row) => {
      const cells = row.split(' | ');
      const tag = cells.every(c => /^[-:]+$/.test(c.trim())) ? null : 'td';
      if (!tag) return '';
      return '<tr>' + cells.map(c => `<td style="padding:6px 12px;border-bottom:1px solid var(--border)">${c}</td>`).join('') + '</tr>';
    })
    .replace(/(<tr>.*<\/tr>\n?)+/gs, m => `<table style="width:100%;border-collapse:collapse;margin:12px 0;font-size:13px">${m}</table>`)
    .replace(/^- (.+)$/gm, '<li style="margin:3px 0">$1</li>')
    .replace(/(<li[^>]*>.*<\/li>\n?)+/gs, m => `<ul style="padding-left:20px;margin:8px 0">${m}</ul>`)
    .replace(/^(?!<[huplt]|$)(.+)$/gm, '<p style="margin:6px 0">$1</p>')
    .replace(/\n{2,}/g, '');
}
