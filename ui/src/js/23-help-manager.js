// ── Help Manager (CRUD + search) ───────────────────────────────────────────────

let _hmArticles   = [];
let _hmCurrentId  = null;
let _hmDirty      = false;
let _hmCm         = null;   // CodeMirror instance
let _hmMode       = 'view'; // 'view' | 'edit' — opening an article always starts read-only
let _hmSearchTimer = null;
let _hmMetaMode   = null;   // 'new' | 'rename'

async function loadHelpManager() {
  _hmInitCm();
  await _hmLoadList('');
}

// ── CodeMirror init ───────────────────────────────────────────────────────────

function _hmInitCm() {
  if (_hmCm) return;
  const host = document.getElementById('hm-cm-host');
  _hmCm = CodeMirror(host, {
    value: '',
    mode: 'markdown',
    theme: 'dracula',
    lineNumbers: true,
    matchBrackets: true,
    indentWithTabs: false,
    tabSize: 2,
    indentUnit: 2,
    keyMap: 'sublime',
    extraKeys: {
      'Ctrl-S': () => hmSaveArticle(),
      'Cmd-S':  () => hmSaveArticle(),
    },
    lineWrapping: true,
    autofocus: false,
  });
  _hmCm.on('change', () => {
    if (_hmCurrentId) _hmSetDirty(true);
  });
}

// ── List / search ────────────────────────────────────────────────────────────

function hmOnSearchInput() {
  clearTimeout(_hmSearchTimer);
  const q = document.getElementById('hm-search-input').value.trim();
  _hmSearchTimer = setTimeout(() => _hmLoadList(q), 250);
}

async function _hmLoadList(q) {
  const data = await api('GET', `/v1/help/articles?q=${encodeURIComponent(q)}`);
  _hmArticles = data.items || [];
  _hmRenderList();
}

function _hmRenderList() {
  const container = document.getElementById('hm-article-list');
  if (!_hmArticles.length) {
    container.innerHTML = '<div class="bm-empty" style="padding:12px">No articles found.</div>';
    return;
  }
  const byCategory = {};
  for (const a of _hmArticles) {
    (byCategory[a.category] ||= []).push(a);
  }
  container.innerHTML = Object.keys(byCategory).sort().map(cat => `
    <div class="ed-dir">
      <div class="ed-dir-label">${_hmEsc(cat)}</div>
      <div>${byCategory[cat].map(a => `
        <div class="ed-file${a.id === _hmCurrentId ? ' active' : ''}"
             onclick="_hmOpenArticle('${a.id}')" data-id="${a.id}">${_hmEsc(a.title)}</div>
      `).join('')}</div>
    </div>
  `).join('');
}

// ── Open / clear ─────────────────────────────────────────────────────────────

async function _hmOpenArticle(id, mode = 'view') {
  const a = await api('GET', `/v1/help/articles/${id}`);
  _hmCurrentId = a.id;
  _hmDirty = false;

  document.querySelectorAll('#hm-article-list .ed-file').forEach(el =>
    el.classList.toggle('active', el.dataset.id === id));

  _hmCm.setValue(a.content || '');
  _hmCm.clearHistory();
  _hmSetDirty(false);

  document.getElementById('hm-title').textContent = a.title;
  document.getElementById('hm-title').dataset.category = a.category;
  document.getElementById('hm-delete-btn').disabled = false;
  document.getElementById('hm-delete-btn').textContent = 'Delete';
  document.getElementById('hm-delete-btn').dataset.confirm = '';
  document.getElementById('hm-delete-btn').classList.remove('btn-danger');
  document.getElementById('hm-rename-btn').disabled = false;
  document.getElementById('hm-preview-btn').disabled = false;

  _hmMode = mode;
  _hmApplyMode();
}

function _hmClearEditor() {
  if (_hmCm) { _hmCm.setValue(''); _hmCm.clearHistory(); }
  document.getElementById('hm-title').textContent = 'No article open';
  delete document.getElementById('hm-title').dataset.category;
  document.getElementById('hm-save-btn').disabled = true;
  document.getElementById('hm-delete-btn').disabled = true;
  document.getElementById('hm-rename-btn').disabled = true;
  document.getElementById('hm-preview-btn').disabled = true;
  _hmMode = 'view';
  _hmApplyMode();
  _hmSetDirty(false);
}

function _hmSetDirty(dirty) {
  _hmDirty = dirty;
  document.getElementById('hm-dirty-ind').textContent = dirty ? ' ●' : '';
}

// ── Save ─────────────────────────────────────────────────────────────────────

