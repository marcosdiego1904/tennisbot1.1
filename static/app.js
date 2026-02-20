// TennisBot Dashboard — frontend logic

// --- State ---
let allResults = [];
let activeFilters = { BUY: true, WAIT: true, SKIP: false };  // WAIT kept for backwards compat
let autoRefreshInterval = null;
let countdownSeconds = 0;
let countdownTimer = null;
let trackedTickers = new Set();   // event tickers already tracked this session
let currentOutcomeBetId = null;   // bet ID being edited in the outcome modal

const AUTO_REFRESH_SECONDS = 60;


// --- Init ---

document.addEventListener("DOMContentLoaded", () => {
    const now = new Date();
    document.getElementById("currentDate").textContent =
        now.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });

    // Load pending badge and sync tracked tickers on page load
    updatePendingBadge();
    fetch("/api/bets").then(r => r.json()).then(data => {
        for (const b of data.bets || []) {
            if (b.event_ticker) trackedTickers.add(b.event_ticker);
        }
    }).catch(() => {});
});


// --- Calculator toggle ---

function toggleCalculator() {
    const panel = document.getElementById("manualPanel");
    const btn = document.getElementById("btnCalcToggle");
    const isOpen = panel.style.display !== "none";
    panel.style.display = isOpen ? "none" : "block";
    btn.classList.toggle("open", !isOpen);
}


// --- Signal filter toggles ---

function toggleFilter(signal) {
    activeFilters[signal] = !activeFilters[signal];

    // Update button appearance
    const cardMap = { BUY: ".summary-card.buy", WAIT: ".summary-card.wait", SKIP: ".summary-card.skip" };
    const card = document.querySelector(cardMap[signal]);
    if (card) card.classList.toggle("active", activeFilters[signal]);

    applyFilters();
}

function applyFilters() {
    const container = document.getElementById("matchesContainer");
    const filtered = allResults.filter(r => activeFilters[r.signal]);

    if (filtered.length === 0 && allResults.length > 0) {
        container.innerHTML = '<div class="empty-state"><p>No matches for selected filters. Click the signal counters above to toggle.</p></div>';
        return;
    }

    if (filtered.length === 0) return;

    // Group by tournament
    const groups = {};
    for (const r of filtered) {
        const key = r.tournament || "Unknown";
        if (!groups[key]) groups[key] = [];
        groups[key].push(r);
    }

    let html = "";
    for (const [tournament, matches] of Object.entries(groups)) {
        const level = matches[0].tournament_level || "";
        html += `<div class="tournament-group">`;
        html += `<div class="tournament-header"><span class="tournament-name">${tournament}</span><span class="tournament-level">${level}</span></div>`;
        html += `<div class="matches-grid">${matches.map(renderMatchCard).join("")}</div>`;
        html += `</div>`;
    }

    container.innerHTML = html;
}


// --- Auto-refresh ---

function toggleAutoRefresh() {
    const btn = document.getElementById("btnAutoRefresh");

    if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
        clearInterval(countdownTimer);
        autoRefreshInterval = null;
        countdownTimer = null;
        btn.textContent = "Auto: OFF";
        btn.classList.remove("active");
        document.getElementById("refreshTimer").textContent = "";
    } else {
        btn.textContent = "Auto: ON";
        btn.classList.add("active");
        startCountdown();
        autoRefreshInterval = setInterval(() => {
            fetchAnalysis();
            startCountdown();
        }, AUTO_REFRESH_SECONDS * 1000);
    }
}

function startCountdown() {
    countdownSeconds = AUTO_REFRESH_SECONDS;
    clearInterval(countdownTimer);
    updateCountdownDisplay();
    countdownTimer = setInterval(() => {
        countdownSeconds--;
        if (countdownSeconds <= 0) countdownSeconds = 0;
        updateCountdownDisplay();
    }, 1000);
}

