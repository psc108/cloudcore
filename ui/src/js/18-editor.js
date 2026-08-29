// ── File Editor ──────────────────────────────────────────────────────────────

let _edRoot  = 'ansible';
let _edPath  = null;
let _edDirty = false;
let _edCm    = null;   // CodeMirror instance
let _edRoots = [];

async function loadEditor() {
  const data = await api('GET', '/v1/editor/roots');
  _edRoots = data.roots || [];
  _edBuildRootTabs();
  _edInitCm();
  await _edLoadTree();
}

// ── Root tabs ────────────────────────────────────────────────────────────────

const _edRootLabels = { ansible: 'Ansible', opentofu: 'OpenTofu' };

function _edBuildRootTabs() {
  const bar = document.getElementById('ed-root-tabs');
  bar.innerHTML = _edRoots.map(r =>
    `<button class="ed-tab${r === _edRoot ? ' active' : ''}"
             onclick="_edSwitchRoot('${r}')">${_edRootLabels[r] || r}</button>`
  ).join('');
}

async function _edSwitchRoot(root) {
  _edRoot = root;
  _edPath = null;
  _edDirty = false;
  _edBuildRootTabs();
  _edClearEditor();
  await _edLoadTree();
}

// ── Tree ─────────────────────────────────────────────────────────────────────

async function _edLoadTree() {
  const data = await api('GET', `/v1/editor/tree?root=${_edRoot}`);
  document.getElementById('ed-tree').innerHTML =
    _edRenderTree(data.tree || [], 0);
}

function _edRenderTree(nodes, depth) {
  return nodes.map(n => {
    const pad = depth * 14;
    if (n.type === 'dir') {
      return `<div class="ed-dir" style="padding-left:${pad}px">
                <div class="ed-dir-label">▸ ${_edEsc(n.name)}</div>
                <div>${_edRenderTree(n.children || [], depth + 1)}</div>
              </div>`;
    }
    const active = n.path === _edPath ? ' active' : '';
    return `<div class="ed-file${active}" style="padding-left:${pad + 14}px"
                 onclick="_edOpenFile('${_edEsc(n.path)}')"
                 data-path="${_edEsc(n.path)}">${_edFileIcon(n.name)} ${_edEsc(n.name)}</div>`;
  }).join('');
}

function _edFileIcon(name) {
  if (name.endsWith('.yml') || name.endsWith('.yaml')) return '📄';
  if (name.endsWith('.tf') || name.endsWith('.hcl'))   return '🟦';
  return '📝';
}

// ── CodeMirror init ───────────────────────────────────────────────────────────

function _edInitCm() {
  if (_edCm) return;
  const host = document.getElementById('ed-cm-host');
  _edCm = CodeMirror(host, {
    value: '',
    mode: 'yaml',
    theme: 'dracula',
    lineNumbers: true,
    matchBrackets: true,
    indentWithTabs: false,
    tabSize: 2,
    indentUnit: 2,
    keyMap: 'sublime',
    extraKeys: {
      'Ctrl-S': () => edSaveFile(),
      'Cmd-S':  () => edSaveFile(),
    },
    lineWrapping: false,
    autofocus: false,
  });
  _edCm.on('change', () => {
    if (_edPath) _edSetDirty(true);
  });
}

function _edModeForPath(path) {
  if (!path) return 'yaml';
  if (path.endsWith('.tf') || path.endsWith('.hcl')) return 'javascript'; // closest available; swap for HCL mode when added
  if (path.endsWith('.json')) return { name: 'javascript', json: true };
  return 'yaml';
}

// ── Open / clear ─────────────────────────────────────────────────────────────

