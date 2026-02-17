// EverythingLogger - Frontend Application

const App = {
    ws: null,
    autoScroll: true,
    liveMode: true,
    currentPage: 0,
    pageSize: 200,
    totalEntries: 0,
    sortBy: 'received_at',
    sortOrder: 'DESC',
    filters: {},
    reconnectAttempts: 0,
    maxReconnectAttempts: 50,
    reconnectDelay: 2000,
    knownHosts: [],
    knownIPs: new Set(),      // for O(1) new-host detection
    lastSeenId: 0,            // highest entry ID displayed; used for catch-up on reconnect

    init() {
        this.logContainer = document.getElementById('log-container');
        this.logBody = document.getElementById('log-body');
        this.statusDot = document.getElementById('status-dot');
        this.statusText = document.getElementById('status-text');
        this.entryCount = document.getElementById('entry-count');
        this.hostCount = document.getElementById('host-count');
        this.pageInfo = document.getElementById('page-info');
        this.scrollBtn = document.getElementById('scroll-to-bottom');

        this.bindEvents();
        this.connectWebSocket();
        this.loadLogs();
        this.loadHosts();

        // Track scroll position for auto-scroll
        this.logContainer.addEventListener('scroll', () => {
            const el = this.logContainer;
            const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
            this.autoScroll = atBottom;
            this.scrollBtn.classList.toggle('visible', !atBottom && this.liveMode);
        });
    },

    bindEvents() {
        // Filter inputs
        document.getElementById('filter-ip').addEventListener('change', () => this.applyFilters());
        document.getElementById('filter-host').addEventListener('input',
            this.debounce(() => this.applyFilters(), 400));
        document.getElementById('filter-severity').addEventListener('change', () => this.applyFilters());
        document.getElementById('filter-search').addEventListener('input',
            this.debounce(() => this.applyFilters(), 400));
        document.getElementById('filter-start').addEventListener('change', () => this.applyFilters());
        document.getElementById('filter-end').addEventListener('change', () => this.applyFilters());

        // Buttons
        document.getElementById('btn-clear-filters').addEventListener('click', () => this.clearFilters());
        document.getElementById('btn-live').addEventListener('click', () => this.toggleLive());
        document.getElementById('btn-marker').addEventListener('click', () => this.showMarkerModal());
        document.getElementById('btn-export').addEventListener('click', () => this.exportCSV());
        document.getElementById('scroll-to-bottom').addEventListener('click', () => this.scrollToBottom());

        // Pagination
        document.getElementById('btn-prev').addEventListener('click', () => this.prevPage());
        document.getElementById('btn-next').addEventListener('click', () => this.nextPage());

        // Marker modal
        document.getElementById('marker-cancel').addEventListener('click', () => this.hideMarkerModal());
        document.getElementById('marker-submit').addEventListener('click', () => this.submitMarker());

        // Modal overlay click to close
        document.getElementById('marker-modal').addEventListener('click', (e) => {
            if (e.target === document.getElementById('marker-modal')) this.hideMarkerModal();
        });

        // Sortable column headers
        document.querySelectorAll('th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const col = th.dataset.sort;
                if (this.sortBy === col) {
                    this.sortOrder = this.sortOrder === 'DESC' ? 'ASC' : 'DESC';
                } else {
                    this.sortBy = col;
                    this.sortOrder = col === 'severity' ? 'ASC' : 'DESC';
                }
                this.updateSortIndicators();
                this.loadLogs();
            });
        });

        // Keyboard shortcut: Escape to clear filters
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                this.hideMarkerModal();
            }
        });
    },

    // ---------- WebSocket ----------

    connectWebSocket() {
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        this.ws = new WebSocket(`${proto}://${location.host}/ws`);

        this.ws.onopen = () => {
            const wasReconnect = this.reconnectAttempts > 0;
            this.reconnectAttempts = 0;
            this.statusDot.className = 'status-dot connected';
            this.statusText.textContent = 'Live';

            // Catch up on any entries that arrived while disconnected
            if (wasReconnect && this.lastSeenId > 0) {
                this.catchUp();
            }
        };

        this.ws.onmessage = (event) => {
            if (event.data === 'pong') return;

            try {
                const entry = JSON.parse(event.data);

                // Always track the latest ID and update counts, even when
                // the user has scrolled away or has live mode off, so the
                // display stays accurate without any polling.
                if (entry.id && entry.id > this.lastSeenId) {
                    this.lastSeenId = entry.id;
                }
                this.totalEntries++;
                this.updatePagination();
                this.updateEntryCount();

                // If this IP is new, add it to the host dropdown immediately
                if (entry.source_ip && entry.source_ip !== 'marker'
                        && !this.knownIPs.has(entry.source_ip)) {
                    this.knownIPs.add(entry.source_ip);
                    this.addHostToDropdown(entry);
                    this.hostCount.textContent = this.knownIPs.size;
                }

                if (!this.liveMode) return;

                this.appendLogRow(entry);
                if (this.autoScroll) {
                    this.scrollToBottom();
                }
            } catch (e) {
                console.error('Failed to parse WS message:', e);
            }
        };

        this.ws.onclose = () => {
            this.statusDot.className = 'status-dot disconnected';
            this.statusText.textContent = 'Disconnected';
            this.scheduleReconnect();
        };

        this.ws.onerror = () => {
            this.ws.close();
        };

        // Keepalive ping
        this._pingInterval = setInterval(() => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send('ping');
            }
        }, 30000);
    },

    scheduleReconnect() {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) return;
        this.reconnectAttempts++;
        const delay = Math.min(this.reconnectDelay * this.reconnectAttempts, 30000);
        setTimeout(() => this.connectWebSocket(), delay);
    },

    // ---------- Data Loading ----------

    async loadLogs() {
        const params = new URLSearchParams();
        params.set('limit', this.pageSize);
        params.set('offset', this.currentPage * this.pageSize);
        params.set('sort_by', this.sortBy);
        params.set('sort_order', this.sortOrder);

        if (this.filters.source_ip) params.set('source_ip', this.filters.source_ip);
        if (this.filters.hostname) params.set('hostname', this.filters.hostname);
        if (this.filters.severity !== undefined && this.filters.severity !== '')
            params.set('severity', this.filters.severity);
        if (this.filters.search) params.set('search', this.filters.search);
        if (this.filters.start_time) params.set('start_time', this.filters.start_time);
        if (this.filters.end_time) params.set('end_time', this.filters.end_time);

        try {
            const resp = await fetch(`/api/logs?${params}`);
            const data = await resp.json();
            this.totalEntries = data.total;
            this.renderLogTable(data.entries);
            this.updatePagination();
            this.updateEntryCount();

            // Seed lastSeenId from the loaded entries
            data.entries.forEach(e => {
                if (e.id && e.id > this.lastSeenId) this.lastSeenId = e.id;
            });
        } catch (e) {
            console.error('Failed to load logs:', e);
        }
    },

    async loadHosts() {
        try {
            const resp = await fetch('/api/hosts');
            const data = await resp.json();
            this.knownHosts = data.hosts;
            // Seed the known-IP set so the WebSocket handler can detect new ones
            this.knownIPs = new Set(data.hosts.map(h => h.ip));
            this.updateHostFilter();
            this.hostCount.textContent = this.knownHosts.length;
        } catch (e) {
            console.error('Failed to load hosts:', e);
        }
    },

    // Fetch entries that arrived while the WebSocket was disconnected
    async catchUp() {
        try {
            const params = new URLSearchParams({
                sort_by: 'id',
                sort_order: 'ASC',
                limit: 500,
            });
            const resp = await fetch(`/api/logs?${params}`);
            const data = await resp.json();

            // Only append entries newer than what we've already seen
            const newEntries = data.entries.filter(e => e.id > this.lastSeenId);
            if (newEntries.length === 0) return;

            newEntries.forEach(entry => {
                if (entry.id > this.lastSeenId) this.lastSeenId = entry.id;
                this.totalEntries++;

                if (entry.source_ip && entry.source_ip !== 'marker'
                        && !this.knownIPs.has(entry.source_ip)) {
                    this.knownIPs.add(entry.source_ip);
                    this.addHostToDropdown(entry);
                }

                if (this.liveMode) this.appendLogRow(entry);
            });

            this.updatePagination();
            this.updateEntryCount();
            this.hostCount.textContent = this.knownIPs.size;

            if (this.liveMode && this.autoScroll) this.scrollToBottom();
        } catch (e) {
            console.error('Catch-up fetch failed:', e);
        }
    },

    // ---------- Rendering ----------

    renderLogTable(entries) {
        this.logBody.innerHTML = '';
        entries.forEach(entry => this.appendLogRow(entry, false));
    },

    updateEntryCount() {
        this.entryCount.textContent = this.totalEntries.toLocaleString();
    },

    // Add a single new host to the dropdown without rebuilding the whole list
    addHostToDropdown(entry) {
        const select = document.getElementById('filter-ip');
        const opt = document.createElement('option');
        opt.value = entry.source_ip;
        opt.textContent = entry.hostname || entry.source_ip;
        select.appendChild(opt);
    },

    appendLogRow(entry, isLive = true) {
        const tr = document.createElement('tr');

        // Determine row class
        if (entry.is_marker) {
            tr.className = `marker-row marker-${entry.marker_style || 'default'}`;
        } else if (entry.is_syslog && entry.severity !== null) {
            tr.className = `sev-${entry.severity}`;
        } else {
            tr.className = 'raw-msg';
        }

        // Format timestamp
        const ts = this.formatTimestamp(entry.timestamp);

        if (entry.is_marker) {
            tr.innerHTML = `
                <td colspan="6" class="msg-cell">
                    &#9646;&#9646;&#9646; ${this.escapeHtml(entry.message)} &#9646;&#9646;&#9646;
                    <span style="float:right; font-size:11px; font-weight:normal; opacity:0.7">${ts}</span>
                </td>`;
        } else {
            const sevCell = entry.severity_name
                ? `<span class="severity-badge">${entry.severity_name}</span>`
                : '<span style="color:var(--text-muted)">raw</span>';

            tr.innerHTML = `
                <td>${ts}</td>
                <td>${this.escapeHtml(entry.source_ip || '')}</td>
                <td>${this.escapeHtml(entry.hostname || entry.source_ip || '')}</td>
                <td>${sevCell}</td>
                <td>${this.escapeHtml(entry.app_name || '')}</td>
                <td class="msg-cell">${this.escapeHtml(entry.message || '')}</td>`;
        }

        this.logBody.appendChild(tr);

        // Keep DOM manageable in live mode (max ~2000 rows)
        if (isLive && this.logBody.children.length > 2000) {
            this.logBody.removeChild(this.logBody.firstChild);
        }
    },

    updateHostFilter() {
        const select = document.getElementById('filter-ip');
        const currentVal = select.value;
        // Keep "All Sources" option
        select.innerHTML = '<option value="">All Sources</option>';
        this.knownHosts.forEach(h => {
            const opt = document.createElement('option');
            opt.value = h.ip;
            opt.textContent = h.display_name || h.hostname || h.ip;
            select.appendChild(opt);
        });
        select.value = currentVal;
    },

    updateSortIndicators() {
        document.querySelectorAll('th[data-sort]').forEach(th => {
            const arrow = th.querySelector('.sort-arrow');
            if (th.dataset.sort === this.sortBy) {
                arrow.textContent = this.sortOrder === 'ASC' ? ' \u25B2' : ' \u25BC';
            } else {
                arrow.textContent = '';
            }
        });
    },

    updatePagination() {
        const totalPages = Math.max(1, Math.ceil(this.totalEntries / this.pageSize));
        const currentDisplay = this.currentPage + 1;
        this.pageInfo.textContent = `Page ${currentDisplay} / ${totalPages} (${this.totalEntries.toLocaleString()} entries)`;
        document.getElementById('btn-prev').disabled = this.currentPage === 0;
        document.getElementById('btn-next').disabled = currentDisplay >= totalPages;
    },

    // ---------- Filtering ----------

    applyFilters() {
        this.filters = {
            source_ip: document.getElementById('filter-ip').value,
            hostname: document.getElementById('filter-host').value,
            severity: document.getElementById('filter-severity').value,
            search: document.getElementById('filter-search').value,
            start_time: this.dateInputToISO(document.getElementById('filter-start').value),
            end_time: this.dateInputToISO(document.getElementById('filter-end').value),
        };
        this.currentPage = 0;
        this.loadLogs();
    },

    clearFilters() {
        document.getElementById('filter-ip').value = '';
        document.getElementById('filter-host').value = '';
        document.getElementById('filter-severity').value = '';
        document.getElementById('filter-search').value = '';
        document.getElementById('filter-start').value = '';
        document.getElementById('filter-end').value = '';
        this.filters = {};
        this.currentPage = 0;
        this.loadLogs();
    },

    // ---------- Live Mode ----------

    toggleLive() {
        this.liveMode = !this.liveMode;
        const btn = document.getElementById('btn-live');
        btn.classList.toggle('active', this.liveMode);
        btn.textContent = this.liveMode ? 'Live: ON' : 'Live: OFF';

        if (this.liveMode) {
            this.autoScroll = true;
            this.loadLogs();
        }
    },

    // ---------- Pagination ----------

    prevPage() {
        if (this.currentPage > 0) {
            this.currentPage--;
            this.loadLogs();
        }
    },

    nextPage() {
        const totalPages = Math.ceil(this.totalEntries / this.pageSize);
        if (this.currentPage + 1 < totalPages) {
            this.currentPage++;
            this.loadLogs();
        }
    },

    // ---------- Markers ----------

    showMarkerModal() {
        document.getElementById('marker-modal').classList.add('active');
        document.getElementById('marker-label').focus();
        // Pre-fill timestamp with now
        const now = new Date();
        now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
        document.getElementById('marker-time').value = now.toISOString().slice(0, 16);
    },

    hideMarkerModal() {
        document.getElementById('marker-modal').classList.remove('active');
    },

    async submitMarker() {
        const label = document.getElementById('marker-label').value.trim();
        if (!label) return;

        const timeVal = document.getElementById('marker-time').value;
        const style = document.getElementById('marker-style').value;

        let timestamp = null;
        if (timeVal) {
            timestamp = new Date(timeVal).toISOString();
        }

        try {
            await fetch('/api/markers', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ label, timestamp, style }),
            });
            this.hideMarkerModal();
            document.getElementById('marker-label').value = '';
        } catch (e) {
            console.error('Failed to create marker:', e);
        }
    },

    // ---------- Export ----------

    exportCSV() {
        const params = new URLSearchParams();
        params.set('sort_by', 'timestamp');
        params.set('sort_order', 'ASC');

        if (this.filters.source_ip) params.set('source_ip', this.filters.source_ip);
        if (this.filters.hostname) params.set('hostname', this.filters.hostname);
        if (this.filters.severity !== undefined && this.filters.severity !== '')
            params.set('severity', this.filters.severity);
        if (this.filters.search) params.set('search', this.filters.search);
        if (this.filters.start_time) params.set('start_time', this.filters.start_time);
        if (this.filters.end_time) params.set('end_time', this.filters.end_time);

        window.location.href = `/api/export?${params}`;
    },

    // ---------- Helpers ----------

    scrollToBottom() {
        this.logContainer.scrollTop = this.logContainer.scrollHeight;
    },

    formatTimestamp(ts) {
        if (!ts) return '';
        try {
            const d = new Date(ts);
            if (isNaN(d.getTime())) return ts;
            return d.toLocaleString('en-US', {
                month: '2-digit', day: '2-digit', year: '2-digit',
                hour: '2-digit', minute: '2-digit', second: '2-digit',
                hour12: false,
            });
        } catch {
            return ts;
        }
    },

    dateInputToISO(val) {
        if (!val) return '';
        try {
            return new Date(val).toISOString();
        } catch {
            return '';
        }
    },

    escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    },

    debounce(fn, delay) {
        let timer;
        return function (...args) {
            clearTimeout(timer);
            timer = setTimeout(() => fn.apply(this, args), delay);
        };
    },
};

// Boot
document.addEventListener('DOMContentLoaded', () => App.init());
