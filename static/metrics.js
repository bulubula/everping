(() => {
  const rows = window.__metricsRows || [];
  const host = document.getElementById("metricsCharts");
  if (!host || rows.length === 0) {
    return;
  }
  const parseTs = (s) => {
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? null : d;
  };

  const filterRows = (range) => {
    const now = new Date();
    if (range === "latest") {
      return rows.slice(-300);
    }
    if (range === "today") {
      const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      return rows.filter(r => {
        const d = parseTs(r.ts);
        return d && d >= start;
      });
    }
    if (range === "week") {
      const start = new Date(now);
      start.setDate(now.getDate() - 7);
      return rows.filter(r => {
        const d = parseTs(r.ts);
        return d && d >= start;
      });
    }
    if (range === "month") {
      const start = new Date(now);
      start.setDate(now.getDate() - 30);
      return rows.filter(r => {
        const d = parseTs(r.ts);
        return d && d >= start;
      });
    }
    return rows;
  };

  const downsample = (points, maxPoints) => {
    if (points.length <= maxPoints) return points;
    const firstTs = parseTs(points[0].ts);
    const lastTs = parseTs(points[points.length - 1].ts);
    if (!firstTs || !lastTs) return points.slice(-maxPoints);
    const span = lastTs.getTime() - firstTs.getTime();
    if (span <= 0) return points.slice(-maxPoints);
    const bucketMs = span / maxPoints;
    const buckets = new Array(maxPoints);
    for (const p of points) {
      const t = parseTs(p.ts);
      if (!t) continue;
      const idx = Math.min(maxPoints - 1, Math.floor((t.getTime() - firstTs.getTime()) / bucketMs));
      // keep the last point in each bucket
      buckets[idx] = p;
    }
    return buckets.filter(Boolean);
  };

  const buildSeries = (filtered) => {
    const series = new Map();
    filtered.forEach((r, idx) => {
      const key = `${r.task_name}:${r.key}`;
      if (!series.has(key)) {
        series.set(key, []);
      }
      const y = Number.parseFloat(r.value);
      if (Number.isFinite(y)) {
        series.get(key).push({ x: idx, y, ts: r.ts });
      }
    });
    for (const [k, points] of series.entries()) {
      series.set(k, downsample(points, 500));
    }
    return series;
  };

  const renderChart = (title, points) => {
    const col = document.createElement("div");
    col.className = "col-12";
    const card = document.createElement("div");
    card.className = "card p-3";
    const h3 = document.createElement("div");
    h3.style.fontWeight = "600";
    h3.style.marginBottom = "6px";
    h3.textContent = title;
    const canvas = document.createElement("canvas");
    canvas.height = 200;
    card.appendChild(h3);
    card.appendChild(canvas);
    col.appendChild(card);
    host.appendChild(col);
    requestAnimationFrame(() => {
      const ctx = canvas.getContext("2d");
      const w = canvas.width = card.clientWidth - 16;
      const h = canvas.height = 200;
      const padding = 28;
      const values = points.map(p => p.y);
      if (values.length === 0) {
        ctx.fillStyle = "#64748b";
        ctx.font = "12px sans-serif";
        ctx.fillText("no data", padding, padding + 12);
        return;
      }
      const minY = Math.min(...values);
      const maxY = Math.max(...values);
      const rangeY = maxY - minY || 1;
      const maxX = points.length - 1 || 1;

      const xToPx = x => padding + (x / maxX) * (w - padding * 2);
      const yToPx = y => h - padding - ((y - minY) / rangeY) * (h - padding * 2);

      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = "#cbd5e1";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padding, padding);
      ctx.lineTo(padding, h - padding);
      ctx.lineTo(w - padding, h - padding);
      ctx.stroke();

      ctx.fillStyle = "#64748b";
      ctx.font = "12px sans-serif";
      // y-axis ticks: min/mid/max
      const yMid = (minY + maxY) / 2;
      ctx.fillText(maxY.toFixed(2), 6, padding + 4);
      ctx.fillText(yMid.toFixed(2), 6, (padding + (h - padding)) / 2 + 4);
      ctx.fillText(minY.toFixed(2), 6, h - padding);
      // x-axis ticks: left/right timestamps
      const leftTs = points[0]?.ts || "";
      const rightTs = points[points.length - 1]?.ts || "";
      ctx.fillText(leftTs, padding, h - 8);
      const rightTextWidth = ctx.measureText(rightTs).width;
      ctx.fillText(rightTs, w - padding - rightTextWidth, h - 8);

      ctx.strokeStyle = "#2563eb";
      ctx.lineWidth = 2;
      ctx.beginPath();
      points.forEach((p, i) => {
        const x = xToPx(i);
        const y = yToPx(p.y);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });
  };

  const renderAll = (range) => {
    host.innerHTML = "";
    const filtered = filterRows(range);
    const series = buildSeries(filtered);
    for (const [key, points] of series.entries()) {
      renderChart(key, points);
    }
  };

  const buttons = document.querySelectorAll("[data-range]");
  buttons.forEach(btn => {
    btn.addEventListener("click", () => {
      const range = btn.getAttribute("data-range");
      buttons.forEach(b => {
        b.classList.remove("btn-primary");
        b.classList.add("btn-outline-secondary");
      });
      btn.classList.remove("btn-outline-secondary");
      btn.classList.add("btn-primary");
      renderAll(range);
    });
  });

  renderAll("latest");
})();
