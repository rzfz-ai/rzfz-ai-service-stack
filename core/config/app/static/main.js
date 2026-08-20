/* rzfz.ai Configuration — Main JS */
'use strict';

const applyDialog = {
    _data: null,
    _actionId: null,
    _eventSource: null,

    open(action, params) {
        this._data = { action, ...params };
        this._actionId = null;

        // Fetch preview
        fetch('/api/apply/preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(this._data),
        })
        .then(r => r.json())
        .then(preview => {
            if (preview.error) {
                this._showError(preview.error);
                return;
            }
            this._renderPreview(preview);
            document.getElementById('apply-overlay').classList.remove('hidden');
        })
        .catch(err => {
            this._showError('Failed to fetch preview: ' + err.message);
        });
    },

    _renderPreview(p) {
        // Reset state
        document.getElementById('apply-preview').classList.remove('hidden');
        document.getElementById('apply-terminal').classList.remove('hidden');
        document.getElementById('apply-terminal').classList.add('hidden');
        document.getElementById('apply-error').classList.add('hidden');
        document.getElementById('apply-conflict').classList.add('hidden');
        document.getElementById('apply-confirm-btn').disabled = false;

        // Title
        const verb = p.enable ? 'Enable' : 'Disable';
        document.getElementById('apply-title').textContent = verb + ' ' + p.profile_name + '?';

        // Impact text
        document.getElementById('apply-impact-text').textContent = p.impact_text;

        // Containers
        document.getElementById('apply-containers').textContent =
            p.containers.join(', ') + ' (' + p.container_count + ')';

        // Risk badge
        const riskEl = document.getElementById('apply-risk');
        riskEl.innerHTML = '<span class="badge badge-risk-' + p.risk + '">' + p.risk + '</span>';

        // Memory bar
        if (p.memory && p.memory.total_mb > 0) {
            const mem = p.memory;
            const currentPct = (mem.current_used_mb / mem.total_mb * 100);
            const projectedPct = mem.projected_pct;

            document.getElementById('memory-bar-current').style.width = Math.min(currentPct, 100) + '%';

            const deltaEl = document.getElementById('memory-bar-delta');
            if (p.enable) {
                deltaEl.style.left = currentPct + '%';
                deltaEl.style.width = Math.min(projectedPct - currentPct, 100 - currentPct) + '%';
                deltaEl.className = 'memory-bar-delta memory-level-' + mem.level;
            } else {
                deltaEl.style.width = '0';
            }

            const totalGb = (mem.total_mb / 1024).toFixed(1);
            const projGb = (mem.projected_used_mb / 1024).toFixed(1);
            const deltaSign = mem.delta_mb >= 0 ? '+' : '';
            const deltaMb = Math.abs(mem.delta_mb);
            document.getElementById('memory-bar-text').textContent =
                projGb + ' / ' + totalGb + ' GB (' + mem.projected_pct + '%) — ' +
                deltaSign + (deltaMb >= 1024 ? (deltaMb/1024).toFixed(1) + ' GB' : deltaMb + ' MB');

            // Warning
            const warnEl = document.getElementById('memory-warning');
            if (mem.level === 'blocked') {
                warnEl.textContent = 'Not enough memory. Disable other modules first.';
                warnEl.className = 'memory-warning memory-level-blocked';
                document.getElementById('apply-confirm-btn').disabled = true;
            } else if (mem.level === 'critical') {
                warnEl.textContent = 'High risk of OOM. Some containers may be killed by the OS.';
                warnEl.className = 'memory-warning memory-level-critical';
            } else if (mem.level === 'warning') {
                warnEl.textContent = 'Consider whether all enabled modules are needed.';
                warnEl.className = 'memory-warning memory-level-warning';
            } else {
                warnEl.classList.add('hidden');
            }

            document.getElementById('apply-memory-section').classList.remove('hidden');
        } else {
            document.getElementById('apply-memory-section').classList.add('hidden');
        }

        // Exclusion conflict
        if (p.exclusion_conflict) {
            document.getElementById('apply-conflict').textContent = p.exclusion_conflict;
            document.getElementById('apply-conflict').classList.remove('hidden');
            document.getElementById('apply-confirm-btn').disabled = true;
        }

        // Password field
        const pwSection = document.getElementById('apply-password-section');
        const pwInput = document.getElementById('apply-password');
        if (p.requires_password) {
            pwSection.classList.remove('hidden');
            pwInput.value = '';
        } else {
            pwSection.classList.add('hidden');
        }
    },

    _showError(msg) {
        const el = document.getElementById('apply-error');
        el.textContent = msg;
        el.classList.remove('hidden');
        document.getElementById('apply-overlay').classList.remove('hidden');
        document.getElementById('apply-preview').classList.remove('hidden');
        document.getElementById('apply-terminal').classList.add('hidden');
    },

    confirm() {
        const btn = document.getElementById('apply-confirm-btn');
        btn.disabled = true;
        btn.textContent = 'Applying...';

        const payload = { ...this._data };
        const pw = document.getElementById('apply-password').value;
        if (pw) payload.password = pw;

        fetch('/api/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
        .then(r => r.json())
        .then(resp => {
            if (resp.error) {
                document.getElementById('apply-error').textContent = resp.error;
                document.getElementById('apply-error').classList.remove('hidden');
                btn.disabled = false;
                btn.textContent = 'Confirm & Apply';
                return;
            }

            this._actionId = resp.action_id;
            this._startStream();
        })
        .catch(err => {
            document.getElementById('apply-error').textContent = 'Request failed: ' + err.message;
            document.getElementById('apply-error').classList.remove('hidden');
            btn.disabled = false;
            btn.textContent = 'Confirm & Apply';
        });
    },

    _startStream() {
        // Switch to terminal view
        document.getElementById('apply-preview').classList.add('hidden');
        document.getElementById('apply-terminal').classList.remove('hidden');

        const terminal = document.getElementById('terminal-output');
        terminal.innerHTML = '';

        this._eventSource = new EventSource('/api/apply/' + this._actionId + '/stream');

        this._eventSource.onmessage = (event) => {
            const data = JSON.parse(event.data);

            if (data.type === 'line') {
                const line = document.createElement('div');
                line.className = 'terminal-line';
                line.textContent = data.text;
                terminal.appendChild(line);
                terminal.scrollTop = terminal.scrollHeight;
            }

            if (data.type === 'done') {
                this._eventSource.close();
                this._eventSource = null;

                const resultEl = document.getElementById('apply-result');
                const iconEl = document.getElementById('apply-result-icon');
                const textEl = document.getElementById('apply-result-text');
                const durEl = document.getElementById('apply-result-duration');

                if (data.success) {
                    iconEl.textContent = '\u2713';
                    iconEl.className = 'result-success';
                    textEl.textContent = 'Applied successfully';
                } else {
                    iconEl.textContent = '\u2717';
                    iconEl.className = 'result-failure';
                    textEl.textContent = 'Failed: ' + (data.error || 'unknown error');
                }

                durEl.textContent = (data.duration_ms / 1000).toFixed(1) + 's';
                resultEl.classList.remove('hidden');
                document.getElementById('apply-done-btn').classList.remove('hidden');
            }
        };

        this._eventSource.onerror = () => {
            if (this._eventSource) {
                this._eventSource.close();
                this._eventSource = null;
            }
        };
    },

    close() {
        if (this._eventSource) {
            this._eventSource.close();
            this._eventSource = null;
        }
        document.getElementById('apply-overlay').classList.add('hidden');
        document.getElementById('apply-error').classList.add('hidden');
    },

    done() {
        this.close();
        window.location.reload();
    },
};

