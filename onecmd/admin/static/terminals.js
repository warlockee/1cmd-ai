/* ---------------------------------------------------------------
 *  Terminals tab — grid, detail panel, WebSocket
 * --------------------------------------------------------------- */
(function () {
    'use strict';

    var panel = document.getElementById('tab-terminals');
    var refreshTimer = null;
    var ws = null;
    var activeTermId = null;

    /* --- helpers --- */

    async function apiFetch(url, opts) {
        var res = await fetch(url, opts);
        if (res.status === 401) {
            window.showToast('Session expired.', 'error');
            return null;
        }
        return res;
    }

    async function fetchTerminals() {
        var res = await apiFetch('/api/terminals');
        if (!res) return [];
        return res.json();
    }

    async function fetchCapture(id) {
        var res = await apiFetch('/api/terminals/' + encodeURIComponent(id));
        if (!res) return null;
        return res.json();
    }

    async function sendKeys(id, text, suppress) {
        var res = await apiFetch('/api/terminals/' + encodeURIComponent(id) + '/keys', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: text, suppress_newline: !!suppress }),
        });
        if (!res) return false;
        if (!res.ok) {
            var d = await res.json().catch(function () { return {}; });
            window.showToast(d.detail || 'Send failed', 'error');
            return false;
        }
        return true;
    }

    async function setAlias(id, name) {
        var res = await apiFetch('/api/terminals/' + encodeURIComponent(id) + '/alias', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name }),
        });
        if (!res) return false;
        if (!res.ok) { window.showToast('Rename failed', 'error'); return false; }
        window.showToast('Renamed', 'success');
        return true;
    }

    /* --- grid --- */

    function renderGrid(terminals) {
        var detail = panel.querySelector('.terminal-detail');

        var grid = document.createElement('div');
        grid.className = 'terminal-grid';

        if (!terminals.length) {
            var p = document.createElement('p');
            p.className = 'terminal-empty';
            p.textContent = 'No terminal sessions found.';
            grid.appendChild(p);
        }

        terminals.forEach(function (t) {
            var card = document.createElement('div');
            card.className = 'terminal-card';

            var hdr = document.createElement('div');
            hdr.className = 'terminal-card-header';

            var nm = document.createElement('span');
            nm.className = 'terminal-card-name';
            nm.textContent = t.alias || t.name;

            var idx = document.createElement('span');
            idx.className = 'terminal-card-index';
            idx.textContent = '.' + t.index;

            hdr.appendChild(nm);
            hdr.appendChild(idx);
            card.appendChild(hdr);

            if (t.title) {
                var sub = document.createElement('div');
                sub.className = 'terminal-card-title';
                sub.textContent = t.title;
                card.appendChild(sub);
            }

            var pre = document.createElement('pre');
            pre.className = 'terminal-preview';
            pre.textContent = '\u00a0';
            card.appendChild(pre);

            fetchCapture(t.id).then(function (data) {
                if (!data) { pre.textContent = ''; return; }
                var lines = data.content.split('\n');
                pre.textContent = lines.slice(-3).join('\n') || '(empty)';
            });

            card.addEventListener('click', function () { openDetail(t); });
            grid.appendChild(card);
        });

        panel.innerHTML = '';
        panel.appendChild(grid);
        if (detail) panel.appendChild(detail);
    }

    /* --- detail --- */

    function openDetail(t) {
        closeDetail();
        activeTermId = t.id;

        var el = document.createElement('div');
        el.className = 'terminal-detail';

        /* header */
        var hdr = document.createElement('div');
        hdr.className = 'terminal-detail-header';

        var nm = document.createElement('h2');
        nm.className = 'terminal-detail-name';
        nm.textContent = t.alias || t.name;
        nm.title = 'Double-click to rename';
        nm.addEventListener('dblclick', function () { startRename(nm, t); });

        var closeBtn = document.createElement('button');
        closeBtn.className = 'btn btn-ghost btn-sm';
        closeBtn.textContent = 'Close';
        closeBtn.addEventListener('click', closeDetail);

        hdr.appendChild(nm);
        hdr.appendChild(closeBtn);
        el.appendChild(hdr);

        if (t.title) {
            var sub = document.createElement('div');
            sub.className = 'terminal-detail-subtitle';
            sub.textContent = t.title;
            el.appendChild(sub);
        }

        var output = document.createElement('pre');
        output.className = 'terminal-output';
        output.textContent = 'Connecting\u2026';
        el.appendChild(output);

        /* input bar */
        var bar = document.createElement('div');
        bar.className = 'terminal-input-bar';

        var input = document.createElement('input');
        input.type = 'text';
        input.className = 'terminal-input';
        input.placeholder = 'Type command\u2026';

        var send = document.createElement('button');
        send.className = 'btn btn-primary btn-sm';
        send.textContent = 'Send';

        function doSend() {
            var txt = input.value;
            if (!txt) return;
            input.value = '';
            if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ text: txt }));
            else sendKeys(t.id, txt, false);
        }

        input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); doSend(); } });
        send.addEventListener('click', doSend);

        bar.appendChild(input);
        bar.appendChild(send);
        el.appendChild(bar);
        panel.appendChild(el);

        requestAnimationFrame(function () {
            el.classList.add('open');
            input.focus();
        });

        connectWS(t.id, output);
    }

    function startRename(nameEl, t) {
        var inp = document.createElement('input');
        inp.className = 'rename-input';
        inp.value = t.alias || t.name;
        nameEl.replaceWith(inp);
        inp.focus();
        inp.select();

        function commit() {
            var v = inp.value.trim();
            if (v && v !== (t.alias || t.name)) {
                setAlias(t.id, v).then(function (ok) {
                    if (ok) t.alias = v;
                    restore();
                    loadGrid();
                });
            } else { restore(); }
        }

        function restore() {
            var h2 = document.createElement('h2');
            h2.className = 'terminal-detail-name';
            h2.textContent = t.alias || t.name;
            h2.title = 'Double-click to rename';
            h2.addEventListener('dblclick', function () { startRename(h2, t); });
            inp.replaceWith(h2);
        }

        inp.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); commit(); }
            if (e.key === 'Escape') restore();
        });
        inp.addEventListener('blur', commit);
    }

    function closeDetail() {
        disconnectWS();
        activeTermId = null;
        var d = panel.querySelector('.terminal-detail');
        if (d) { d.classList.remove('open'); setTimeout(function () { d.remove(); }, 360); }
    }

    /* --- websocket --- */

    function connectWS(id, outputEl) {
        disconnectWS();
        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        var url = proto + '//' + location.host + '/ws/terminal/' + encodeURIComponent(id);
        var cookies = document.cookie.split(';');
        for (var i = 0; i < cookies.length; i++) {
            var c = cookies[i].trim();
            if (c.indexOf('onecmd_session=') === 0) {
                url += '?token=' + encodeURIComponent(c.substring('onecmd_session='.length));
                break;
            }
        }
        ws = new WebSocket(url);
        ws.onmessage = function (ev) {
            try {
                var d = JSON.parse(ev.data);
                if (d.error) { window.showToast(d.error, 'error'); return; }
                if (d.content !== undefined) {
                    outputEl.textContent = d.content;
                    outputEl.scrollTop = outputEl.scrollHeight;
                }
            } catch (e) {}
        };
        ws.onclose = function (ev) {
            if (ev.code === 4001) window.showToast('WebSocket auth failed.', 'error');
        };
    }

    function disconnectWS() {
        if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    }

    /* --- lifecycle --- */

    async function loadGrid() {
        renderGrid(await fetchTerminals());
    }

    function startRefresh() { stopRefresh(); refreshTimer = setInterval(loadGrid, 10000); }
    function stopRefresh() { if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; } }

    new MutationObserver(function () {
        if (panel.classList.contains('active')) { loadGrid(); startRefresh(); }
        else { stopRefresh(); closeDetail(); }
    }).observe(panel, { attributes: true, attributeFilter: ['class'] });

    window.initTerminals = function () { loadGrid(); startRefresh(); };

    if (panel.classList.contains('active')) window.initTerminals();
})();