function updateCountdownDisplay() {
    document.getElementById("refreshTimer").textContent = countdownSeconds > 0 ? countdownSeconds + "s" : "";
}


// --- Fetch live analysis from Kalshi ---

async function fetchAnalysis() {
    const btn = document.getElementById("btnRefresh");
    const container = document.getElementById("matchesContainer");

    btn.disabled = true;
    btn.textContent = "Loading...";
    container.innerHTML = '<div class="loading"><div class="spinner"></div><p>Fetching markets from Kalshi...</p></div>';

    try {
        const resp = await fetch("/api/analyze");
        const data = await resp.json();

        if (!resp.ok) {
            throw new Error(data.detail || "API error");
        }

        renderResults(data);

        // Update last-updated timestamp
        const now = new Date();
        document.getElementById("lastUpdated").textContent =
            "Updated " + now.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
    } catch (err) {
        container.innerHTML = `
            <div class="empty-state">
                <p>Error fetching markets: <strong>${err.message}</strong></p>
                <p>Check your API keys in .env or use the manual calculator.</p>
            </div>`;
    } finally {
        btn.disabled = false;
        btn.textContent = "Refresh Markets";
    }
}


// --- Manual analysis ---

async function analyzeManual() {
    const payload = {
        fav_name: document.getElementById("mFavName").value,
        dog_name: document.getElementById("mDogName").value,
        fav_probability: parseFloat(document.getElementById("mFavPct").value),
        kalshi_price: parseFloat(document.getElementById("mKalshiPrice").value),
        tournament_level: document.getElementById("mTournament").value,
        surface: document.getElementById("mSurface").value,
        volume: parseFloat(document.getElementById("mVolume").value) || 50000,
        tournament_name: document.getElementById("mTournament").value + " Tour",
    };

    const resultDiv = document.getElementById("manualResult");

    try {
        const resp = await fetch("/api/analyze/manual", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();

        if (!resp.ok) {
            throw new Error(data.detail || "Error");
        }

        resultDiv.innerHTML = renderMatchCard(data);
    } catch (err) {
        resultDiv.innerHTML = `<div class="manual-result" style="color: #f85149;">Error: ${err.message}</div>`;
    }
}


// --- Render results ---

function renderResults(data) {
    const container = document.getElementById("matchesContainer");
    const summaryBar = document.getElementById("summaryBar");

    // Update summary
    if (data.summary) {
        summaryBar.style.display = "flex";
        document.getElementById("sumBuy").textContent = data.summary.buy;
        document.getElementById("sumWait").textContent = data.summary.wait;
        document.getElementById("sumSkip").textContent = data.summary.skip;
        document.getElementById("sumTotal").textContent = data.summary.total;
    }

    if (!data.results || data.results.length === 0) {
        allResults = [];
        container.innerHTML = `
            <div class="empty-state">
                <p>${data.message || "No tennis markets found."}</p>
            </div>`;
        return;
    }

    // Sort: BUY first, then WAIT, then SKIP — within each, soonest match first
    const order = { BUY: 0, WAIT: 1, SKIP: 2 };
    allResults = data.results.sort((a, b) => {
        const oa = order[a.signal] ?? 3;
        const ob = order[b.signal] ?? 3;
        if (oa !== ob) return oa - ob;
        // Within same signal, sort by close_time ascending (soonest first)
        const ta = a.close_time || "9999";
        const tb = b.close_time || "9999";
        if (ta !== tb) return ta < tb ? -1 : 1;
        return 0;
    });

    applyFilters();
}


// --- Tab switching ---

function switchTab(tab) {
    document.getElementById("panelLive").style.display  = tab === "live"  ? "block" : "none";
    document.getElementById("panelBets").style.display  = tab === "bets"  ? "block" : "none";
    document.getElementById("panelStats").style.display = tab === "stats" ? "block" : "none";

    document.getElementById("tabLive").classList.toggle("active",  tab === "live");
    document.getElementById("tabBets").classList.toggle("active",  tab === "bets");
    document.getElementById("tabStats").classList.toggle("active", tab === "stats");

    if (tab === "bets")  loadMyBets();
    if (tab === "stats") loadStats();
}


// --- Track a bet (Moment 1 snapshot) ---

async function trackBet(matchData) {
    const key = matchData.ticker || matchData.fav_name;
    const btn = document.getElementById(`track-${key}`);
    if (btn) { btn.disabled = true; btn.textContent = "Saving..."; }

    try {
        const resp = await fetch("/api/bets/track", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(matchData),
        });
        const data = await resp.json();

        if (!resp.ok) throw new Error(data.detail || "Error saving bet");

        trackedTickers.add(key);
        if (btn) { btn.textContent = "Tracked ✓"; btn.classList.add("tracked"); }

        // Update pending badge
        updatePendingBadge();
    } catch (err) {
        if (btn) { btn.disabled = false; btn.textContent = "+ Track this bet"; }
        alert("Error tracking bet: " + err.message);
    }
}

