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
