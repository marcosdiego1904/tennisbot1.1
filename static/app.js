// TennisBot Dashboard — frontend logic

// --- State ---
let allResults = [];
let activeFilters = { BUY: true, WAIT: true, SKIP: false };  // WAIT kept for backwards compat
let autoRefreshInterval = null;
let countdownSeconds = 0;
let countdownTimer = null;

const AUTO_REFRESH_SECONDS = 60;


// --- Init ---

document.addEventListener("DOMContentLoaded", () => {
    const now = new Date();
    document.getElementById("currentDate").textContent =
        now.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
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
        fav_ranking: parseInt(document.getElementById("mFavRank").value) || null,
        dog_ranking: parseInt(document.getElementById("mDogRank").value) || null,
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

    // Sort: BUY first, then WAIT sorted by edge desc, then SKIP
    const order = { BUY: 0, WAIT: 1, SKIP: 2 };
    allResults = data.results.sort((a, b) => {
        const oa = order[a.signal] ?? 3;
        const ob = order[b.signal] ?? 3;
        if (oa !== ob) return oa - ob;
        // Within same signal, sort by edge descending (best opportunity first)
        return (b.edge || -999) - (a.edge || -999);
    });

    applyFilters();
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

    // Meta tags
    const tags = [];
    if (r.surface) tags.push(r.surface);
    if (r.tournament_level) tags.push(r.tournament_level);
    if (r.ranking_gap !== null && r.ranking_gap !== undefined) tags.push(`Gap: ${r.ranking_gap}`);
    if (r.factor) tags.push(`Factor: ${r.factor}`);

    const tagsHTML = tags.map(t => `<span class="tag">${t}</span>`).join("");

    // Detail line
    let detailHTML = "";
    if (isSkip) {
        detailHTML = `<div class="match-detail">${r.skip_reason || ""}</div>`;
    } else {
        detailHTML = `<div class="match-detail">Fav: ${r.fav_probability}% | ${r.tournament || ""}</div>`;
    }

    return `
        <div class="match-card signal-${signal}">
            <div class="card-top">
                <div class="signal-badge ${signal}">${signal}</div>
                <div class="match-players">
                    <span class="fav">${r.fav_name}</span> vs ${r.dog_name}
                </div>
            </div>
            <div class="match-meta">${tagsHTML}</div>
            ${detailHTML}
            ${pricesHTML}
        </div>`;
}
