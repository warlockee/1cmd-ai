/* ---------------------------------------------------------------
 *  Cronjobs tab
 * --------------------------------------------------------------- */
window.initCron = function () {
    var panel = document.getElementById('tab-cronjobs');
    if (!panel || panel.dataset.init) return;
    panel.dataset.init = '1';

    var jobs = [];
    var refreshTimer = null;

    /* --- scaffold --- */

    panel.innerHTML = `
        <div class="cron-add-bar">
            <input id="cron-input" type="text" class="cron-add-input"
                   placeholder="Describe a new cron job\u2026">
            <button id="cron-add" class="btn btn-primary btn-sm">Add</button>
        </div>
        <table class="data-table">
            <thead>
                <tr>
                    <th>Description</th>
                    <th>Schedule</th>
                    <th>Status</th>
                    <th>Last run</th>
                    <th style="text-align:right">Actions</th>
                </tr>
            </thead>
            <tbody id="cron-tbody"></tbody>
        </table>
    `;

    var input = document.getElementById('cron-input');
    var addBtn = document.getElementById('cron-add');
    var tbody  = document.getElementById('cron-tbody');

    /* --- helpers --- */

    async function api(path, opts) {
        opts = opts || {};
        var res = await fetch('/api/cron' + path, {
            headers: { 'Content-Type': 'application/json' },
            ...opts,
        });
        if (res.status === 401) { window.showToast('Session expired.', 'error'); return null; }
        if (!res.ok) { var d = await res.json().catch(function () { return {}; }); window.showToast(d.detail || 'Failed', 'error'); return null; }
        return res.json();
    }

    function relTime(epoch) {
        if (!epoch) return 'never';
        var d = Math.floor(Date.now() / 1000 - epoch);
        if (d < 0) return 'just now';
        if (d < 60) return d + 's ago';
        if (d < 3600) return Math.floor(d / 60) + 'm ago';
        if (d < 86400) return Math.floor(d / 3600) + 'h ago';
        return Math.floor(d / 86400) + 'd ago';
    }

    function badge(status) {
        var c = { draft: 'gray', compiled: 'yellow', active: 'green', paused: 'blue', error: 'red' }[status] || 'gray';
        return '<span class="status-badge ' + c + '"></span>' + status;
    }

    function esc(s) { var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

    /* --- render --- */

    function render() {
        if (!jobs.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="terminal-empty">No cron jobs yet.</td></tr>';
            return;
        }

        tbody.innerHTML = jobs.map(function (j) {
            var isActive = j.status === 'active';
            var toggleLabel = isActive ? 'Pause' : 'Activate';
            var toggleCls = isActive ? 'btn-ghost' : 'btn-primary';
            var canToggle = j.status === 'active' || (j.schedule && j.status !== 'draft');
            var planHtml = j.llm_plan
                ? '<div class="cron-plan" style="display:none">' + esc(j.llm_plan) + '</div>'
                : '';
            var planLink = j.llm_plan
                ? '<a href="#" class="cron-plan-toggle" data-id="' + j.id + '">show plan</a>'
                : '';
            var errorMark = j.error
                ? '<span class="cron-error-mark" title="' + esc(j.error) + '">!</span>'
                : '';

            return '<tr data-id="' + j.id + '">'
                + '<td><span class="cron-desc-editable" data-id="' + j.id + '">' + esc(j.description) + '</span>' + planLink + planHtml + '</td>'
                + '<td><span class="cron-schedule-editable" data-id="' + j.id + '">' + esc(j.schedule || '\u2014') + '</span></td>'
                + '<td>' + badge(j.status) + errorMark + '</td>'
                + '<td style="color:var(--text-muted);font-size:12px">' + relTime(j.last_run_at) + '</td>'
                + '<td class="cron-actions">'
                    + '<button class="btn btn-ghost btn-sm cron-compile" data-id="' + j.id + '">Compile</button>'
                    + (canToggle ? '<button class="btn ' + toggleCls + ' btn-sm cron-toggle" data-id="' + j.id + '" data-action="' + (isActive ? 'pause' : 'activate') + '">' + toggleLabel + '</button>' : '')
                    + '<button class="btn btn-danger btn-sm cron-del" data-id="' + j.id + '">Del</button>'
                + '</td></tr>';
        }).join('');

        attachListeners();
    }

    /* --- listeners --- */

    function attachListeners() {
        tbody.querySelectorAll('.cron-compile').forEach(function (btn) {
            btn.addEventListener('click', async function () {
                btn.disabled = true; btn.textContent = '\u2026';
                var r = await api('/' + btn.dataset.id + '/compile', { method: 'POST' });
                btn.disabled = false; btn.textContent = 'Compile';
                if (r) { window.showToast('Compiled', 'success'); loadJobs(); }
            });
        });

        tbody.querySelectorAll('.cron-toggle').forEach(function (btn) {
            btn.addEventListener('click', async function () {
                var r = await api('/' + btn.dataset.id + '/' + btn.dataset.action, { method: 'POST' });
                if (r) { window.showToast('Done', 'success'); loadJobs(); }
            });
        });

        tbody.querySelectorAll('.cron-del').forEach(function (btn) {
            btn.addEventListener('click', async function () {
                if (!confirm('Delete this job?')) return;
                var r = await api('/' + btn.dataset.id, { method: 'DELETE' });
                if (r) { window.showToast('Deleted', 'info'); loadJobs(); }
            });
        });

        tbody.querySelectorAll('.cron-desc-editable').forEach(function (span) {
            span.addEventListener('click', function () { inlineEdit(span, 'description'); });
        });

        tbody.querySelectorAll('.cron-schedule-editable').forEach(function (span) {
            span.addEventListener('click', function () { inlineEdit(span, 'schedule'); });
        });

        tbody.querySelectorAll('.cron-plan-toggle').forEach(function (link) {
            link.addEventListener('click', function (e) {
                e.preventDefault();
                var plan = link.parentElement.querySelector('.cron-plan');
                if (!plan) return;
                var vis = plan.style.display !== 'none';
                plan.style.display = vis ? 'none' : 'block';
                link.textContent = vis ? 'show plan' : 'hide plan';
            });
        });
    }

    /* --- inline edit (schedule only) --- */

    function inlineEdit(span, field) {
        if (field === 'description') { openDescEditor(span); return; }

        var id = span.dataset.id;
        var job = jobs.find(function (j) { return j.id == id; }) || {};
        var current = job.schedule || '';

        var inp = document.createElement('input');
        inp.type = 'text';
        inp.value = current;
        inp.className = 'inline-edit-input mono';
        span.replaceWith(inp);
        inp.focus();
        inp.select();

        async function commit() {
            var v = inp.value.trim();
            if (v && v !== current) {
                await api('/' + id, { method: 'PUT', body: JSON.stringify({ schedule: v }) });
            }
            loadJobs();
        }

        inp.addEventListener('blur', commit);
        inp.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); inp.blur(); }
            if (e.key === 'Escape') { inp.value = current; inp.blur(); }
        });
    }

    /* --- description editor (floating modal with CodeMirror) --- */

    var descOverlay = null;

    function openDescEditor(span) {
        var id = span.dataset.id;
        var job = jobs.find(function (j) { return j.id == id; }) || {};
        var current = job.description || '';

        // Remove existing overlay
        if (descOverlay) descOverlay.remove();

        // Build modal
        descOverlay = document.createElement('div');
        descOverlay.className = 'desc-overlay';
        descOverlay.innerHTML =
            '<div class="desc-modal">' +
                '<div class="desc-modal-header">' +
                    '<span>Edit description</span>' +
                    '<div class="desc-modal-buttons">' +
                        '<button class="btn btn-primary btn-sm desc-save">Save</button>' +
                        '<button class="btn btn-ghost btn-sm desc-close">Close</button>' +
                    '</div>' +
                '</div>' +
                '<div class="desc-editor-wrap"></div>' +
            '</div>';
        document.body.appendChild(descOverlay);

        var wrap = descOverlay.querySelector('.desc-editor-wrap');
        var cm = null;
        var textarea = null;

        if (typeof CodeMirror !== 'undefined') {
            cm = CodeMirror(wrap, {
                value: current,
                mode: 'markdown',
                theme: 'material-darker',
                lineWrapping: true,
                autofocus: true,
                viewportMargin: Infinity,
            });
            setTimeout(function () { cm.refresh(); }, 50);
        } else {
            textarea = document.createElement('textarea');
            textarea.value = current;
            textarea.className = 'desc-textarea-fallback';
            wrap.appendChild(textarea);
            textarea.focus();
        }

        function getValue() {
            return cm ? cm.getValue().trim() : textarea.value.trim();
        }

        async function save() {
            var v = getValue();
            if (v && v !== current) {
                await api('/' + id, { method: 'PUT', body: JSON.stringify({ description: v }) });
                window.showToast('Saved', 'success');
            }
            descOverlay.remove();
            descOverlay = null;
            loadJobs();
        }

        descOverlay.querySelector('.desc-save').addEventListener('click', save);
        descOverlay.querySelector('.desc-close').addEventListener('click', function () {
            descOverlay.remove();
            descOverlay = null;
        });

        // Click outside modal = close
        descOverlay.addEventListener('click', function (e) {
            if (e.target === descOverlay) {
                descOverlay.remove();
                descOverlay = null;
            }
        });

        // Ctrl/Cmd+S = save
        if (cm) {
            cm.setOption('extraKeys', {
                'Cmd-S': save,
                'Ctrl-S': save,
            });
        }
    }

    /* --- data --- */

    async function loadJobs() {
        var d = await api('');
        if (d !== null) { jobs = d; render(); }
    }

    async function addJob() {
        var desc = input.value.trim();
        if (!desc) return;
        var r = await api('', { method: 'POST', body: JSON.stringify({ description: desc }) });
        if (r) { input.value = ''; window.showToast('Created', 'success'); loadJobs(); }
    }

    addBtn.addEventListener('click', addJob);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); addJob(); } });

    /* --- refresh --- */

    function start() { stop(); refreshTimer = setInterval(loadJobs, 15000); }
    function stop() { if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; } }

    new MutationObserver(function () {
        if (panel.classList.contains('active')) { loadJobs(); start(); }
        else stop();
    }).observe(panel, { attributes: true, attributeFilter: ['class'] });

    if (panel.classList.contains('active')) { loadJobs(); start(); }
};
