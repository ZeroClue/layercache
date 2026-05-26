// LayerCache Dashboard JS

function initCharts() {
  loadHistoryChart("chart-requests", "llm_requests_total", "Requests/s", "#3b82f6", "rate");
  loadHistoryChart("chart-cache-rate", "semantic_cache_hit_rate", "Hit Rate", "#22c55e");
  loadHistoryChart("chart-tokens", "total_input_tokens", "Input Tokens", "#eab308");
}

async function loadHistoryChart(canvasId, seriesName, label, color, transform) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  // Destroy existing chart instance if any
  const existing = Chart.getChart(canvas);
  if (existing) existing.destroy();

  try {
    const params = new URLSearchParams({ range: "86400", resolution: "300" });
    const resp = await fetch(`/v1/cache/metrics/history?${params}`);
    if (!resp.ok) return;
    const data = await resp.json();

    const series = (data.series || []).find(
      s => s.name === seriesName && Object.keys(s.labels || {}).length === 0
    );
    if (!series || !series.buckets || series.buckets.length === 0) return;

    let points = series.buckets.map(b => ({ ts: b.ts * 1000, v: b.avg }));

    if (transform === "rate") {
      let prev = null;
      points = points.map(p => {
        if (prev === null) { prev = p; return { ...p, v: null }; }
        const dt = (p.ts - prev.ts) / 1000;
        const dv = p.v - prev.v;
        prev = p;
        return { ...p, v: dt > 0 ? dv / dt : null };
      });
    }

    const labels = points.map(p => {
      const d = new Date(p.ts);
      return d.getHours().toString().padStart(2, "0") + ":" +
             d.getMinutes().toString().padStart(2, "0");
    });
    const values = points.map(p => p.v);

    new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label,
          data: values,
          borderColor: color,
          backgroundColor: color + "33",
          fill: true,
          tension: 0.2,
          pointRadius: 0,
          borderWidth: 2,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: "#8b8d95", maxTicksLimit: 8, maxRotation: 0 },
          },
          y: {
            beginAtZero: true,
            grid: { color: "#2a2d35" },
            ticks: { color: "#8b8d95" },
          }
        },
        elements: {
          point: { radius: 0 }
        }
      }
    });
  } catch (e) {
    console.error("Chart load failed:", e);
  }
}

// Init on first load
document.addEventListener("DOMContentLoaded", initCharts);

// Re-init after HTMX swap (DOMContentLoaded doesn't fire again)
document.addEventListener("htmx:afterSwap", function(evt) {
  if (evt.detail && evt.detail.target && evt.detail.target.id === "main-content") {
    initCharts();
  }
});
