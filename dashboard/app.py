import os
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from db.state_store import StateStore
from tools.replay_audit import replay_and_optionally_save

app = Flask(__name__)
DB_PATH = str(Path(__file__).resolve().parents[1] / "data" / "gpt_trader.db")
state_store = StateStore(DB_PATH)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GPT Trader Control Room</title>
    <style>
        :root {
            --bg: #f2efe8;
            --panel: rgba(255, 252, 247, 0.92);
            --ink: #1f1a17;
            --muted: #6f655c;
            --accent: #b64926;
            --ok: #205c3b;
            --warn: #8e4b10;
            --bad: #8c1d18;
            --line: rgba(31, 26, 23, 0.08);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            color: var(--ink);
            background:
                radial-gradient(circle at top left, rgba(182, 73, 38, 0.16), transparent 28%),
                radial-gradient(circle at right 20%, rgba(32, 92, 59, 0.12), transparent 22%),
                linear-gradient(180deg, #f8f3ea, var(--bg));
            font-family: Georgia, "Times New Roman", serif;
        }
        .shell { max-width: 1440px; margin: 0 auto; padding: 28px 20px 48px; }
        .header {
            display: flex; justify-content: space-between; gap: 16px; align-items: end; margin-bottom: 24px;
        }
        .header h1 { margin: 0; font-size: clamp(34px, 5vw, 56px); letter-spacing: -0.04em; }
        .header p { margin: 8px 0 0; color: var(--muted); max-width: 720px; font-size: 15px; }
        .refresh, .action-button {
            border: 0; background: var(--ink); color: #fff; padding: 12px 16px; border-radius: 999px; cursor: pointer; font-size: 14px;
        }
        .action-button { background: var(--accent); }
        .grid {
            display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 14px; margin-bottom: 22px;
        }
        .card, .panel {
            background: var(--panel); backdrop-filter: blur(8px); border: 1px solid var(--line); border-radius: 22px; box-shadow: 0 16px 40px rgba(58, 39, 23, 0.08);
        }
        .card { padding: 18px 18px 20px; min-height: 140px; }
        .eyebrow { margin: 0 0 10px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); }
        .value { font-size: clamp(28px, 4vw, 44px); line-height: 1; margin: 0; }
        .sub { margin-top: 12px; color: var(--muted); font-size: 14px; }
        .layout { display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 14px; margin-bottom: 14px; }
        .stack { display: grid; gap: 14px; }
        .panel { padding: 18px; }
        table { width: 100%; border-collapse: collapse; }
        th, td {
            text-align: left; padding: 12px 8px; border-bottom: 1px solid var(--line); vertical-align: top; font-size: 14px;
        }
        th { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; }
        .audit-row { cursor: pointer; }
        .audit-row:hover { background: rgba(182, 73, 38, 0.06); }
        .badge {
            display: inline-flex; align-items: center; gap: 6px; border-radius: 999px; padding: 5px 10px; font-size: 12px; font-weight: bold;
        }
        .ok { background: rgba(32, 92, 59, 0.12); color: var(--ok); }
        .warn { background: rgba(142, 75, 16, 0.14); color: var(--warn); }
        .bad { background: rgba(140, 29, 24, 0.12); color: var(--bad); }
        .memory-item, .fill-item {
            padding: 14px 0; border-bottom: 1px solid var(--line);
        }
        .memory-item:last-child, .fill-item:last-child { border-bottom: 0; }
        .memory-item strong, .fill-item strong { display: block; font-size: 15px; margin-bottom: 6px; }
        .memory-meta, .fill-meta { color: var(--muted); font-size: 13px; }
        .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
        .detail-box {
            background: rgba(255,255,255,0.55); border: 1px solid var(--line); border-radius: 16px; padding: 14px;
        }
        .detail-box pre {
            margin: 0; font-size: 12px; line-height: 1.55; overflow: auto; white-space: pre-wrap; word-break: break-word;
        }
        mark.path-highlight {
            background: rgba(182, 73, 38, 0.2);
            color: var(--ink);
            padding: 2px 0;
            border-radius: 4px;
        }
        .source-link {
            border: 0;
            background: rgba(182, 73, 38, 0.1);
            color: var(--accent);
            border-radius: 999px;
            padding: 3px 8px;
            cursor: pointer;
            font-size: 11px;
        }
        .detail-actions { display: flex; gap: 10px; margin-bottom: 14px; }
        @media (max-width: 1080px) {
            .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .layout, .detail-grid { grid-template-columns: 1fr; }
        }
        @media (max-width: 680px) {
            .grid { grid-template-columns: 1fr; }
            .detail-actions { flex-direction: column; }
        }
    </style>
