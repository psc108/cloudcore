// ── Drag ─────────────────────────────────────────────────────────────────────
function _makeDraggable(win, handle) {
  let ox = 0, oy = 0;
  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    ox = e.clientX - win.offsetLeft;
    oy = e.clientY - win.offsetTop;
    const onMove = e => {
      win.style.left = (e.clientX - ox) + 'px';
      win.style.top  = (e.clientY - oy) + 'px';
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

// ── Resize ────────────────────────────────────────────────────────────────────
function _makeResizable(win, handle, instanceId) {
  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    const startW = win.offsetWidth;
    const startH = win.offsetHeight;
    const startX = e.clientX;
    const startY = e.clientY;
    const onMove = e => {
      win.style.width  = Math.max(400, startW + e.clientX - startX) + 'px';
      win.style.height = Math.max(260, startH + e.clientY - startY) + 'px';
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      const t = _openTerminals[instanceId];
      if (t && t.fitAddon) t.fitAddon.fit();
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

// ── Form buttons wired directly in HTML ──
