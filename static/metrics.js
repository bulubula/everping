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

  const downsampleKeepFeatures = (points, maxPoints) => {
    if (points.length <= maxPoints) return points;

    const bucketCount = Math.max(1, Math.floor(maxPoints / 4));
    const span = points.length / bucketCount;
    const picked = new Map();

    const add = (p) => {
      if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) {
        picked.set(p.idx, p);
      }
    };

    for (let i = 0; i < bucketCount; i += 1) {
      const start = Math.floor(i * span);
      const end = Math.min(points.length, Math.floor((i + 1) * span));
      if (start >= end) continue;
      const bucket = points.slice(start, end);

      let minP = bucket[0];
      let maxP = bucket[0];
      for (const p of bucket) {
        if (p.y < minP.y) minP = p;
        if (p.y > maxP.y) maxP = p;
      }

      add(bucket[0]); // local start
      add(minP);      // local min
      add(maxP);      // local max
      add(bucket[bucket.length - 1]); // local end
    }

    // Keep global anchors as a hard guarantee.
    add(points[0]);
    add(points[points.length - 1]);
    add(points.reduce((a, b) => (a.y <= b.y ? a : b)));
    add(points.reduce((a, b) => (a.y >= b.y ? a : b)));

    let out = Array.from(picked.values()).sort((a, b) => a.idx - b.idx);
    if (out.length <= maxPoints) return out;

    // If still too many points, thin evenly but always preserve anchors.
    const anchors = new Map();
    anchors.set(out[0].idx, out[0]);
    anchors.set(out[out.length - 1].idx, out[out.length - 1]);
    const gMin = out.reduce((a, b) => (a.y <= b.y ? a : b));
    const gMax = out.reduce((a, b) => (a.y >= b.y ? a : b));
    anchors.set(gMin.idx, gMin);
    anchors.set(gMax.idx, gMax);

    const remainSlots = Math.max(0, maxPoints - anchors.size);
    const rest = out.filter(p => !anchors.has(p.idx));
    const step = remainSlots > 0 ? rest.length / remainSlots : rest.length;
    const thinned = [];
    for (let i = 0; i < remainSlots && rest.length > 0; i += 1) {
      const idx = Math.min(rest.length - 1, Math.floor(i * step));
      thinned.push(rest[idx]);
    }

    out = Array.from(new Map([...anchors, ...thinned.map(p => [p.idx, p])]).values())
      .sort((a, b) => a.idx - b.idx);
    return out.slice(0, maxPoints);
  };

  const buildSeries = (filtered) => {
    const series = new Map();
    filtered.forEach((r, idx) => {
      const key = `${r.task_name}:${r.key}`;
      if (!series.has(key)) {
        series.set(key, []);
      }
      const y = Number.parseFloat(r.value);
      const t = parseTs(r.ts);
      if (Number.isFinite(y) && t) {
        series.get(key).push({ idx, x: t.getTime(), y, ts: r.ts });
      }
    });
    for (const [k, points] of series.entries()) {
      points.sort((a, b) => a.x - b.x);
      series.set(k, downsampleKeepFeatures(points, 500));
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
      const minX = points[0].x;
      const maxX = points[points.length - 1].x;
      const rangeX = maxX - minX || 1;
      const xToPx = x => padding + ((x - minX) / rangeX) * (w - padding * 2);
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
      const midTs = points[Math.floor(points.length / 2)]?.ts || "";
      const rightTs = points[points.length - 1]?.ts || "";
      ctx.fillText(leftTs, padding, h - 8);
      const midTextWidth = ctx.measureText(midTs).width;
      ctx.fillText(midTs, (w - midTextWidth) / 2, h - 8);
      const rightTextWidth = ctx.measureText(rightTs).width;
      ctx.fillText(rightTs, w - padding - rightTextWidth, h - 8);

      ctx.strokeStyle = "#2563eb";
      ctx.lineWidth = 2;
      ctx.beginPath();
      points.forEach((p, i) => {
        const x = xToPx(p.x);
        const y = yToPx(p.y);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();

      const minPoint = points.reduce((a, b) => (a.y <= b.y ? a : b));
      const maxPoint = points.reduce((a, b) => (a.y >= b.y ? a : b));
      const drawMarker = (p, color, label) => {
        const x = xToPx(p.x);
        const y = yToPx(p.y);
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(x, y, 3.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.font = "11px sans-serif";
        ctx.fillText(label, x + 6, y - 6);
      };
      drawMarker(maxPoint, "#dc2626", `max ${maxPoint.y.toFixed(2)}`);
      drawMarker(minPoint, "#0891b2", `min ${minPoint.y.toFixed(2)}`);
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
