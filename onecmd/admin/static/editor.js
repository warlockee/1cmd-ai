/* ---------------------------------------------------------------
 *  Editor tab — Skills files + memories
 * --------------------------------------------------------------- */
(function () {
    'use strict';

    let cm = null;
    let currentFileKey = null;
    let currentReadonly = false;
    let filesLoaded = false;
    let memoriesLoaded = false;
    let $panel, $cmHost, $memBody;

    /* --- helpers --- */

    async function api(url, opts = {}) {
        const res = await fetch(url, {
            headers: { 'Content-Type': 'application/json', ...opts.headers },
            ...opts,
        });
        if (res.status === 401) { window.showToast('Session expired.', 'error'); throw new Error('auth'); }
        if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || res.statusText); }
        return res.json();
    }

    function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
    function trunc(s, n) { return !s ? '' : s.length > n ? s.slice(0, n) + '\u2026' : s; }
    function fmtDate(epoch) { return epoch ? new Date(epoch * 1000).toLocaleString() : ''; }

    /* --- scaffold --- */

    function scaffold() {
        $panel = document.getElementById('tab-editor');
        if ($panel.dataset.scaffolded) return;
        $panel.dataset.scaffolded = '1';

        $panel.innerHTML = `
            <div class="editor-container">
                <div class="editor-sidebar" id="editor-sidebar">
                    <div class="editor-sidebar-title">Files</div>
                    <div id="editor-file-list"></div>
                    <div style="margin-top:auto;padding-top:16px;">
                        <button class="btn btn-ghost btn-sm" id="editor-reload-btn" style="width:100%">Reload Skills</button>
                    </div>
                </div>
                <div class="editor-main">
                    <div class="editor-toolbar">
                        <span class="editor-file-label" id="editor-file-label"></span>
                        <span class="editor-readonly-badge" id="editor-readonly-badge" style="display:none">Read only</span>
                        <button class="btn btn-primary btn-sm" id="editor-save-btn" style="display:none">Save</button>
                    </div>
                    <div class="editor-cm-host" id="editor-cm-host"></div>
                </div>
            </div>

            <div class="mem-section" id="editor-mem-section">
                <div class="mem-header">
                    <h3>Memories</h3>
                    <button class="btn btn-primary btn-sm" id="editor-mem-add-btn">Add memory</button>
                </div>
                <table class="data-table" id="editor-mem-table">
                    <thead>
                        <tr><th>ID</th><th>Chat</th><th>Content</th><th>Category</th><th>Created</th><th style="text-align:right">Actions</th></tr>
                    </thead>
                    <tbody id="editor-mem-tbody"></tbody>
                </table>
                <p class="mem-empty" id="editor-mem-empty" style="display:none">No memories yet.</p>
            </div>
        `;

        $cmHost  = document.getElementById('editor-cm-host');
        $memBody = document.getElementById('editor-mem-tbody');

        document.getElementById('editor-save-btn').addEventListener('click', saveFile);
        document.getElementById('editor-reload-btn').addEventListener('click', reloadSkills);
        document.getElementById('editor-mem-add-btn').addEventListener('click', showAddForm);
    }

    /* --- codemirror --- */

    function ensureCM() {
        if (cm) return;
        const ta = document.createElement('textarea');
        $cmHost.appendChild(ta);
        cm = CodeMirror.fromTextArea(ta, {
            theme: 'material-darker',
            mode: 'markdown',
            lineNumbers: true,
            lineWrapping: true,
            readOnly: true,
        });
        cm.setSize('100%', '100%');
    }

    /* --- files --- */

    async function loadFiles() {
        try {
            const files = await api('/api/files');
            const list = document.getElementById('editor-file-list');
            list.innerHTML = '';
            files.forEach(f => {
                const btn = document.createElement('button');
                btn.className = 'editor-sidebar-btn';
                btn.textContent = f.label;
                btn.dataset.key = f.key;
                btn.addEventListener('click', () => selectFile(f.key));
                list.appendChild(btn);
            });
            filesLoaded = true;
            if (files.length && !currentFileKey) selectFile(files[0].key);
        } catch (e) { if (e.message !== 'auth') window.showToast('Load failed: ' + e.message, 'error'); }
    }

    function highlightSidebar(key) {
        document.querySelectorAll('#editor-file-list .editor-sidebar-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.key === key);
        });
    }

    async function selectFile(key) {
        try {
            const data = await api('/api/files/' + encodeURIComponent(key));
            currentFileKey = key;
            currentReadonly = data.readonly;

            ensureCM();
            cm.setOption('readOnly', data.readonly);
            cm.setValue(data.content || '');
            cm.clearHistory();

            document.getElementById('editor-file-label').textContent = data.label;
            document.getElementById('editor-readonly-badge').style.display = data.readonly ? 'inline-block' : 'none';
            document.getElementById('editor-save-btn').style.display = data.readonly ? 'none' : 'inline-flex';

            highlightSidebar(key);
        } catch (e) { if (e.message !== 'auth') window.showToast('Load failed: ' + e.message, 'error'); }
    }

    async function saveFile() {
        if (!currentFileKey || currentReadonly) return;
        try {
            await api('/api/files/' + encodeURIComponent(currentFileKey), {
                method: 'PUT',
                body: JSON.stringify({ content: cm.getValue() }),
            });
            window.showToast('Saved & reloaded', 'success');
        } catch (e) { if (e.message !== 'auth') window.showToast('Save failed: ' + e.message, 'error'); }
    }

    async function reloadSkills() {
        try {
            await api('/api/files/reload', { method: 'POST' });
            window.showToast('Skills reloaded', 'success');
        } catch (e) { if (e.message !== 'auth') window.showToast('Reload failed: ' + e.message, 'error'); }
    }

    /* --- memories --- */

    async function loadMemories() {
        try {
            const rows = await api('/api/memories');
            renderMemories(rows);
            memoriesLoaded = true;
        } catch (e) { if (e.message !== 'auth') window.showToast('Load failed: ' + e.message, 'error'); }
    }

    function renderMemories(rows) {
        const $empty = document.getElementById('editor-mem-empty');
        const $table = document.getElementById('editor-mem-table');
        if (!rows.length) { $table.style.display = 'none'; $empty.style.display = 'block'; return; }
        $table.style.display = ''; $empty.style.display = 'none';

        $memBody.innerHTML = rows.map(m => `
            <tr data-id="${m.id}">
                <td>${m.id}</td>
                <td>${m.chat_id}</td>
                <td title="${esc(m.content)}">${esc(trunc(m.content, 80))}</td>
                <td>${esc(m.category)}</td>
                <td style="color:var(--text-muted);font-size:12px">${fmtDate(m.created_at)}</td>
                <td style="text-align:right;white-space:nowrap">
                    <button class="btn btn-ghost btn-sm" data-action="edit" data-id="${m.id}">Edit</button>
                    <button class="btn btn-danger btn-sm" data-action="delete" data-id="${m.id}">Del</button>
                </td>
            </tr>
        `).join('');

        $memBody.onclick = function (e) {
            const btn = e.target.closest('[data-action]');
            if (!btn) return;
            const id = parseInt(btn.dataset.id, 10);
            if (btn.dataset.action === 'delete') deleteMemory(id);
            if (btn.dataset.action === 'edit') editMemory(id, rows.find(r => r.id === id));
        };
    }

    /* --- add memory --- */

    function showAddForm() {
        if (document.getElementById('editor-mem-form')) return;
        const $sec = document.getElementById('editor-mem-section');
        const form = document.createElement('div');
        form.id = 'editor-mem-form';
        form.className = 'mem-form';
        form.innerHTML = `
            <div class="mem-form-row">
                <div class="mem-form-field grow">
                    <label class="mem-form-label">Content</label>
                    <textarea id="mem-form-content" rows="2" style="width:100%"></textarea>
                </div>
                <div class="mem-form-field">
                    <label class="mem-form-label">Category</label>
                    <select id="mem-form-category" style="width:100%">
                        <option value="general">general</option>
                        <option value="rule">rule</option>
                        <option value="knowledge">knowledge</option>
                        <option value="preference">preference</option>
                    </select>
                </div>
                <div class="mem-form-field" style="width:80px">
                    <label class="mem-form-label">Chat ID</label>
                    <input id="mem-form-chatid" type="number" value="0" min="0" style="width:100%">
                </div>
                <div style="display:flex;gap:6px;align-items:flex-end">
                    <button class="btn btn-primary btn-sm" id="mem-form-save">Save</button>
                    <button class="btn btn-ghost btn-sm" id="mem-form-cancel">Cancel</button>
                </div>
            </div>
        `;
        $sec.insertBefore(form, document.getElementById('editor-mem-table'));
        document.getElementById('mem-form-cancel').onclick = () => form.remove();
        document.getElementById('mem-form-save').onclick = async () => {
            const content  = document.getElementById('mem-form-content').value.trim();
            const category = document.getElementById('mem-form-category').value;
            const chatId   = parseInt(document.getElementById('mem-form-chatid').value, 10) || 0;
            if (!content) { window.showToast('Content required', 'warning'); return; }
            try {
                await api('/api/memories', { method: 'POST', body: JSON.stringify({ content, category, chat_id: chatId }) });
                window.showToast('Created', 'success'); form.remove(); loadMemories();
            } catch (e) { if (e.message !== 'auth') window.showToast('Failed: ' + e.message, 'error'); }
        };
        document.getElementById('mem-form-content').focus();
    }

    /* --- edit memory --- */

    function editMemory(id, mem) {
        if (!mem) return;
        const tr = $memBody.querySelector('tr[data-id="' + id + '"]');
        if (!tr) return;
        tr.innerHTML = `
            <td colspan="6">
                <div class="mem-form-row">
                    <div class="mem-form-field grow">
                        <textarea id="mem-edit-c-${id}" rows="2" style="width:100%">${esc(mem.content)}</textarea>
                    </div>
                    <div class="mem-form-field">
                        <select id="mem-edit-cat-${id}" style="width:100%">
                            <option value="general" ${mem.category==='general'?'selected':''}>general</option>
                            <option value="rule" ${mem.category==='rule'?'selected':''}>rule</option>
                            <option value="knowledge" ${mem.category==='knowledge'?'selected':''}>knowledge</option>
                            <option value="preference" ${mem.category==='preference'?'selected':''}>preference</option>
                        </select>
                    </div>
                    <div style="display:flex;gap:6px;align-items:flex-end">
                        <button class="btn btn-primary btn-sm" onclick="window._edSaveMem(${id})">Save</button>
                        <button class="btn btn-ghost btn-sm" onclick="window._edCancel()">Cancel</button>
                    </div>
                </div>
            </td>
        `;
    }

    window._edSaveMem = async function (id) {
        const c = document.getElementById('mem-edit-c-' + id).value.trim();
        const cat = document.getElementById('mem-edit-cat-' + id).value;
        if (!c) { window.showToast('Content required', 'warning'); return; }
        try {
            await api('/api/memories/' + id, { method: 'PUT', body: JSON.stringify({ content: c, category: cat }) });
            window.showToast('Updated', 'success'); loadMemories();
        } catch (e) { if (e.message !== 'auth') window.showToast('Failed: ' + e.message, 'error'); }
    };

    window._edCancel = function () { loadMemories(); };

    async function deleteMemory(id) {
        if (!confirm('Delete memory #' + id + '?')) return;
        try {
            await api('/api/memories/' + id, { method: 'DELETE' });
            window.showToast('Deleted', 'success'); loadMemories();
        } catch (e) { if (e.message !== 'auth') window.showToast('Failed: ' + e.message, 'error'); }
    }

    /* --- init --- */

    window.initEditor = function () {
        scaffold();
        if (!filesLoaded) loadFiles();
        if (!memoriesLoaded) loadMemories();
        if (cm) setTimeout(() => cm.refresh(), 50);
    };
})();