async function updatePendingBadge() {
    try {
        const resp = await fetch("/api/bets?status=pending");
        const data = await resp.json();
        const badge = document.getElementById("pendingBadge");
        if (data.count > 0) {
            badge.textContent = data.count;
            badge.style.display = "inline-block";
        } else {
            badge.style.display = "none";
        }
    } catch (_) {}
}


// --- My Bets tab ---

async function loadMyBets() {
    try {
        const resp = await fetch("/api/bets");
        const data = await resp.json();

        const pending   = data.bets.filter(b => b.status === "pending");
        const completed = data.bets.filter(b => b.status === "completed");

        renderPendingBets(pending);
        renderCompletedBets(completed);

        // Sync trackedTickers with DB (so Track buttons stay correct after page refresh)
        for (const b of data.bets) {
            if (b.event_ticker) trackedTickers.add(b.event_ticker);
        }

        const badge = document.getElementById("pendingBadge");
        if (pending.length > 0) {
            badge.textContent = pending.length;
            badge.style.display = "inline-block";
        } else {
            badge.style.display = "none";
        }
    } catch (err) {
        document.getElementById("pendingBetsContainer").innerHTML =
            `<div class="empty-state"><p>Error loading bets: ${err.message}</p></div>`;
    }
}

function renderPendingBets(bets) {
    const container = document.getElementById("pendingBetsContainer");
    if (bets.length === 0) {
        container.innerHTML = '<div class="empty-state" style="padding:24px;"><p>No pending bets. Track a match from Live Markets.</p></div>';
        return;
    }

    let html = `<table class="bets-table">
        <thead>
            <tr>
                <th>Match</th>
                <th>Tournament</th>
                <th>Prob%</th>
                <th>Market</th>
                <th>Target</th>
                <th>Surface</th>
                <th>Tracked</th>
                <th></th>
            </tr>
        </thead><tbody>`;

    for (const b of bets) {
        const date = new Date(b.tracked_at).toLocaleDateString("en-US", { month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" });
        html += `<tr>
            <td><strong class="fav-name">${b.player_fav}</strong><span class="vs-text"> vs </span>${b.player_dog}</td>
            <td>${b.tournament}<br><span class="level-tag">${b.tournament_level}</span></td>
            <td>${b.fav_probability}%</td>
            <td>${b.kalshi_price}¢</td>
            <td class="target-price">${b.target_price}¢</td>
            <td>${b.surface}</td>
            <td class="date-cell">${date}</td>
            <td>
                <button class="btn btn-sm btn-primary" onclick="openOutcomeModal(${b.id}, '${b.player_fav}', '${b.player_dog}', ${b.target_price})">Update</button>
                <button class="btn btn-sm btn-danger" onclick="deleteBet(${b.id})" title="Delete">✕</button>
            </td>
        </tr>`;
    }
    html += "</tbody></table>";
    container.innerHTML = html;
}

function renderCompletedBets(bets) {
    const container = document.getElementById("completedBetsContainer");
    if (bets.length === 0) {
        container.innerHTML = '<div class="empty-state" style="padding:24px;"><p>No completed bets yet.</p></div>';
        return;
    }

    let html = `<table class="bets-table">
        <thead>
            <tr>
                <th>Match</th>
                <th>Tournament</th>
                <th>Prob%</th>
                <th>Target</th>
                <th>Lowest</th>
                <th>Filled?</th>
                <th>Outcome</th>
                <th>Edge</th>
                <th>PNL</th>
            </tr>
        </thead><tbody>`;

    for (const b of bets) {
        const filled = b.order_filled ? '<span class="badge-yes">YES</span>' : '<span class="badge-no">NO</span>';
        const outcome = b.match_outcome === "fav_won"
            ? '<span class="badge-yes">Fav Won ✓</span>'
            : '<span class="badge-no">Fav Lost ✗</span>';
        const edgeColor = b.edge < 0 ? "color:#3fb950" : "color:#f85149";
        const pnlColor = b.pnl >= 0 ? "color:#3fb950" : "color:#f85149";
        const pnlSign = b.pnl > 0 ? "+" : "";

        html += `<tr>
            <td><strong class="fav-name">${b.player_fav}</strong><span class="vs-text"> vs </span>${b.player_dog}</td>
            <td>${b.tournament}<br><span class="level-tag">${b.tournament_level}</span></td>
            <td>${b.fav_probability}%</td>
            <td>${b.target_price}¢</td>
            <td>${b.lowest_price_reached ?? "—"}¢</td>
            <td>${filled}</td>
            <td>${outcome}</td>
            <td style="${edgeColor}">${b.edge > 0 ? "+" : ""}${b.edge}¢</td>
            <td style="${pnlColor};font-weight:700;">${pnlSign}$${b.pnl?.toFixed(2) ?? "—"}</td>
        </tr>`;
    }
    html += "</tbody></table>";
    container.innerHTML = html;
}

async function deleteBet(betId) {
    if (!confirm("Delete this tracked bet?")) return;
    try {
        await fetch(`/api/bets/${betId}`, { method: "DELETE" });
        loadMyBets();
    } catch (err) {
        alert("Error deleting bet: " + err.message);
    }
}


// --- Outcome modal ---

function openOutcomeModal(betId, favName, dogName, targetPrice) {
    currentOutcomeBetId = betId;
    document.getElementById("modalTitle").textContent = "Update Outcome";
    document.getElementById("modalSubtitle").textContent = `${favName} vs ${dogName} — Target: ${targetPrice}¢`;
    document.getElementById("oContracts").value = "";
    document.getElementById("oLowestPrice").value = "";
    document.getElementById("oMatchOutcome").value = "";
    document.getElementById("outcomePreview").style.display = "none";
    document.getElementById("modalError").style.display = "none";

    // Store target for live preview
    document.getElementById("oLowestPrice").dataset.target = targetPrice;
    document.getElementById("oContracts").dataset.target = targetPrice;

    document.getElementById("outcomeModal").style.display = "flex";

    // Live preview on input change
    document.getElementById("oLowestPrice").oninput = previewOutcome;
    document.getElementById("oContracts").oninput = previewOutcome;
    document.getElementById("oMatchOutcome").onchange = previewOutcome;
}

function closeOutcomeModal(event) {
    if (event && event.target !== document.getElementById("outcomeModal")) return;
    document.getElementById("outcomeModal").style.display = "none";
    currentOutcomeBetId = null;
}

function previewOutcome() {
    const target   = parseInt(document.getElementById("oLowestPrice").dataset.target);
    const lowest   = parseInt(document.getElementById("oLowestPrice").value);
    const contracts = parseInt(document.getElementById("oContracts").value) || 0;
    const outcome  = document.getElementById("oMatchOutcome").value;

    if (!lowest || !outcome) {
        document.getElementById("outcomePreview").style.display = "none";
        return;
    }

    const filled = lowest <= target;
    const edge   = lowest - target;
    let pnl = 0;
    if (filled && contracts > 0) {
        pnl = outcome === "fav_won"
            ? ((100 - target) * contracts / 100)
            : -(target * contracts / 100);
    }

    document.getElementById("prevFilled").textContent = filled ? "YES ✓" : "NO ✗";
    document.getElementById("prevFilled").style.color = filled ? "#3fb950" : "#f85149";
    document.getElementById("prevEdge").textContent = (edge > 0 ? "+" : "") + edge + "¢";
    document.getElementById("prevEdge").style.color = edge < 0 ? "#3fb950" : "#f85149";
    document.getElementById("prevPnl").textContent = (pnl >= 0 ? "+" : "") + "$" + pnl.toFixed(2);
    document.getElementById("prevPnl").style.color = pnl >= 0 ? "#3fb950" : "#f85149";
    document.getElementById("outcomePreview").style.display = "block";
}

async function submitOutcome() {
    const lowest   = parseInt(document.getElementById("oLowestPrice").value);
    const outcome  = document.getElementById("oMatchOutcome").value;
    const contracts = parseInt(document.getElementById("oContracts").value) || 0;
    const errDiv   = document.getElementById("modalError");
    const btn      = document.getElementById("btnSubmitOutcome");

    errDiv.style.display = "none";

    if (!lowest || lowest < 1 || lowest > 99) {
        errDiv.textContent = "Enter a valid lowest price (1–99)";
        errDiv.style.display = "block";
        return;
    }
    if (!outcome) {
        errDiv.textContent = "Select a match outcome";
        errDiv.style.display = "block";
        return;
    }

    btn.disabled = true;
    btn.textContent = "Saving...";

    try {
        const resp = await fetch(`/api/bets/${currentOutcomeBetId}/outcome`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ lowest_price_reached: lowest, match_outcome: outcome, contracts }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "Error");

        document.getElementById("outcomeModal").style.display = "none";
        currentOutcomeBetId = null;
        loadMyBets();
    } catch (err) {
        errDiv.textContent = "Error: " + err.message;
        errDiv.style.display = "block";
    } finally {
        btn.disabled = false;
        btn.textContent = "Save";
    }
}


// --- Stats tab ---

async function loadStats() {
    const container = document.getElementById("statsContainer");
    container.innerHTML = '<div class="loading"><div class="spinner"></div><p>Loading stats...</p></div>';

    try {
        const resp = await fetch("/api/bets/stats");
        const data = await resp.json();
        renderStats(data.stats);
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>Error loading stats: ${err.message}</p></div>`;
    }
}

function renderStats(s) {
    const container = document.getElementById("statsContainer");

    if (!s.completed || s.completed === 0) {
        container.innerHTML = '<div class="empty-state" style="padding:48px;"><p>No completed bets yet. Complete at least one bet to see stats.</p></div>';
        return;
    }

    const pnlColor = s.total_pnl >= 0 ? "#3fb950" : "#f85149";
    const pnlSign  = s.total_pnl > 0 ? "+" : "";

    let html = `
    <div class="stats-summary">
        <div class="stat-card">
            <div class="stat-label">Total Tracked</div>
            <div class="stat-value">${s.total_tracked}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Completed</div>
            <div class="stat-value">${s.completed}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Fill Rate</div>
            <div class="stat-value" style="color:#58a6ff;">${s.fill_rate_pct}%</div>
            <div class="stat-sub">${s.filled} of ${s.completed} filled</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Win Rate</div>
            <div class="stat-value" style="color:${s.win_rate_pct >= 70 ? '#3fb950' : '#d29922'};">${s.win_rate_pct}%</div>
            <div class="stat-sub">${s.won} of ${s.filled} filled bets</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Total P&L</div>
            <div class="stat-value" style="color:${pnlColor};">${pnlSign}$${s.total_pnl?.toFixed(2)}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Avg Edge</div>
            <div class="stat-value" style="color:${(s.avg_edge_cents ?? 0) <= 0 ? '#3fb950' : '#f85149'};">${s.avg_edge_cents !== null ? (s.avg_edge_cents > 0 ? "+" : "") + s.avg_edge_cents + "¢" : "—"}</div>
            <div class="stat-sub">target - lowest</div>
        </div>
    </div>`;

    // By probability bucket
    if (s.by_prob_bucket && s.by_prob_bucket.length > 0) {
        html += `<div class="stats-section-title">By Probability Range</div>`;
        html += renderStatsTable(s.by_prob_bucket, "bucket");
    }

    // By tournament level
    if (s.by_level && s.by_level.length > 0) {
        html += `<div class="stats-section-title">By Tournament Level</div>`;
        html += renderStatsTable(s.by_level, "label");
    }

    // By surface
    if (s.by_surface && s.by_surface.length > 0) {
        html += `<div class="stats-section-title">By Surface</div>`;
        html += renderStatsTable(s.by_surface, "label");
    }

    container.innerHTML = html;
}

function renderStatsTable(rows, labelField) {
    let html = `<table class="bets-table stats-table">
        <thead><tr>
            <th>${labelField === "bucket" ? "Range" : "Category"}</th>
            <th>Bets</th>
            <th>Filled</th>
            <th>Fill Rate</th>
            <th>Won</th>
            <th>Win Rate</th>
            <th>P&L</th>
        </tr></thead><tbody>`;

    for (const r of rows) {
        const pnlColor = r.pnl >= 0 ? "#3fb950" : "#f85149";
        const pnlSign  = r.pnl > 0 ? "+" : "";
        html += `<tr>
            <td><strong>${r[labelField]}</strong></td>
            <td>${r.count}</td>
            <td>${r.filled}</td>
            <td style="color:#58a6ff;">${r.fill_rate_pct}%</td>
            <td>${r.won}</td>
            <td style="color:${r.win_rate_pct >= 70 ? '#3fb950' : '#d29922'};">${r.win_rate_pct}%</td>
            <td style="color:${pnlColor};font-weight:700;">${pnlSign}$${r.pnl.toFixed(2)}</td>
        </tr>`;
    }

    html += "</tbody></table>";
    return html;
}


// --- Debug: show raw Kalshi data on the dashboard ---

async function fetchDebug() {
    const container = document.getElementById("debugContainer");
    container.innerHTML = '<div class="loading"><div class="spinner"></div><p>Fetching raw Kalshi data...</p></div>';

    try {
        const resp = await fetch("/api/debug/kalshi");
        const data = await resp.json();

        let html = '<div class="manual-panel" style="margin-top: 12px; border-radius: 8px;">';
        html += '<h3>Kalshi Debug — Series Discovery</h3>';

        // Discovery results
        if (data.discovery) {
            const d = data.discovery;

            // All sports list
            html += '<h4 style="color: #d29922; margin: 12px 0 8px;">1. ALL Sports on Kalshi</h4>';
            if (d.all_sports && d.all_sports.length > 0) {
                html += `<div style="background:#0d1117; padding:8px; border-radius:4px; font-size:12px; word-break:break-all;">`;
                html += d.all_sports.map(s => {
                    const isTennis = s.toLowerCase().includes('tennis');
                    return `<span style="color:${isTennis ? '#3fb950; font-weight:bold' : '#8b949e'}; margin-right:8px;">${s}</span>`;
                }).join(' · ');
                html += '</div>';
            }

            // Tennis filters
            html += '<h4 style="color: #d29922; margin: 8px 0 8px;">Tennis Filters Detail</h4>';
            if (d.filters_by_sport) {
                html += `<div style="background:#0d1117; padding:8px; border-radius:4px; font-size:11px; max-height:250px; overflow-y:auto; word-break:break-all;">`;
                html += JSON.stringify(d.filters_by_sport, null, 2).replace(/\n/g, '<br>').replace(/ /g, '&nbsp;');
                html += '</div>';
            }
            if (d.filters_by_sport_error) {
                html += `<p style="color:#f85149;">${d.filters_by_sport_error}</p>`;
            }

            // tags_by_categories
            html += '<h4 style="color: #d29922; margin: 12px 0 8px;">2. /search/tags_by_categories (Tennis tags)</h4>';
            if (d.tags_by_categories) {
                html += `<div style="background:#0d1117; padding:8px; border-radius:4px; font-size:11px; max-height:200px; overflow-y:auto; word-break:break-all;">`;
                html += JSON.stringify(d.tags_by_categories, null, 2).replace(/\n/g, '<br>').replace(/ /g, '&nbsp;');
                html += '</div>';
            }
            if (d.tags_by_categories_error) {
                html += `<p style="color:#f85149;">${d.tags_by_categories_error}</p>`;
            }

            // Discovered series tickers
            html += '<h4 style="color: #3fb950; margin: 12px 0 8px;">3. Discovered Tennis Series Tickers</h4>';
            if (d.discovered_series && d.discovered_series.length > 0) {
                for (const ticker of d.discovered_series) {
                    html += `<div style="background:#0d1117; padding:6px 8px; margin:4px 0; border-radius:4px; font-size:13px;">`;
                    html += `<strong style="color:#3fb950;">${ticker}</strong>`;
                    html += '</div>';
                }
            } else {
                html += '<p style="color:#f85149;">No series discovered — using fallback tickers</p>';
            }
        }

        // Series market counts
        if (data.series_tried) {
            html += '<h4 style="color: #58a6ff; margin: 12px 0 8px;">4. Markets Per Series</h4>';
            for (const s of data.series_tried) {
                const color = s.error ? '#f85149' : (s.count > 0 ? '#3fb950' : '#8b949e');
                const status = s.error ? `ERROR: ${s.error}` : `${s.count} markets (${s.with_prices || 0} with prices)`;
                html += `<p style="margin:4px 0;"><strong>${s.series}</strong>: <span style="color:${color};">${status}</span></p>`;
            }
        }

        // Total
        html += `<p style="margin:12px 0;"><strong>Total raw markets:</strong> ${data.raw_markets_found || 0}</p>`;

        // Time fields diagnostic
        if (data.time_fields) {
            html += '<h4 style="color: #d29922; margin: 12px 0 8px;">Time Fields (first market)</h4>';
            html += '<div style="background:#0d1117; padding:8px; border-radius:4px; font-size:12px;">';
            for (const [k, v] of Object.entries(data.time_fields)) {
                html += `<p style="margin:2px 0;"><strong>${k}:</strong> <span style="color:#79c0ff;">${v}</span></p>`;
            }
            html += '</div>';
        }

        // Parse results
        html += `<h4 style="color: #58a6ff; margin: 12px 0 8px;">5. Parsing Results</h4>`;
        html += `<p>OK: <strong style="color:#3fb950;">${data.parsed_ok || 0}</strong> | Failed: <strong style="color:#f85149;">${(data.parse_failures || []).length}</strong></p>`;

        if (data.parsed_matches && data.parsed_matches.length > 0) {
            for (const m of data.parsed_matches) {
                html += `<div style="background:#0d1117; padding:8px; margin:4px 0; border-radius:4px; font-size:12px;">`;
                html += `<strong style="color:#58a6ff;">${m.fav}</strong> vs ${m.dog} `;
                html += `| ${m.fav_pct}% | ${m.tournament} (${m.level})`;
                html += '</div>';
            }
        }

        if (data.parse_failures && data.parse_failures.length > 0) {
            html += '<details style="margin-top:8px;"><summary style="color:#f85149; cursor:pointer;">Parse Failures</summary>';
            for (const f of data.parse_failures) {
                html += `<div style="background:#0d1117; padding:6px; margin:4px 0; border-radius:4px; font-size:11px; color:#f85149;">`;
                html += `${f.ticker}: ${f.reason}`;
                html += '</div>';
            }
            html += '</details>';
        }

        html += '</div>';
        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = `<div style="color:#f85149; padding:12px;">Debug error: ${err.message}</div>`;
    }
}


// --- Render a single match card ---

function renderMatchCard(r) {
    const signal = r.signal;
    const isSkip = signal === "SKIP";

    // Price section
    let pricesHTML = "";
    if (!isSkip) {
        pricesHTML = `
            <div class="match-prices">
                <div class="price-block kalshi">
                    <span class="label">Market</span>
                    <span class="price">${r.kalshi_price}¢</span>
                </div>
                <div class="price-block target">
                    <span class="label">Limit Order</span>
                    <span class="price">${r.target_price}¢</span>
                </div>
                <div class="price-block edge">
                    <span class="label">Spread</span>
                    <span class="price">${r.edge.toFixed(1)}¢</span>
                </div>
            </div>`;
    }

    // Time display
    let timeHTML = "";
    if (r.close_time) {
        const ct = new Date(r.close_time);
        const now = new Date();
        const diffMs = ct - now;
        const diffH = Math.floor(diffMs / 3600000);
        const diffM = Math.floor((diffMs % 3600000) / 60000);

        let timeLabel;
        if (diffMs < 0) {
            timeLabel = "Live";
        } else if (diffH < 1) {
            timeLabel = `${diffM}m`;
        } else if (diffH < 24) {
            timeLabel = `${diffH}h ${diffM}m`;
        } else {
            const days = Math.floor(diffH / 24);
            timeLabel = `${days}d ${diffH % 24}h`;
        }
        timeHTML = `<span class="match-time">${timeLabel}</span>`;
    }

    // Meta tags
    const tags = [];
    if (r.surface) tags.push(r.surface);
    if (r.tournament_level) tags.push(r.tournament_level);
    if (r.factor) tags.push(`Factor: ${r.factor}`);

    const tagsHTML = tags.map(t => `<span class="tag">${t}</span>`).join("");

    // Detail line
    let detailHTML = "";
    if (isSkip) {
        detailHTML = `<div class="match-detail">${r.skip_reason || ""}</div>`;
    } else {
        detailHTML = `<div class="match-detail">Fav: ${r.fav_probability}% | ${r.tournament || ""}</div>`;
    }

    // Track button — available on all signals
    let trackHTML = "";
    if (r.kalshi_price != null) {
        const alreadyTracked = trackedTickers.has(r.ticker || r.fav_name);
        trackHTML = `
            <div class="card-track">
                <button
                    class="btn-track ${alreadyTracked ? 'tracked' : ''}"
                    id="track-${r.ticker || r.fav_name}"
                    onclick="trackBet(${JSON.stringify(r).replace(/"/g, '&quot;')})"
                    ${alreadyTracked ? 'disabled' : ''}
                >
                    ${alreadyTracked ? 'Tracked ✓' : '+ Track this bet'}
                </button>
            </div>`;
    }

    return `
        <div class="match-card signal-${signal}">
            <div class="card-top">
                <div class="signal-badge ${signal}">${signal}</div>
                <div class="match-players">
                    <span class="fav">${r.fav_name}</span> vs ${r.dog_name}
                </div>
                ${timeHTML}
            </div>
            <div class="match-meta">${tagsHTML}</div>
            ${detailHTML}
            ${pricesHTML}
            ${trackHTML}
        </div>`;
}
