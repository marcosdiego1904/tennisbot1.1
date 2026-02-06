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
        html += '<h3>Kalshi Debug — Series Discovery</h3>';

        // Discovery results
        if (data.discovery) {
            const d = data.discovery;

            // filters_by_sport
            html += '<h4 style="color: #d29922; margin: 12px 0 8px;">1. /search/filters_by_sport (Tennis)</h4>';
            if (d.filters_by_sport) {
                html += `<div style="background:#0d1117; padding:8px; border-radius:4px; font-size:11px; max-height:200px; overflow-y:auto; word-break:break-all;">`;
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
