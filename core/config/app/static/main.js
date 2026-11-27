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
            headers: window.rzfzCsrfHeaders({ 'Content-Type': 'application/json' }),
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
            headers: window.rzfzCsrfHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify(payload),
        })
        .then(r => r.json())
        .then(resp => {
            if (resp.self_heal) {
                this._followSelfHeal(resp.self_heal);
                return;
            }
            if (resp.error) {
                const errEl = document.getElementById('apply-error');
                errEl.textContent = resp.error;
                // #2993: another apply is running - offer its progress instead
                // of leaving the caller with a bare refusal.
                if (resp.running_action_id) {
                    const follow = document.createElement('button');
                    follow.type = 'button';
                    follow.className = 'btn btn-secondary apply-follow-running';
                    follow.textContent = 'Show its progress';
                    follow.addEventListener('click', () => {
                        errEl.classList.add('hidden');
                        this._actionId = resp.running_action_id;
                        this._startStream();
                    });
                    errEl.appendChild(document.createTextNode(' '));
                    errEl.appendChild(follow);
                }
                errEl.classList.remove('hidden');
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

    // The portal is recreating itself to repair its .env bind. The dialog stays
    // open: the toggle resumes in the new portal, or Confirm offers it again.
    _followSelfHeal(info) {
        const btn = document.getElementById('apply-confirm-btn');
        const errEl = document.getElementById('apply-error');
        const say = (text) => {
            errEl.textContent = text;
            errEl.classList.remove('hidden');
        };
        const offer = (text, token) => {
            window.rzfzSelfHeal.adoptCsrf(document, token);
            say(text);
            btn.disabled = false;
            btn.textContent = 'Apply again';
        };
        say(info.message);
        if (info.state !== 'reconnecting') {
            btn.disabled = false;
            btn.textContent = 'Confirm & Apply';
            return;
        }
        btn.disabled = true;
        btn.textContent = 'Reconnecting...';
        window.rzfzSelfHeal.watch(fetch.bind(window), info, {
            onResumed: (actionId) => {
                errEl.classList.add('hidden');
                this._actionId = actionId;
                this._startStream();
            },
            onOffer: offer,
            onFailed: (text) => offer(text, null),
        });
    },

    // #2993: follow the action by polling its status. One short request a
    // second, never a long-lived stream: the portal serves one request at a
    // time, and a stream held open over an apply that waits for an unhealthy
    // service blocked every other page, including a Cancel.
    _startStream() {
        document.getElementById('apply-preview').classList.add('hidden');
        document.getElementById('apply-terminal').classList.remove('hidden');
        document.getElementById('apply-result').classList.add('hidden');
        document.getElementById('apply-done-btn').classList.add('hidden');
        document.getElementById('terminal-output').innerHTML = '';
        this._next = 0;
        this._following = this._actionId;
        this._poll();
    },

    _poll() {
        const id = this._following;
        if (!id) return;
        fetch('/api/apply/' + id + '?from=' + (this._next || 0))
        .then(r => r.json())
        .then(st => {
            if (this._following !== id) return;
            if (!st.action_id) {
                this._renderStatus({ state: 'failed' });
                this._renderResult({ state: 'failed', error: st.error || 'the action is no longer known' });
                return;
            }
            const terminal = document.getElementById('terminal-output');
            (st.lines || []).forEach(text => {
                const line = document.createElement('div');
                line.className = 'terminal-line';
                line.textContent = text;
                terminal.appendChild(line);
            });
            terminal.scrollTop = terminal.scrollHeight;
            if (typeof st.next === 'number') this._next = st.next;
            this._renderStatus(st);
            if (st.done) {
                this._following = null;
                this._renderResult(st);
            } else {
                this._timer = setTimeout(() => this._poll(), 1000);
            }
        })
        .catch(() => {
            // The portal may be restarting (it reconnects itself): keep asking.
            if (this._following === id) this._timer = setTimeout(() => this._poll(), 2000);
        });
    },

    _elapsed(s) {
        s = Math.max(0, s || 0);
        const m = Math.floor(s / 60);
        return m ? m + 'm ' + String(s % 60).padStart(2, '0') + 's' : s + 's';
    },

    _renderStatus(st) {
        const el = document.getElementById('apply-job-status');
        const note = document.getElementById('apply-job-note');
        const btn = document.getElementById('apply-job-cancel');
        let text = '';
        if (st.step) text = 'Step ' + st.step_index + ' of ' + st.step_total + ': ' + st.step;
        if (st.waiting_for && st.waiting_for.length) {
            text += ' \u2014 waiting for ' + st.waiting_for.join(', ') + ' to become healthy';
        }
        if (!st.done && typeof st.elapsed_s === 'number') text += ' \u00b7 ' + this._elapsed(st.elapsed_s);
        if (st.state === 'cancelling') text += ' \u00b7 cancelling...';
        el.textContent = text;
        if (!st.done && st.cancel_note) {
            note.textContent = (st.cancellable ? 'Cancel: ' : '') + st.cancel_note;
            note.classList.remove('hidden');
        } else {
            note.classList.add('hidden');
        }
        if (!st.done && st.cancellable) {
            btn.classList.remove('hidden');
            btn.disabled = false;
        } else {
            btn.classList.add('hidden');
        }
    },

    _renderResult(st) {
        const iconEl = document.getElementById('apply-result-icon');
        const textEl = document.getElementById('apply-result-text');
        if (st.state === 'cancelled') {
            iconEl.textContent = '\u2013';
            iconEl.className = 'result-cancelled';
            textEl.textContent = st.error || 'Cancelled';
        } else if (st.success) {
            iconEl.textContent = '\u2713';
            iconEl.className = 'result-success';
            textEl.textContent = 'Applied successfully';
        } else {
            iconEl.textContent = '\u2717';
            iconEl.className = 'result-failure';
            textEl.textContent = 'Failed: ' + (st.error || 'unknown error');
        }
        document.getElementById('apply-result-duration').textContent =
            ((st.duration_ms || 0) / 1000).toFixed(1) + 's';
        document.getElementById('apply-result').classList.remove('hidden');
        document.getElementById('apply-done-btn').classList.remove('hidden');
    },

    cancel() {
        const id = this._following;
        if (!id) return;
        const btn = document.getElementById('apply-job-cancel');
        btn.disabled = true;
        fetch('/api/apply/' + id + '/cancel', {
            method: 'POST',
            headers: window.rzfzCsrfHeaders({ 'Content-Type': 'application/json' }),
            body: '{}',
        })
        .then(r => r.json())
        .then(resp => {
            const note = document.getElementById('apply-job-note');
            note.textContent = resp.error ? resp.error : 'Cancelling: ' + (resp.message || '');
            note.classList.remove('hidden');
            if (resp.error) btn.disabled = false;
        })
        .catch(err => { btn.disabled = false; });
    },

    // A page opened while a change runs shows that change (#2993).
    followRunning() {
        if (!document.getElementById('apply-overlay')) return;
        fetch('/api/apply/current')
        .then(r => r.json())
        .then(resp => {
            if (!resp || !resp.running || this._following) return;
            document.getElementById('apply-title').textContent = resp.running.description || 'Change in progress';
            document.getElementById('apply-overlay').classList.remove('hidden');
            this._actionId = resp.running.action_id;
            this._startStream();
        })
        .catch(() => {});
    },

    close() {
        if (this._eventSource) {
            this._eventSource.close();
            this._eventSource = null;
        }
        // Closing the dialog does not cancel the change; it keeps running.
        this._following = null;
        if (this._timer) { clearTimeout(this._timer); this._timer = null; }
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
        headers: window.rzfzCsrfHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ key: imageKey }),
    })
    .then(r => r.json())
    .then(data => {
        if (data.self_heal) {
            btn.disabled = false;
            btn.textContent = 'Update';
            window.rzfzSelfHeal.follow(document, fetch.bind(window), data.self_heal,
                                       () => applyImageUpdate(imageKey, btn));
            return;
        }
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
    applyDialog.followRunning();
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
