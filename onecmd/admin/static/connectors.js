/* ---------------------------------------------------------------
 *  Connectors tab — status cards
 * --------------------------------------------------------------- */
(function () {
    'use strict';

    var panel = document.getElementById('tab-connectors');
    var refreshTimer = null;

    async function apiFetch(url) {
        var res = await fetch(url);
        if (res.status === 401) { window.showToast('Session expired.', 'error'); return null; }
        return res;
    }

    function fmtUptime(s) {
        s = Math.floor(s);
        var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
        return h + 'h ' + m + 'm ' + sec + 's';
    }

    function icon(type) {
        return { telegram: '\uD83D\uDCF1' }[type] || '\uD83D\uDD0C';
    }

    async function fetchList() {
        var r = await apiFetch('/api/connectors');
        return r ? r.json() : [];
    }

    async function fetchDetail(name) {
        var r = await apiFetch('/api/connectors/' + encodeURIComponent(name));
        return (r && r.ok) ? r.json() : null;
    }

    function renderCards(list) {
        panel.innerHTML = '';

        var grid = document.createElement('div');
        grid.className = 'connector-grid';

        list.forEach(function (c) {
            var card = document.createElement('div');
            card.className = 'connector-card';

            var ic = document.createElement('div');
            ic.className = 'connector-card-icon';
            ic.textContent = icon(c.type);
            card.appendChild(ic);

            var nm = document.createElement('div');
            nm.className = 'connector-card-name';
            nm.textContent = c.name.charAt(0).toUpperCase() + c.name.slice(1);
            card.appendChild(nm);

            var st = document.createElement('div');
            st.className = 'connector-card-status';
            var dot = document.createElement('span');
            dot.className = 'status-badge ' + (c.status === 'running' ? 'green' : 'red');
            var stxt = document.createElement('span');
            stxt.textContent = c.status === 'running' ? 'Running' : 'Stopped';
            st.appendChild(dot);
            st.appendChild(stxt);
            card.appendChild(st);

            var sum = document.createElement('div');
            sum.className = 'connector-card-summary';
            sum.textContent = c.config_summary;
            card.appendChild(sum);

            var det = document.createElement('div');
            det.className = 'connector-card-detail';
            card.appendChild(det);

            card.addEventListener('click', function () {
                if (det.style.display === 'block') { det.style.display = 'none'; return; }
                expand(c.name, det);
            });

            grid.appendChild(card);
        });

        /* placeholder */
        var add = document.createElement('div');
        add.className = 'connector-card connector-card-placeholder';
        add.innerHTML = '<div style="font-size:28px;color:var(--text-muted)">+</div><div style="font-size:13px;color:var(--text-muted);margin-top:6px">Coming soon</div>';
        grid.appendChild(add);

        panel.appendChild(grid);
    }

    async function expand(name, el) {
        el.style.display = 'block';
        el.innerHTML = '<span style="color:var(--text-muted)">Loading\u2026</span>';
        var d = await fetchDetail(name);
        if (!d) { el.innerHTML = '<span style="color:var(--red)">Failed.</span>'; return; }

        var lines = [];
        lines.push('<strong>Uptime</strong> &nbsp;' + fmtUptime(d.uptime_seconds));
        lines.push('<strong>OTP</strong> &nbsp;' + (d.auth.otp_enabled ? 'Enabled' : 'Disabled'));
        if (d.auth.weak_security) lines.push('<strong>Security</strong> &nbsp;<span style="color:var(--yellow)">Weak</span>');
        lines.push('<strong>Mode</strong> &nbsp;' + d.config.mode);
        lines.push('<strong>Visible lines</strong> &nbsp;' + d.config.visible_lines);
        lines.push('<strong>Split messages</strong> &nbsp;' + (d.config.split_messages ? 'Yes' : 'No'));
        el.innerHTML = lines.join('<br>');
    }

    async function loadGrid() { renderCards(await fetchList()); }
    function start() { stop(); refreshTimer = setInterval(loadGrid, 30000); }
    function stop() { if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; } }

    new MutationObserver(function () {
        if (panel.classList.contains('active')) { loadGrid(); start(); }
        else stop();
    }).observe(panel, { attributes: true, attributeFilter: ['class'] });

    window.initConnectors = function () { loadGrid(); start(); };
    if (panel.classList.contains('active')) window.initConnectors();
})();
