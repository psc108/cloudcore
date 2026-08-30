// ── Terminal tab ──────────────────────────────────────────────────────────────
const WS_BASE = 'ws://127.0.0.1:8081';
const _openTerminals = {};   // instance_id -> { ws, term, fitAddon, el }

function _pickNonSudoUser(instance) {
  const users = instance.users || [];
  const nonSudo = users.find(u => !u.sudo);
  if (nonSudo) return nonSudo.username;
  return instance.ssh_user || null;
}

async function loadTerminalInstances() {
  const list = document.getElementById('terminal-instance-list');
  list.innerHTML = '<div style="color:var(--text-muted);font-size:13px">Loading…</div>';
  try {
    const [instData, nfsData] = await Promise.all([
      api('GET', '/v1/instances'),
      api('GET', '/v1/nfs-servers'),
    ]);
    const instances = (instData.items || []).filter(i => i.status !== 'deleted');
    const nfsItems  = (nfsData.items  || []).filter(s => s.status !== 'deleted');

    const allItems = [
      ...instances.map(i => ({ ...i, _kind: 'instance' })),
      ...nfsItems.map(s => ({ ...s, _kind: 'nfs', ssh_user: 'ubuntu', users: [] })),
    ];

    if (!allItems.length) {
      list.innerHTML = '<div style="color:var(--text-muted);font-size:13px">No instances or NFS servers found.</div>';
      return;
    }
    list.innerHTML = allItems.map(i => {
      const running   = i.status === 'running';
      const nonSudo   = _pickNonSudoUser(i);
      const allSudo   = (i.users||[]).length > 0 && (i.users||[]).every(u => u.sudo);
      const noSshPort = !i.ssh_port;
      const isNfs     = i._kind === 'nfs';

      let actionHtml;
      if (!running) {
        actionHtml = `<span class="text-muted" style="font-size:12px">${isNfs ? 'NFS server' : 'Instance'} not running</span>`;
      } else if (noSshPort) {
        actionHtml = `<span class="text-muted" style="font-size:12px">No SSH port</span>`;
      } else if (!isNfs && allSudo) {
        actionHtml = `
          <div class="no-sudo-msg">
            ⚠ All users on this instance have sudo privileges.<br>
            Add a non-sudo user via the <strong>Users</strong> panel before opening a terminal.
          </div>`;
      } else {
        const user = isNfs ? 'ubuntu' : (nonSudo || i.ssh_user || 'ubuntu');
        actionHtml = `
          <button class="btn btn-primary" onclick="openTerminal('${i.id}','${i.name}','${user}')">
            Open Terminal
          </button>`;
      }

      const metaKind = isNfs ? `NFS &nbsp;·&nbsp; ${i.flavor}` : `${i.image_id} &nbsp;·&nbsp; ${i.flavor}`;
      const metaUser = isNfs ? 'ubuntu' : (_pickNonSudoUser(i) || '—');

      return `
        <div class="instance-card">
          <div class="inst-info">
            <div class="inst-name">${i.name} ${badge(i.status)}</div>
            <div class="inst-meta">
              ${metaKind}
              &nbsp;·&nbsp; SSH: <code>${i.ssh_port ? '127.0.0.1:' + i.ssh_port : '—'}</code>
              &nbsp;·&nbsp; User: <code>${metaUser}</code>
            </div>
          </div>
          <div>${actionHtml}</div>
        </div>`;
    }).join('');
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger);font-size:13px">Error: ${e.message}</div>`;
  }
}

function openTerminal(instanceId, instanceName, username) {
  // Only one terminal per instance
  if (_openTerminals[instanceId]) {
    _openTerminals[instanceId].el.style.display = 'flex';
    return;
  }

  // Build floating window
  const win = document.createElement('div');
  win.className = 'term-window';
  win.style.cssText = 'width:760px;height:420px;top:80px;left:120px;';
  win.innerHTML = `
    <div class="term-titlebar" id="tb-${instanceId}">
      <span style="font-size:14px">⬛</span>
      <span class="term-title">${instanceName} — ${username}</span>
      <button class="term-close" onclick="closeTerminal('${instanceId}')" title="Close">✕</button>
    </div>
    <div class="term-body" id="term-body-${instanceId}">
      <div class="term-resize-handle" id="rh-${instanceId}"></div>
    </div>`;
  document.body.appendChild(win);
  _openTerminals[instanceId] = { el: win, ws: null, term: null, fitAddon: null };

  _makeDraggable(win, document.getElementById(`tb-${instanceId}`));
  _makeResizable(win, document.getElementById(`rh-${instanceId}`), instanceId);

  // Init xterm
  const term = new Terminal({
    theme: { background: '#0d0d0d', foreground: '#e2e6f0', cursor: '#4f8ef7' },
    fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
    fontSize: 13,
    cursorBlink: true,
    scrollback: 2000,
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  const body = document.getElementById(`term-body-${instanceId}`);
  term.open(body);
  fitAddon.fit();
  _openTerminals[instanceId].term = term;
  _openTerminals[instanceId].fitAddon = fitAddon;

  // Connect WebSocket
  const ws = new WebSocket(`${WS_BASE}/terminal?instance_id=${instanceId}`);
  _openTerminals[instanceId].ws = ws;

  ws.onopen = () => {
    const { cols, rows } = term;
    ws.send(JSON.stringify({ type: 'resize', cols, rows }));
  };

  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'output' || msg.type === 'connected') {
        term.write(msg.data);
      } else if (msg.type === 'error') {
        // Replace terminal with error message
        term.dispose();
        body.innerHTML = `<div class="term-error">${msg.data}</div><div class="term-resize-handle" id="rh-${instanceId}"></div>`;
        _makeResizable(win, document.getElementById(`rh-${instanceId}`), instanceId);
      }
    } catch {}
  };

  ws.onclose = () => {
    if (_openTerminals[instanceId] && _openTerminals[instanceId].term) {
      _openTerminals[instanceId].term.write('\r\n\x1b[33m[Connection closed]\x1b[0m\r\n');
    }
  };

  ws.onerror = () => {
    term.write('\r\n\x1b[31m[WebSocket error - is the terminal server running?]\x1b[0m\r\n');
  };

  // Keyboard input
  term.onData(data => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'input', data }));
    }
  });

  // Resize → send PTY resize
  const ro = new ResizeObserver(() => {
    fitAddon.fit();
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
    }
  });
  ro.observe(body);
  _openTerminals[instanceId].ro = ro;
}

function closeTerminal(instanceId) {
  const t = _openTerminals[instanceId];
  if (!t) return;
  if (t.ws)       t.ws.close();
  if (t.term)     t.term.dispose();
  if (t.ro)       t.ro.disconnect();
  if (t.el)       t.el.remove();
  delete _openTerminals[instanceId];
}
