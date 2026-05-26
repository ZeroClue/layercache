// LayerCache Dashboard JS

function showToast(message, type, duration) {
  type = type || "info";
  duration = duration || 4000;
  var container = document.getElementById("toast-container");
  if (!container) return;
  var toast = document.createElement("div");
  toast.className = "toast toast-" + type;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(function () {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(100%)";
    toast.style.transition = "all 0.3s ease";
    setTimeout(function () { toast.remove(); }, 300);
  }, duration);
}

// ---- Charts ----

function initCharts() {
  loadHistoryChart("chart-requests", "llm_requests_total", "Requests/s", "#3b82f6", "rate");
  loadHistoryChart("chart-cache-rate", "semantic_cache_hit_rate", "Hit Rate", "#22c55e");
  loadHistoryChart("chart-tokens", "total_input_tokens", "Input Tokens", "#eab308");
}

async function loadHistoryChart(canvasId, seriesName, label, color, transform) {
  var canvas = document.getElementById(canvasId);
  if (!canvas) return;
  var existing = Chart.getChart(canvas);
  if (existing) existing.destroy();
  try {
    var params = new URLSearchParams({ range: "86400", resolution: "300" });
    var resp = await fetch("/v1/cache/metrics/history?" + params.toString());
    if (!resp.ok) return;
    var data = await resp.json();
    var series = (data.series || []).find(function (s) {
      return s.name === seriesName && Object.keys(s.labels || {}).length === 0;
    });
    if (!series || !series.buckets || series.buckets.length === 0) return;
    var points = series.buckets.map(function (b) { return { ts: b.ts * 1000, v: b.avg }; });
    if (transform === "rate") {
      var prev = null;
      points = points.map(function (p) {
        if (prev === null) { prev = p; return { ts: p.ts, v: null }; }
        var dt = (p.ts - prev.ts) / 1000;
        var dv = p.v - prev.v;
        prev = p;
        return { ts: p.ts, v: dt > 0 ? dv / dt : null };
      });
    }
    var labels = points.map(function (p) {
      var d = new Date(p.ts);
      return (d.getHours() < 10 ? "0" : "") + d.getHours() + ":" +
             (d.getMinutes() < 10 ? "0" : "") + d.getMinutes();
    });
    var values = points.map(function (p) { return p.v; });
    new Chart(canvas, {
      type: "line",
      data: {
        labels: labels,
        datasets: [{
          label: label,
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
        elements: { point: { radius: 0 } }
      }
    });
  } catch (e) {
    console.error("Chart load failed:", e);
  }
}

// Init charts on load
document.addEventListener("DOMContentLoaded", initCharts);
document.addEventListener("htmx:afterSwap", function (evt) {
  if (evt.detail && evt.detail.target && evt.detail.target.id === "main-content") {
    initCharts();
  }
});

// Toast on template actions
document.addEventListener("htmx:afterRequest", function (evt) {
  var path = evt.detail.pathInfo && evt.detail.pathInfo.requestPath;
  var verb = evt.detail.requestConfig && evt.detail.requestConfig.verb;
  if (!path) return;
  if (path === "/dashboard/templates/reload") {
    showToast(
      evt.detail.successful ? "Templates reloaded from disk" : "Failed to reload templates",
      evt.detail.successful ? "success" : "error"
    );
  }
  if (verb === "delete" && path.match(/^\/dashboard\/templates\//)) {
    showToast(
      evt.detail.successful ? "Template deleted" : "Failed to delete template",
      evt.detail.successful ? "success" : "error"
    );
  }
});