async function _edOpenFile(path) {
  _edDirty = false;
  _edPath  = path;

  document.querySelectorAll('.ed-file').forEach(el =>
    el.classList.toggle('active', el.dataset.path === path));

  const data = await api('GET',
    `/v1/editor/file?root=${_edRoot}&path=${encodeURIComponent(path)}`);

  _edCm.setOption('mode', _edModeForPath(path));
  _edCm.setValue(data.content || '');
  _edCm.clearHistory();
  _edSetDirty(false);

  document.getElementById('ed-filename').textContent = path;
  document.getElementById('ed-save-btn').disabled   = false;
  document.getElementById('ed-delete-btn').disabled = false;
  document.getElementById('ed-delete-btn').textContent = 'Delete';
  document.getElementById('ed-delete-btn').dataset.confirm = '';
  document.getElementById('ed-delete-btn').classList.remove('btn-danger');
  setTimeout(() => _edCm.refresh(), 10);
}

function _edClearEditor() {
  if (_edCm) { _edCm.setValue(''); _edCm.clearHistory(); }
  document.getElementById('ed-filename').textContent    = 'No file open';
  document.getElementById('ed-save-btn').disabled       = true;
  document.getElementById('ed-delete-btn').disabled     = true;
  _edSetDirty(false);
}

function _edSetDirty(dirty) {
  _edDirty = dirty;
  const ind = document.getElementById('ed-dirty-ind');
  ind.textContent = dirty ? ' ●' : '';
}

// ── Save ─────────────────────────────────────────────────────────────────────

async function edSaveFile() {
  if (!_edPath || !_edCm) return;
  const btn = document.getElementById('ed-save-btn');
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    await api('PUT',
      `/v1/editor/file?root=${_edRoot}&path=${encodeURIComponent(_edPath)}`,
      { content: _edCm.getValue() });
    _edSetDirty(false);
    toast('Saved', 'success');
  } catch (e) {
    toast(e.message || 'Save failed', 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Save';
  }
}

// ── New file ─────────────────────────────────────────────────────────────────

async function edNewFile() {
  const panel = document.getElementById('ed-new-panel');
  const visible = panel.style.display !== 'none';
  panel.style.display = visible ? 'none' : 'flex';
  if (!visible) {
    const input = document.getElementById('ed-new-name');
    input.value = '';
    input.placeholder = _edRoot === 'opentofu' ? 'main.tf' : '08-my-playbook.yml';
    input.focus();
  }
}

async function edCreateFile() {
  const name = document.getElementById('ed-new-name').value.trim();
  if (!name) { toast('Enter a filename', 'error'); return; }
  try {
    await api('POST', `/v1/editor/file?root=${_edRoot}`, { filename: name, content: '' });
    document.getElementById('ed-new-panel').style.display = 'none';
    toast(`Created ${name}`, 'success');
    await _edLoadTree();
    await _edOpenFile(name);
  } catch (e) {
    toast(e.message || 'Create failed', 'error');
  }
}

// ── Delete (2-stage) ─────────────────────────────────────────────────────────

function edDeleteFile() {
  if (!_edPath) return;
  const btn = document.getElementById('ed-delete-btn');
  if (btn.dataset.confirm !== '1') {
    btn.dataset.confirm = '1';
    btn.textContent = 'Confirm delete?';
    btn.classList.add('btn-danger');
    setTimeout(() => {
      if (btn.dataset.confirm === '1') {
        btn.dataset.confirm = '';
        btn.textContent = 'Delete';
        btn.classList.remove('btn-danger');
      }
    }, 4000);
    return;
  }
  btn.dataset.confirm = '';
  btn.textContent = 'Delete';
  btn.classList.remove('btn-danger');
  _edDoDelete();
}

async function _edDoDelete() {
  const path = _edPath;
  try {
    await api('DELETE',
      `/v1/editor/file?root=${_edRoot}&path=${encodeURIComponent(path)}`);
    toast(`Deleted ${path}`, 'success');
    _edPath = null;
    _edDirty = false;
    _edClearEditor();
    await _edLoadTree();
  } catch (e) {
    toast(e.message || 'Delete failed', 'error');
  }
}

function _edEsc(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/"/g,'&quot;')
    .replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