// Toggle button handler
function toggleProfile(profileId, currentlyEnabled) {
    applyDialog.open('profile_toggle', {
        profile_id: profileId,
        enable: !currentlyEnabled,
    });
}

// Per-image update: bump .env version, pull, and recreate via apply dialog
function applyImageUpdate(imageKey, btn) {
    btn.disabled = true;
    btn.textContent = 'Updating...';

    fetch('/api/updates/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: imageKey }),
    })
    .then(r => r.json())
    .then(data => {
        if (data.error) {
            btn.textContent = 'Failed';
            btn.classList.remove('btn-primary');
            btn.classList.add('btn-secondary');
            alert('Update failed: ' + data.error);
            return;
        }
        // Open the apply dialog terminal to stream progress
        document.getElementById('apply-overlay').classList.remove('hidden');
        document.getElementById('apply-preview').classList.add('hidden');
        document.getElementById('apply-terminal').classList.remove('hidden');
        document.getElementById('apply-error').classList.add('hidden');
        document.getElementById('apply-result-icon').textContent = '';
        document.getElementById('apply-result-text').textContent = '';
        document.getElementById('apply-done-btn').classList.add('hidden');
        const termEl = document.getElementById('terminal-output');
        termEl.innerHTML = '';

        // Stream SSE output
        const es = new EventSource('/api/apply/' + data.action_id + '/stream');
        es.onmessage = function(event) {
            const msg = JSON.parse(event.data);
            if (msg.type === 'line') {
                const line = document.createElement('div');
                line.className = 'terminal-line';
                line.textContent = msg.text;
                termEl.appendChild(line);
                termEl.scrollTop = termEl.scrollHeight;
            }
            if (msg.type === 'done') {
                es.close();
                document.getElementById('apply-result-icon').textContent = msg.success ? '\u2713' : '\u2717';
                document.getElementById('apply-result-text').textContent = msg.success ? 'Update applied' : 'Failed: ' + (msg.error || '');
                document.getElementById('apply-done-btn').classList.remove('hidden');
                btn.textContent = msg.success ? 'Done' : 'Failed';
                if (msg.success) {
                    btn.classList.remove('btn-primary');
                    btn.classList.add('btn-secondary');
                }
            }
        };
        es.onerror = function() {
            es.close();
            btn.textContent = 'Error';
        };
    })
    .catch(err => {
        btn.disabled = false;
        btn.textContent = 'Update';
        alert('Request failed: ' + err.message);
    });
}

document.addEventListener('DOMContentLoaded', function() {
    // Wire up toggle buttons
    document.querySelectorAll('[data-toggle-profile]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            const profileId = btn.dataset.toggleProfile;
            const enabled = btn.dataset.enabled === 'true';
            toggleProfile(profileId, enabled);
        });
    });
});