</head>
<body>
    <div class="shell">
        <div class="header">
            <div>
                <h1>Trader Control Room</h1>
                <p>Operational health, replay controls, cost drag, symbol reports, and full audit drill-down for execution review.</p>
            </div>
            <button class="refresh" onclick="window.location.reload();">Refresh</button>
        </div>

        <section class="grid">
            <article class="card">
                <p class="eyebrow">Audits 24h</p>
                <p class="value" id="val-audits">-</p>
                <p class="sub">Total trading cycle audit rows recorded today.</p>
            </article>
            <article class="card">
                <p class="eyebrow">Risk Rejections</p>
                <p class="value" id="val-rejections">-</p>
                <p class="sub">Share of intents blocked by policy enforcement.</p>
            </article>
            <article class="card">
                <p class="eyebrow">Loop Errors</p>
                <p class="value" id="val-errors">-</p>
                <p class="sub">Unhandled loop failures in the last 24 hours.</p>
            </article>
            <article class="card">
                <p class="eyebrow">Unresolved Timeouts</p>
                <p class="value" id="val-timeouts">-</p>
                <p class="sub">Timed out orders that were not recovered.</p>
            </article>
            <article class="card">
                <p class="eyebrow">Semantic Fallbacks</p>
                <p class="value" id="val-semantic-fallbacks">-</p>
                <p class="sub" id="val-semantic-top-issue">Top issue: -</p>
                <p class="sub" id="val-semantic-models">Models: -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Fees 24h</p>
                <p class="value" id="val-fees">-</p>
                <p class="sub">Total captured execution fees from synced fills.</p>
            </article>
            <article class="card">
                <p class="eyebrow">Ledger Net PnL</p>
                <p class="value" id="val-ledger-pnl">-</p>
                <p class="sub">Net realized pnl aggregated from fill ledger rows.</p>
            </article>
        </section>

        <section class="layout">
            <article class="panel">
                <p class="eyebrow">Recent Audit Logs</p>
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Decision</th>
                            <th>Status</th>
                            <th>Reason</th>
                        </tr>
                    </thead>
                    <tbody id="audit-table-body">
                        <tr><td colspan="4">Loading...</td></tr>
                    </tbody>
                </table>
            </article>
            <div class="stack">
                <article class="panel">
                    <p class="eyebrow">Recent Memory</p>
                    <div id="memory-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Recent Fills</p>
                    <div id="fill-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Recent Ledger</p>
                    <div id="ledger-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Recent Symbol Journal</p>
                    <div id="symbol-journal-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Recent Replays</p>
                    <div id="replay-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Symbol Report</p>
                    <div id="symbol-report-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Daily Report</p>
                    <div id="daily-report-list">Loading...</div>
                </article>
            </div>
        </section>

        <section class="panel">
            <p class="eyebrow">Audit Detail</p>
            <div class="detail-actions">
                <button class="action-button" id="run-replay-button" disabled>Run Replay + Save</button>
                <span class="sub" id="replay-status">Select an audit row to inspect and replay.</span>
            </div>
            <div id="audit-detail">
                <p class="sub">Select an audit row to inspect stored request, AI response, ledger rows, execution summary, and synced fills.</p>
            </div>
        </section>
    </div>

    <script>
        let selectedAuditId = null;
        let currentRequestPayload = {};
        let selectedSourcePath = '';
        let currentAuditDetail = null;

        function renderModelRouting(models) {
            const value = models || {};
            const defaultModel = value.default_model || 'unset';
            const triggerModel = value.trigger_model || defaultModel;
            const memoryModel = value.memory_model || triggerModel || defaultModel;
            return `default ${defaultModel} | trigger ${triggerModel} | memory ${memoryModel}`;
        }

        function parseJson(raw, fallback = {}) {
            if (raw && typeof raw === 'object') return raw;
            try { return JSON.parse(raw || '{}'); } catch { return fallback; }
        }

        function pretty(value) {
            return JSON.stringify(value, null, 2);
        }

        function escapeHtml(value) {
            return String(value)
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;')
                .replaceAll('"', '&quot;')
                .replaceAll("'", '&#39;');
        }

        function summarizeCandidateLessons(metrics) {
            const lessons = (metrics && metrics.candidate_lessons) || [];
            if (!lessons.length) return 'no heuristic candidates';
            return lessons.slice(0, 2).map(item => {
                const support = item.support_count || 0;
                const seed = item.seed_signal || 'signal';
                return `${seed} (${support})`;
            }).join(' | ');
        }

        function summarizeEvidenceDiff(run) {
            const added = (run.evidence_signals_added || []).slice(0, 2).join(', ') || 'none';
            const removed = (run.evidence_signals_removed || []).slice(0, 2).join(', ') || 'none';
            const delta = run.evidence_signal_count_delta || 0;
            return `evidence Δ ${delta} • + ${added} • - ${removed}`;
        }

        function renderRationaleItems(intent) {
            const items = (intent && intent.rationale_items) || [];
            if (!items.length) {
                return '<p class="sub">No structured rationale stored.</p>';
            }
            return items.map(item => {
                const evidence = (item.evidence || []).map(ev => {
                    const symbol = ev.symbol ? `${ev.symbol} ` : '';
                    const threshold = ev.threshold === null || ev.threshold === undefined ? '' : ` ${ev.operator} ${ev.threshold}`;
                    return `
                        <div class="fill-meta">
                            ${symbol}${ev.signal}@${ev.timeframe}: ${ev.observed_value}${threshold}
                            <button class="source-link" data-source="${escapeHtml(ev.source || '')}">${escapeHtml(ev.source || 'source')}</button>
                        </div>
                    `;
                }).join('');
                return `
                    <div class="memory-item">
                        <strong>${item.summary || 'Structured rationale'}</strong>
                        <div class="memory-meta">${item.why_it_matters || ''}</div>
                        <div class="memory-meta">${item.trade_implication || ''}</div>
                        <div class="memory-meta">failure mode: ${item.failure_mode || 'n/a'}</div>
                        ${evidence}
                    </div>
                `;
            }).join('');
        }

        function renderEvidenceQuality(executionSummary) {
            const evidenceQuality = executionSummary.evidence_quality || {};
            const entrySizeCap = executionSummary.entry_size_cap || {};
            if (!Object.keys(evidenceQuality).length && !Object.keys(entrySizeCap).length) {
                return '<p class="sub">No evidence quality summary stored.</p>';
            }
            return `
                <div class="memory-item">
                    <strong>Evidence quality factor ${evidenceQuality.factor ?? 'n/a'}</strong>
                    <div class="memory-meta">
                        signals ${evidenceQuality.unique_signal_count ?? 0} • evidence rows ${evidenceQuality.evidence_count ?? 0}
                        • covered symbols ${(evidenceQuality.covered_symbols || []).join(', ') || 'none'}
                    </div>
                    <div class="memory-meta">
                        issues ${(evidenceQuality.issues || []).join(', ') || 'none'}
                    </div>
                    <div class="memory-meta">
                        entry cap ${(entrySizeCap.base_cap_pct ?? 0)}% -> ${(entrySizeCap.effective_cap_pct ?? 0)}%
                        • adjusted ${entrySizeCap.adjusted ? 'yes' : 'no'}
                    </div>
                </div>
            `;
        }

        function renderReplaySourceDiffs(detail) {
            const latestRun = (detail && detail.replay_runs || [])[0];
            if (!latestRun) {
                return '<p class="sub">No replay run stored yet.</p>';
            }
            const payload = parseJson(latestRun.payload, {});
            const diffs = (((payload || {}).comparison || {}).evidence_source_diffs) || [];
            if (!diffs.length) {
                return '<p class="sub">No source-level evidence diff detected.</p>';
            }
            return `
                <table>
                    <thead>
                        <tr>
                            <th>Source</th>
                            <th>Original</th>
                            <th>Replayed</th>
                            <th>Delta</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${diffs.map(diff => {
                            const originalValue = summarizeEvidenceObservedValue(diff.original || []);
                            const replayedValue = summarizeEvidenceObservedValue(diff.replayed || []);
                            return `
                                <tr>
                                    <td>${escapeHtml(diff.source || 'source')}</td>
                                    <td>${escapeHtml(originalValue.label)}</td>
                                    <td>${escapeHtml(replayedValue.label)}</td>
                                    <td>${escapeHtml(computeObservedDeltaLabel(originalValue.numeric, replayedValue.numeric))}</td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            `;
        }

        function summarizeEvidenceObservedValue(rows) {
            const values = (rows || []).map(row => row && row.observed_value).filter(value => value !== undefined && value !== null);
            if (!values.length) {
                return { label: 'n/a', numeric: null };
            }
            if (values.length === 1) {
                return { label: String(values[0]), numeric: parseNumericValue(values[0]) };
            }
            const label = values.map(value => String(value)).join(', ');
            return { label: label, numeric: null };
        }

        function parseNumericValue(value) {
            const numeric = Number(value);
            return Number.isFinite(numeric) ? numeric : null;
        }

        function computeObservedDeltaLabel(originalValue, replayedValue) {
            if (originalValue === null || replayedValue === null) {
                return 'n/a';
            }
            const delta = replayedValue - originalValue;
            return delta > 0 ? `+${delta.toFixed(4)}` : delta.toFixed(4);
        }

        function getPreviousValuePath(path) {
            if (!path || !path.includes('.')) return '';
            const parts = path.split('.');
            const metric = parts.pop();
            return `${parts.join('.')}.previous_values.${metric}`;
        }

        function collectThresholdValues(intent, path) {
            const values = [];
            for (const item of ((intent && intent.rationale_items) || [])) {
                for (const evidence of ((item && item.evidence) || [])) {
                    if ((evidence && evidence.source) !== path) continue;
                    if (evidence.threshold === null || evidence.threshold === undefined) continue;
                    values.push(evidence.threshold);
                }
            }
            return values;
        }

        function resolvePath(payload, path) {
            if (!path) return undefined;
            return path.split('.').reduce((current, part) => {
                if (current === undefined || current === null) return undefined;
                if (Array.isArray(current)) {
                    const index = Number(part);
                    return Number.isInteger(index) ? current[index] : undefined;
                }
                return current[part];
            }, payload);
        }

        function updateSourceLookup(path) {
            const root = document.getElementById('source-lookup');
            if (!root) return;
            selectedSourcePath = path || '';
            const requestPre = document.getElementById('stored-request-json');
            if (requestPre) {
                requestPre.innerHTML = highlightRequestPayload(currentRequestPayload, selectedSourcePath);
            }
            if (!path) {
                root.innerHTML = '<p class="sub">Click a source path in Structured Rationale to inspect the stored request value.</p>';
                return;
            }
            const resolved = resolvePath(currentRequestPayload, path);
            const previousPath = getPreviousValuePath(path);
            const previousResolved = previousPath ? resolvePath(currentRequestPayload, previousPath) : undefined;
            const auditResolved = resolveAuditMarketPath(currentAuditDetail, path);
            const auditPreviousResolved = previousPath ? resolveAuditMarketPath(currentAuditDetail, previousPath) : undefined;
            const originalObserved = collectObservedValues((currentAuditDetail && currentAuditDetail.intent) || {}, path);
            const originalThresholds = collectThresholdValues((currentAuditDetail && currentAuditDetail.intent) || {}, path);
            const replayObserved = collectLatestReplayObservedValues(currentAuditDetail, path);
            const latestReplay = (((currentAuditDetail && currentAuditDetail.replay_runs) || [])[0] || {});
            const replayPayload = parseJson(latestReplay.payload, {});
            const replayThresholds = collectThresholdValues((replayPayload && replayPayload.replayed_intent) || {}, path);
            root.innerHTML = `
                <div class="memory-item">
                    <strong>${escapeHtml(path)}</strong>
                    <div class="memory-meta">resolved from stored DecisionRequest</div>
                    <pre>${escapeHtml(pretty(resolved === undefined ? '(missing)' : resolved))}</pre>
                    <div class="memory-meta">previous value from stored DecisionRequest</div>
                    <pre>${escapeHtml(pretty(previousResolved === undefined ? '(missing)' : previousResolved))}</pre>
                    <div class="memory-meta">resolved from stored audit market payload</div>
                    <pre>${escapeHtml(pretty(auditResolved === undefined ? '(missing)' : auditResolved))}</pre>
                    <div class="memory-meta">previous value from stored audit market payload</div>
                    <pre>${escapeHtml(pretty(auditPreviousResolved === undefined ? '(missing)' : auditPreviousResolved))}</pre>
                    <div class="memory-meta">original intent evidence observed_value</div>
                    <pre>${escapeHtml(pretty(originalObserved.length ? originalObserved : '(missing)'))}</pre>
                    <div class="memory-meta">original intent evidence threshold</div>
                    <pre>${escapeHtml(pretty(originalThresholds.length ? originalThresholds : '(missing)'))}</pre>
                    <div class="memory-meta">latest replay evidence observed_value</div>
                    <pre>${escapeHtml(pretty(replayObserved.length ? replayObserved : '(missing)'))}</pre>
                    <div class="memory-meta">latest replay evidence threshold</div>
                    <pre>${escapeHtml(pretty(replayThresholds.length ? replayThresholds : '(missing)'))}</pre>
                </div>
            `;
        }

        function resolveAuditMarketPath(detail, path) {
            if (!detail || !path) return undefined;
            const marketState = parseJson(detail.market_state, {});
            if (path.startsWith('market_snapshots.')) {
                return resolvePath(marketState, path);
            }
            if (path.startsWith('market_snapshot.')) {
                const marketSnapshot = (marketState.market_snapshots && Object.values(marketState.market_snapshots)[0]) || marketState.market_snapshot;
                const suffix = path.slice('market_snapshot.'.length);
                return resolvePath(marketSnapshot || {}, suffix);
            }
            return undefined;
        }

        function collectObservedValues(intent, path) {
            const values = [];
            for (const item of (intent && intent.rationale_items) || []) {
                for (const evidence of (item.evidence || [])) {
                    if ((evidence.source || '') === path) {
                        values.push({
                            signal: evidence.signal || 'signal',
                            observed_value: evidence.observed_value,
                            threshold: evidence.threshold,
                            operator: evidence.operator,
                        });
                    }
                }
            }
            return values;
        }

        function collectLatestReplayObservedValues(detail, path) {
            if (!detail) return [];
            const latestRun = (detail.replay_runs || [])[0];
            if (!latestRun) return [];
            const payload = parseJson(latestRun.payload, {});
            return collectObservedValues(payload.replayed_intent || {}, path);
        }

        function highlightRequestPayload(payload, path) {
            const raw = pretty(payload);
            const escapedRaw = escapeHtml(raw);
            if (!path) {
                return escapedRaw;
            }
            const resolved = resolvePath(payload, path);
            if (resolved === undefined) {
                return escapedRaw;
            }
            const snippet = escapeHtml(pretty(resolved));
            const highlighted = `<mark class="path-highlight">${snippet}</mark>`;
            const index = escapedRaw.indexOf(snippet);
            if (index < 0) {
                return escapedRaw;
            }
            return escapedRaw.slice(0, index) + highlighted + escapedRaw.slice(index + snippet.length);
        }

        function bindSourceLinks() {
            document.querySelectorAll('.source-link').forEach(button => {
                button.addEventListener('click', () => {
                    updateSourceLookup(button.dataset.source || '');
                });
            });
        }

        function renderDetail(detail) {
            currentAuditDetail = detail;
            const executionSummary = parseJson(detail.execution_summary, {});
            const requestPayload = parseJson(detail.decision_request, {});
            currentRequestPayload = requestPayload;
            const fills = detail.fills || [];
            const replayRuns = detail.replay_runs || [];
            const ledgerEntries = detail.ledger_entries || [];
            const riskProfile = parseJson(detail.risk_profile, {});
            const orderStateTransitions = detail.order_state_transitions || [];
            const positionStateTransitions = detail.position_state_transitions || [];
            const storedIntent = detail.intent || {};
            document.getElementById('audit-detail').innerHTML = `
                <div class="detail-grid">
                    <div class="detail-box">
                        <p class="eyebrow">Candidate Input Hash</p>
                        <pre>${detail.decision_input_hash || '(empty)'}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Risk Mode / Profile</p>
                        <pre>${pretty({ risk_mode: detail.risk_mode, risk_profile: riskProfile })}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Stored Request</p>
                        <pre id="stored-request-json">${highlightRequestPayload(requestPayload, selectedSourcePath)}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Structured Rationale</p>
                        ${renderRationaleItems(storedIntent)}
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Evidence Quality</p>
                        ${renderEvidenceQuality(executionSummary)}
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Execution Summary</p>
                        <pre>${pretty(executionSummary)}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">AI Raw Response</p>
                        <pre>${detail.ai_raw_response || '(empty)'}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Stored Intent</p>
                        <pre>${pretty(storedIntent)}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Source Lookup</p>
                        <div id="source-lookup">
                            <p class="sub">Click a source path in Structured Rationale to inspect the stored request value.</p>
                        </div>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Replay Source Diff</p>
                        ${renderReplaySourceDiffs(detail)}
                    </div>
                    <div class="detail-box" style="grid-column: 1 / -1;">
                        <p class="eyebrow">Ledger Entries</p>
                        <pre>${pretty(ledgerEntries)}</pre>
                    </div>
                    <div class="detail-box" style="grid-column: 1 / -1;">
                        <p class="eyebrow">Synced Fills</p>
                        <pre>${pretty(fills)}</pre>
                    </div>
                    <div class="detail-box" style="grid-column: 1 / -1;">
                        <p class="eyebrow">Replay Runs</p>
                        <pre>${pretty(replayRuns)}</pre>
                    </div>
                    <div class="detail-box" style="grid-column: 1 / -1;">
                        <p class="eyebrow">Order State Transitions</p>
                        <pre>${pretty(orderStateTransitions)}</pre>
                    </div>
                    <div class="detail-box" style="grid-column: 1 / -1;">
                        <p class="eyebrow">Position State Transitions</p>
                        <pre>${pretty(positionStateTransitions)}</pre>
                    </div>
                </div>
            `;
            bindSourceLinks();
            updateSourceLookup('');
        }

        function loadAuditDetail(auditId) {
            selectedAuditId = auditId;
            document.getElementById('run-replay-button').disabled = false;
            fetch(`/api/audits/${auditId}`)
                .then(res => res.json())
                .then(renderDetail);
        }

        function renderList(elementId, emptyText, items, renderItem) {
            const root = document.getElementById(elementId);
            if (!items.length) {
                root.innerHTML = `<p class="sub">${emptyText}</p>`;
                return;
            }
            root.innerHTML = items.map(renderItem).join('');
        }

        function runReplay() {
            if (!selectedAuditId) return;
            const status = document.getElementById('replay-status');
            status.innerText = `Running replay for audit ${selectedAuditId}...`;
            fetch(`/api/audits/${selectedAuditId}/replay`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ save: true })
            })
                .then(async res => {
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.error || 'Replay failed');
                    status.innerText = `Replay saved. Decision changed: ${data.comparison.decision_changed ? 'yes' : 'no'}`;
                    loadAuditDetail(selectedAuditId);
                    return fetch('/api/stats');
                })
                .then(res => res.json())
                .then(data => {
                    renderList('replay-list', 'No replay runs saved yet.', data.recent_replays, run => `
                        <div class="fill-item">
                            <strong>audit ${run.audit_log_id}: ${run.original_decision || 'UNKNOWN'} -> ${run.replayed_decision || 'UNKNOWN'}</strong>
                            <div class="fill-meta">
                                changed ${run.decision_changed ? 'yes' : 'no'} • side ${run.side_changed ? 'changed' : 'same'} • size ${run.size_delta || 0} • confidence ${run.confidence_delta || 0}
                            </div>
                        </div>
                    `);
                })
                .catch(err => {
                    status.innerText = err.message;
                });
        }

        document.getElementById('run-replay-button').addEventListener('click', runReplay);

        fetch('/api/stats')
            .then(res => res.json())
            .then(data => {
                document.getElementById('val-audits').innerText = data.total_audits_24h;
                document.getElementById('val-errors').innerText = data.loop_errors_24h || 0;
                document.getElementById('val-timeouts').innerText = data.timeouts_unresolved_24h || 0;
                const semanticSummary = data.semantic_validation_summary || {};
                const semantic24h = semanticSummary.last_24h || {};
                const semantic7d = semanticSummary.last_7d || {};
                const semanticLatest50 = semanticSummary.latest_50 || {};
                document.getElementById('val-semantic-fallbacks').innerText =
                    `${semantic24h.failure_count || 0} / ${semantic7d.failure_count || 0} / ${semanticLatest50.failure_count || 0}`;
                document.getElementById('val-fees').innerText = Number(data.fee_cost_24h || 0).toFixed(4);
                document.getElementById('val-ledger-pnl').innerText = Number(data.ledger_net_pnl_24h || 0).toFixed(4);
                const rejectRate = data.total_audits_24h > 0
                    ? ((data.rejected_24h / data.total_audits_24h) * 100).toFixed(1) + '%'
                    : '0%';
                document.getElementById('val-rejections').innerText = rejectRate;
                const topSemanticIssue = ((semantic24h.top_issues || [])[0] || {});
                document.getElementById('val-semantic-top-issue').innerText =
                    `Issues 24h/7d/50: ${topSemanticIssue.issue || '-'}${topSemanticIssue.count ? ` (${topSemanticIssue.count})` : ''}`;
                const topSemanticModels = (semantic24h.top_models || [])
                    .map(item => {
                        const rate24 = Number(item.failure_rate || 0) * 100;
                        const model7d = (semantic7d.top_models || []).find(model => model.model_name === item.model_name) || {};
                        const model50 = (semanticLatest50.top_models || []).find(model => model.model_name === item.model_name) || {};
                        const rate7 = Number(model7d.failure_rate || 0) * 100;
                        const rate50 = Number(model50.failure_rate || 0) * 100;
                        return `${item.model_name} (24h ${rate24.toFixed(1)}%, 7d ${rate7.toFixed(1)}%, 50 ${rate50.toFixed(1)}%)`;
                    })
                    .join(' | ');
                document.getElementById('val-semantic-models').innerText =
                    `Models: ${topSemanticModels || '-'}`;
                const headerDescription = document.querySelector('.header p');
                if (headerDescription) {
                    headerDescription.innerText =
                        `Operational health, replay controls, cost drag, symbol reports, and full audit drill-down for execution review. Models: ${renderModelRouting(data.models)}`;
                }

                const tbody = document.getElementById('audit-table-body');
                tbody.innerHTML = '';
                data.recent_logs.forEach(log => {
                    const intent = parseJson(log.intent);
                    const decision = intent.decision || 'UNKNOWN';
                    const approved = !!log.risk_approved;
                    const badgeClass = approved ? 'ok' : 'bad';
                    const badgeText = approved ? 'Approved' : 'Rejected';
                    tbody.innerHTML += `
                        <tr class="audit-row" data-audit-id="${log.id}">
                            <td>${log.timestamp}</td>
                            <td><strong>${decision}</strong></td>
                            <td><span class="badge ${badgeClass}">${badgeText}</span></td>
                            <td>${log.risk_reason || '-'}</td>
                        </tr>
                    `;
                });
                if (!data.recent_logs.length) {
                    tbody.innerHTML = '<tr><td colspan="4">No audit logs yet.</td></tr>';
                } else {
                    document.querySelectorAll('.audit-row').forEach(row => {
                        row.addEventListener('click', () => loadAuditDetail(row.dataset.auditId));
                    });
                    loadAuditDetail(data.recent_logs[0].id);
                }

                renderList('memory-list', 'No memory rules saved yet.', data.recent_memory, item => {
                    const metrics = item.metrics || {};
                    const badgeClass = metrics.status === 'rule' ? 'ok' : 'warn';
                    const applyWhen = item.apply_when || {};
                    const ignoreWhen = item.ignore_when || [];
                    return `
                        <div class="memory-item">
                            <span class="badge ${badgeClass}">${metrics.status || 'unknown'}</span>
                            <strong>${item.rule}</strong>
                            <div class="memory-meta">
                                support ${metrics.support || 0} • regime ${metrics.regime || 'general'} • apply ${((applyWhen.trend || []).join('/') || 'n/a')} • ignore ${ignoreWhen[0] || 'n/a'}
                                <br>candidates ${summarizeCandidateLessons(metrics)}
                            </div>
                        </div>
                    `;
                });

                renderList('fill-list', 'No synced fills yet.', data.recent_fills, fill => `
                    <div class="fill-item">
                        <strong>${fill.symbol || 'UNKNOWN'} ${fill.side || ''} ${fill.amount || 0}</strong>
                        <div class="fill-meta">
                            price ${fill.price || 0} • fee ${fill.fee_cost || 0} ${fill.fee_currency || ''} • pnl ${fill.realized_pnl || 0}
                        </div>
                    </div>
                `);

                renderList('ledger-list', 'No ledger rows yet.', data.recent_ledger, row => `
                    <div class="fill-item">
                        <strong>${row.symbol || 'UNKNOWN'} ${row.decision || ''}</strong>
                        <div class="fill-meta">
                            net pnl ${row.net_realized_pnl || 0} • fee ${row.fee_cost || 0} • fills ${row.fill_count || 0}
                        </div>
                    </div>
                `);

                renderList('symbol-journal-list', 'No symbol journal rows yet.', data.recent_symbol_journal || [], row => `
                    <div class="fill-item">
                        <strong>${row.symbol || 'UNKNOWN'} ${row.decision || 'UNKNOWN'} • audit ${row.audit_log_id || '-'}</strong>
                        <div class="fill-meta">
                            mode ${row.risk_mode || 'n/a'} • triggered ${row.triggered ? 'yes' : 'no'} • risk ${row.risk_triggered ? 'yes' : 'no'}
                            <br>est pnl ${row.estimated_pnl_pct ?? 'n/a'} • net pnl ${row.net_realized_pnl ?? 'n/a'} • signals ${(row.evidence_signals || []).slice(0, 3).join(', ') || 'none'}
                        </div>
                    </div>
                `);

                renderList('replay-list', 'No replay runs saved yet.', data.recent_replays, run => `
                    <div class="fill-item">
                        <strong>audit ${run.audit_log_id}: ${run.original_decision || 'UNKNOWN'} -> ${run.replayed_decision || 'UNKNOWN'}</strong>
                        <div class="fill-meta">
                            changed ${run.decision_changed ? 'yes' : 'no'} • side ${run.side_changed ? 'changed' : 'same'} • size ${run.size_delta || 0} • confidence ${run.confidence_delta || 0}
                            <br>${summarizeEvidenceDiff(run)}
                        </div>
                    </div>
                `);

                renderList('symbol-report-list', 'No symbol performance rows yet.', data.symbol_report || [], row => `
                    <div class="fill-item">
                        <strong>${row.symbol}: net ${row.net_realized_pnl}</strong>
                        <div class="fill-meta">
                            gross ${row.gross_realized_pnl} • fee ${row.fee_cost} • fills ${row.fill_count} • win rate ${row.win_rate}%
                        </div>
                    </div>
                `);

                renderList('daily-report-list', 'No daily ledger rows yet.', data.daily_report || [], row => `
                    <div class="fill-item">
                        <strong>${row.day}: net ${row.net_realized_pnl}</strong>
                        <div class="fill-meta">
                            gross ${row.gross_realized_pnl} • fee ${row.fee_cost} • rows ${row.row_count}
                        </div>
                    </div>
                `);
            });
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/stats")
def api_stats():
    try:
        snapshot = state_store.get_monitoring_snapshot()
        snapshot["symbol_report"] = state_store.get_symbol_performance_report(hours=24)
        snapshot["daily_report"] = state_store.get_daily_ledger_report(days=7)
        default_model = (os.getenv("GEMINI_DEFAULT_MODEL", "").strip() or os.getenv("GEMINI_MODEL", "").strip())
        trigger_model = (os.getenv("GEMINI_TRIGGER_MODEL", "").strip() or default_model)
        memory_model = (
            os.getenv("GEMINI_MEMORY_MODEL", "").strip()
            or trigger_model
            or default_model
            or "gemini-3.0-pro"
        )
        snapshot["models"] = {
            "default_model": default_model,
            "trigger_model": trigger_model,
            "memory_model": memory_model,
        }
        return jsonify(snapshot)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/audits/<int:audit_id>")
def api_audit_detail(audit_id: int):
    try:
        detail = state_store.get_audit_log(audit_id)
        if detail is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(detail)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/audits/<int:audit_id>/replay", methods=["POST"])
def api_audit_replay(audit_id: int):
    try:
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not gemini_key:
            return jsonify({"error": "GEMINI_API_KEY is required"}), 400
        payload = request.get_json(silent=True) or {}
        result = replay_and_optionally_save(
            state_store,
            audit_id=audit_id,
            gemini_key=gemini_key,
            save=bool(payload.get("save", True)),
        )
        return jsonify(result)
    except SystemExit as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