async function hmSaveArticle() {
  if (!_hmCurrentId || !_hmCm) return;
  const btn = document.getElementById('hm-save-btn');
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    await api('PUT', `/v1/help/articles/${_hmCurrentId}`, { content: _hmCm.getValue() });
    _hmSetDirty(false);
    toast('Saved', 'success');
  } catch (e) {
    toast(e.message || 'Save failed', 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Save';
  }
}

// ── New article / rename (meta panel) ───────────────────────────────────────

function hmNewArticle() {
  _hmMetaMode = 'new';
  document.getElementById('hm-meta-title').value = '';
  document.getElementById('hm-meta-category').value = '';
  document.getElementById('hm-meta-panel').style.display = 'flex';
  document.getElementById('hm-meta-title').focus();
}

function hmEditMeta() {
  if (!_hmCurrentId) return;
  _hmMetaMode = 'rename';
  const titleEl = document.getElementById('hm-title');
  document.getElementById('hm-meta-title').value = titleEl.textContent;
  document.getElementById('hm-meta-category').value = titleEl.dataset.category || '';
  document.getElementById('hm-meta-panel').style.display = 'flex';
  document.getElementById('hm-meta-title').focus();
}

function hmCancelMeta() {
  document.getElementById('hm-meta-panel').style.display = 'none';
  _hmMetaMode = null;
}

async function hmConfirmMeta() {
  const title = document.getElementById('hm-meta-title').value.trim();
  const category = document.getElementById('hm-meta-category').value.trim() || 'General';
  if (!title) { toast('Enter a title', 'error'); return; }

  try {
    if (_hmMetaMode === 'new') {
      const a = await api('POST', '/v1/help/articles', {
        title, category, content: `## ${title}\n\n`,
      });
      document.getElementById('hm-meta-panel').style.display = 'none';
      toast(`Created "${title}"`, 'success');
      await _hmLoadList(document.getElementById('hm-search-input').value.trim());
      await _hmOpenArticle(a.id, 'edit'); // new articles start blank — go straight to editing
    } else if (_hmMetaMode === 'rename') {
      await api('PUT', `/v1/help/articles/${_hmCurrentId}`, { title, category });
      document.getElementById('hm-meta-panel').style.display = 'none';
      toast('Updated', 'success');
      const modeBeforeRename = _hmMode;
      await _hmLoadList(document.getElementById('hm-search-input').value.trim());
      await _hmOpenArticle(_hmCurrentId, modeBeforeRename);
    }
  } catch (e) {
    toast(e.message || 'Save failed', 'error');
  }
  _hmMetaMode = null;
}

// ── Delete (2-stage) ─────────────────────────────────────────────────────────

function hmDeleteArticle() {
  if (!_hmCurrentId) return;
  const btn = document.getElementById('hm-delete-btn');
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
  _hmDoDelete();
}

async function _hmDoDelete() {
  const id = _hmCurrentId;
  try {
    await api('DELETE', `/v1/help/articles/${id}`);
    toast('Deleted', 'success');
    _hmCurrentId = null;
    _hmDirty = false;
    _hmClearEditor();
    await _hmLoadList(document.getElementById('hm-search-input').value.trim());
  } catch (e) {
    toast(e.message || 'Delete failed', 'error');
  }
}

// ── View / Edit mode ─────────────────────────────────────────────────────────
// Opening an article always lands in read-only "view" mode (rendered
// markdown) — editing is an explicit action via the Edit button, not the
// default landing state.

function hmToggleEdit() {
  _hmMode = _hmMode === 'edit' ? 'view' : 'edit';
  _hmApplyMode();
}

function _hmApplyMode() {
  const cmHost = document.getElementById('hm-cm-host');
  const previewHost = document.getElementById('hm-preview-host');
  const btn = document.getElementById('hm-preview-btn');
  const saveBtn = document.getElementById('hm-save-btn');
  if (_hmMode === 'edit') {
    cmHost.style.display = 'flex';
    previewHost.style.display = 'none';
    btn.textContent = 'View';
    saveBtn.disabled = !_hmCurrentId;
    setTimeout(() => _hmCm.refresh(), 10);
  } else {
    _hmRenderPreview();
    cmHost.style.display = 'none';
    previewHost.style.display = 'block';
    btn.textContent = 'Edit';
    saveBtn.disabled = true;
  }
}

function _hmRenderPreview() {
  document.getElementById('hm-preview-host').innerHTML = mdToHtml(_hmCm.getValue());
}

function _hmEsc(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/"/g,'&quot;')
    .replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
