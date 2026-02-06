// TennisBot Dashboard — frontend logic

document.addEventListener("DOMContentLoaded", () => {
    const now = new Date();
    document.getElementById("currentDate").textContent =
        now.toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" });
});


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
        container.innerHTML = `
            <div class="empty-state">
                <p>${data.message || "No tennis markets found."}</p>
            </div>`;
        return;
    }

    container.innerHTML = '<div class="matches-grid">'
        + data.results.map(renderMatchCard).join("")
        + '</div>';
}


// --- Debug: show raw Kalshi data on the dashboard ---

async function fetchDebug() {
    const container = document.getElementById("debugContainer");
    container.innerHTML = '<div class="loading"><div class="spinner"></div><p>Fetching raw Kalshi data...</p></div>';

    try {
        const resp = await fetch("/api/debug/kalshi");
        const data = await resp.json();

        let html = '<div class="manual-panel" style="margin-top: 12px;">';
        html += '<h3>Raw Kalshi Debug Info</h3>';

        // Series results
        if (data.series_tried) {
            html += '<h4 style="color: #58a6ff; margin: 12px 0 8px;">Series Tickers Tried</h4>';
            for (const s of data.series_tried) {
                const status = s.error ? `<span style="color:#f85149;">ERROR: ${s.error}</span>` :
                    `<span style="color:#3fb950;">${s.count} markets found</span>`;
                html += `<p style="margin: 4px 0;"><strong>${s.series}</strong>: ${status}</p>`;

                if (s.sample && s.sample.length > 0) {
                    for (const m of s.sample) {
                        html += `<div style="background:#0d1117; padding:8px; margin:4px 0 4px 16px; border-radius:4px; font-size:12px; word-break:break-all;">`;
                        html += `ticker: <strong>${m.ticker || '-'}</strong><br>`;
                        html += `title: <strong>${m.title || '-'}</strong><br>`;
                        html += `subtitle: ${m.subtitle || '-'}<br>`;
                        html += `yes: ${m.yes_price}¢ | no: ${m.no_price}¢ | vol: ${m.volume}<br>`;
                        html += `event: ${m.event_ticker || '-'}`;
                        html += '</div>';
                    }
                }
            }
        }

        // Tennis events from broad search
        if (data.tennis_events_from_broad_search) {
            html += `<h4 style="color: #58a6ff; margin: 12px 0 8px;">Tennis Events (Broad Search): ${data.tennis_events_from_broad_search.length}</h4>`;
            for (const e of data.tennis_events_from_broad_search) {
                html += `<div style="background:#0d1117; padding:6px 8px; margin:4px 0; border-radius:4px; font-size:12px; word-break:break-all;">`;
                html += `<strong>${e.ticker}</strong> — ${e.title} [series: ${e.series}]`;
                html += '</div>';
            }
        }

        // Series found in events
        if (data.tennis_series_found_in_events) {
            html += `<h4 style="color: #d29922; margin: 12px 0 8px;">Actual Tennis Series Tickers Found</h4>`;
            html += `<p style="font-size:14px;"><strong>${data.tennis_series_found_in_events.join(', ')}</strong></p>`;
        }

        // Parse results
        html += `<h4 style="color: #58a6ff; margin: 12px 0 8px;">Parsing Results</h4>`;
        html += `<p>Parsed OK: <strong style="color:#3fb950;">${data.parsed_ok || 0}</strong></p>`;

        if (data.parse_failures && data.parse_failures.length > 0) {
            html += `<p>Parse failures: <strong style="color:#f85149;">${data.parse_failures.length}</strong></p>`;
            for (const f of data.parse_failures) {
                html += `<div style="background:#0d1117; padding:8px; margin:4px 0; border-radius:4px; font-size:12px; color:#f85149; word-break:break-all;">`;
                html += `<strong>${f.ticker}</strong>: ${f.reason}<br>`;
                html += `title: "${f.title}" | subtitle: "${f.subtitle}" | yes: ${f.yes_price}¢`;
                html += '</div>';
            }
        }

        html += `<p style="margin-top:8px; color:#8b949e;">Total open events on Kalshi: ${data.total_open_events || '?'}</p>`;
        html += '</div>';

        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = `<div style="color:#f85149; padding:12px;">Debug error: ${err.message}</div>`;
    }
}


function renderMatchCard(r) {
    const signal = r.signal;
    const isSkip = signal === "SKIP";

    // Price section
    let pricesHTML = "";
    if (!isSkip) {
        const edgeClass = r.edge > 0 ? "positive" : "negative";
        pricesHTML = `
            <div class="match-prices">
                <div class="price-block kalshi">
                    <span class="label">Kalshi</span>
                    <span class="price">${r.kalshi_price}¢</span>
                </div>
                <div class="price-block target">
                    <span class="label">Target</span>
                    <span class="price">${r.target_price}¢</span>
                </div>
                <div class="price-block edge">
                    <span class="label">Edge</span>
                    <span class="price ${edgeClass}">${r.edge > 0 ? "+" : ""}${r.edge}¢</span>
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
            <div class="signal-badge ${signal}">${signal}</div>
            <div class="match-info">
                <div class="match-players">
                    <span class="fav">${r.fav_name}</span> vs ${r.dog_name}
                </div>
                <div class="match-meta">${tagsHTML}</div>
                ${detailHTML}
            </div>
            ${pricesHTML}
        </div>`;
}
