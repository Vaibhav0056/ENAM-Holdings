/* FINN charts: one renderer for every chart spec the API computes (finn/charts.py). Inline SVG, no library.
   Thin 1.5px lines, muted grid, direct labels instead of legends, bars from zero, gaps for missing data.
   Every mark carries data-s (series) and data-i (point) so a tap can show its value and source.
   FinnCharts.render(spec) -> SVG markup string. */
(function () {
  const W = 320;
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const num = (v) => typeof v === "number" && isFinite(v);

  function fmt(v, unit) {
    if (!num(v)) return "–";
    if (unit === "%") return v.toFixed(1) + "%";
    if (unit === "₹ cr") return "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: Math.abs(v) >= 100 ? 0 : 1 });
    if (unit === "₹") return "₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 });
    if (unit === "×") return v.toFixed(2) + "×";
    return v.toLocaleString("en-IN", { maximumFractionDigits: 1 });
  }
  function short(x) {           // compact axis labels: "Q2 FY27" -> "Q2·27", "2026-11-13" -> "13 Nov"
    if (/^Q\d FY\d\d$/.test(x)) return x.replace(" FY", "·");
    if (/^\d{4}-\d{2}-\d{2}$/.test(x)) return new Date(x).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
    return x.length > 10 ? x.slice(0, 9) + "…" : x;
  }
  function scale(vals, lo0, top, bot) {
    const v = vals.filter(num);
    let lo = Math.min(0, ...v), hi = Math.max(0, ...v);
    if (lo0 === false && v.length) { lo = Math.min(...v); hi = Math.max(...v); const pad = (hi - lo || Math.abs(hi) || 1) * 0.15; lo -= pad; hi += pad; }
    if (hi === lo) hi = lo + 1;
    return { y: (x) => bot - ((x - lo) / (hi - lo)) * (bot - top), lo, hi };
  }
  const xpos = (n, i, l = 18, r = W - 18) => (n === 1 ? (l + r) / 2 : l + (i / (n - 1)) * (r - l));
  const band = (n, i, l = 10, r = W - 10) => { const w = (r - l) / n; return { x: l + i * w, w }; };
  const text = (x, y, s, a = "middle", cls = "") => `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="${a}" class="${cls}">${esc(s)}</text>`;
  const anchor = (n, i) => (n > 1 && i === 0 ? "start" : n > 1 && i === n - 1 ? "end" : "middle");

  function linePath(pts) {      // pts: [{x, y} | null] -> path with gaps
    let d = "", pen = false;
    for (const p of pts) {
      if (!p) { pen = false; continue; }
      d += `${pen ? "L" : "M"}${p.x.toFixed(1)} ${p.y.toFixed(1)} `; pen = true;
    }
    return d.trim();
  }

  // ---- bars (grouped) + optional line on a right axis + optional threshold line ----
  function bars(spec) {
    const H = 176, top = 30, bot = 146, n = spec.x.length;
    const barS = spec.series.filter((s) => s.as === "bar"), lineS = spec.series.filter((s) => s.as === "line");
    const L = scale(barS.flatMap((s) => s.values).concat(spec.threshold ? [spec.threshold.value] : []), true, top, bot);
    let g = `<line class="grid" x1="0" x2="${W}" y1="${L.y(0)}" y2="${L.y(0)}"/>`;
    const k = barS.length, shades = ["var(--accent)", "color-mix(in srgb, var(--accent) 42%, var(--surface-2))", "var(--text-2)"];
    for (let i = 0; i < n; i++) {
      const b = band(n, i), inner = b.w * 0.72, bw = inner / Math.max(1, k);
      barS.forEach((s, j) => {
        const v = s.values[i]; const si = spec.series.indexOf(s);
        if (!num(v)) { g += `<text x="${(b.x + b.w * 0.14 + j * bw + bw / 2).toFixed(1)}" y="${(L.y(0) - 3).toFixed(1)}" text-anchor="middle" class="gap">·</text>`; return; }
        const y = L.y(Math.max(0, v)), h = Math.abs(L.y(v) - L.y(0));
        g += `<rect data-s="${si}" data-i="${i}" x="${(b.x + b.w * 0.14 + j * bw).toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(1, bw - 1.5).toFixed(1)}" height="${Math.max(0.5, h).toFixed(1)}" rx="1.5" fill="${shades[j]}"/>`;
      });
      const every = n > 12 ? 5 : n > 8 ? 2 : 1;      // thin the axis so labels never overlap
      if (i % every === 0 || i === n - 1) g += text(b.x + b.w / 2, H - 14, short(spec.x[i]), n > 8 ? anchor(n, i) : "middle");
    }
    if (spec.threshold) {
      const ty = L.y(spec.threshold.value);
      g += `<line class="th" x1="0" x2="${W}" y1="${ty}" y2="${ty}"/>` + text(W - 2, ty - 4, spec.threshold.label, "end", "thl");
    }
    lineS.forEach((s) => {
      const R = scale(s.values, false, top + 6, bot - 20), si = spec.series.indexOf(s);
      const pts = s.values.map((v, i) => (num(v) ? { x: band(n, i).x + band(n, i).w / 2, y: R.y(v) } : null));
      g += `<path class="line" d="${linePath(pts)}"/>`;
      pts.forEach((p, i) => { if (p) g += `<circle data-s="${si}" data-i="${i}" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="2.5" class="dot"/>`; });
    });
    // direct labels: each series with its latest value, in one row above the plot, next to its own mark
    let lx = 0;
    spec.series.forEach((s, si) => {
      const last = s.values.filter(num).pop();
      const label = `${s.name}${num(last) ? " " + fmt(last, s.unit) : ""}`;
      const j = barS.indexOf(s);
      g += j >= 0 ? `<rect x="${lx}" y="5" width="8" height="8" rx="1.5" fill="${shades[j]}"/>`
                  : `<line x1="${lx}" x2="${lx + 9}" y1="9" y2="9" class="line"/><circle cx="${lx + 4.5}" cy="9" r="2" class="dot"/>`;
      g += text(lx + 12, 12.5, label, "start", "lbl");
      lx += 12 + label.length * 5.6 + 10;
    });
    return { H, g };
  }

  // ---- waterfall: totals from zero, steps floating from the running total ----
  function waterfall(spec) {
    const H = 180, top = 18, bot = 140, s = spec.series[0], n = s.values.length;
    const ends = [], starts = [];
    let run = 0;
    s.values.forEach((v, i) => {
      if (s.steps[i] === "total") { starts.push(0); ends.push(v); run = v; } else { starts.push(run); ends.push(run + v); run += v; }
    });
    const L = scale(starts.concat(ends), true, top, bot);
    let g = `<line class="grid" x1="0" x2="${W}" y1="${L.y(0)}" y2="${L.y(0)}"/>`;
    for (let i = 0; i < n; i++) {
      const b = band(n, i), x = b.x + b.w * 0.18, w = b.w * 0.64;
      const y1 = L.y(Math.max(starts[i], ends[i])), y2 = L.y(Math.min(starts[i], ends[i]));
      const fill = s.steps[i] === "total" ? "var(--accent)" : s.values[i] >= 0 ? "var(--up)" : "var(--down)";
      g += `<rect data-s="0" data-i="${i}" x="${x.toFixed(1)}" y="${y1.toFixed(1)}" width="${w.toFixed(1)}" height="${Math.max(0.8, y2 - y1).toFixed(1)}" rx="1.5" fill="${fill}"/>`;
      g += text(b.x + b.w / 2, y1 - 4, (s.steps[i] === "total" ? "" : s.values[i] >= 0 ? "+" : "") + s.values[i].toFixed(1), "middle", "lbl");
      if (i < n - 1) g += `<line class="grid" x1="${(x + w).toFixed(1)}" x2="${(band(n, i + 1).x + b.w * 0.18).toFixed(1)}" y1="${L.y(ends[i]).toFixed(1)}" y2="${L.y(ends[i]).toFixed(1)}" stroke-dasharray="2 2"/>`;
      g += text(b.x + b.w / 2, H - 18, (spec.axis || spec.x)[i]);
    }
    return { H, g };
  }

  // ---- stacked bars (verdicts) ----
  const VERDICT = { delivered: "var(--up)", partial: "var(--watch)", missed: "var(--down)", pending: "color-mix(in srgb, var(--text-2) 55%, var(--surface-2))", unverifiable: "var(--border)" };
  function stacked(spec) {
    const H = 170, top = 14, bot = 140, n = spec.x.length;
    const totals = spec.x.map((_, i) => spec.series.reduce((a, s) => a + (s.values[i] || 0), 0));
    const L = scale(totals, true, top, bot);
    let g = `<line class="grid" x1="0" x2="${W}" y1="${L.y(0)}" y2="${L.y(0)}"/>`;
    for (let i = 0; i < n; i++) {
      const b = band(n, i); let acc = 0;
      spec.series.forEach((s, si) => {
        const v = s.values[i] || 0; if (!v) return;
        const y = L.y(acc + v), h = L.y(acc) - y;
        g += `<rect data-s="${si}" data-i="${i}" x="${(b.x + b.w * 0.2).toFixed(1)}" y="${y.toFixed(1)}" width="${(b.w * 0.6).toFixed(1)}" height="${Math.max(0.8, h - 0.8).toFixed(1)}" fill="${VERDICT[s.verdict] || "var(--accent)"}"/>`;
        acc += v;
      });
      if (totals[i]) g += text(b.x + b.w / 2, L.y(totals[i]) - 4, totals[i], "middle", "lbl");
      g += text(b.x + b.w / 2, H - 14, short(spec.x[i]));
    }
    return { H, g };
  }

  // ---- line / step / band / spark / price ----
  function lines(spec) {
    const s = spec.series[0], n = spec.x.length, kind = spec.kind;
    const H = kind === "spark" ? 96 : 160, top = 18, bot = kind === "spark" ? 66 : 128;
    const vals = s.values.concat(spec.threshold ? [spec.threshold.value] : []);
    const L = scale(vals, false, top, bot);
    const pts = s.values.map((v, i) => (num(v) ? { x: xpos(n, i), y: L.y(v) } : null));
    let g = `<line class="grid" x1="0" x2="${W}" y1="${bot}" y2="${bot}"/>`;
    if (spec.threshold) {
      const ty = L.y(spec.threshold.value), good = spec.threshold.op === "<=" ? [ty, bot] : [top - 6, ty];
      g += `<rect x="0" y="${Math.min(...good).toFixed(1)}" width="${W}" height="${Math.abs(good[1] - good[0]).toFixed(1)}" class="okband"/>`;
      g += `<line class="th" x1="0" x2="${W}" y1="${ty}" y2="${ty}"/>` + text(W - 2, ty - 4, spec.threshold.label, "end", "thl");
    }
    if (kind === "step") {
      let d = "", prev = null;
      pts.forEach((p) => { if (!p) return; d += prev ? `H${p.x.toFixed(1)} V${p.y.toFixed(1)} ` : `M${p.x.toFixed(1)} ${p.y.toFixed(1)} `; prev = p; });
      g += `<path class="line" d="${d.trim()}"/>`;
    } else g += `<path class="line" d="${linePath(pts)}"/>`;
    const dense = n > 24;
    pts.forEach((p, i) => {
      if (!p) return;
      const bad = spec.threshold && (spec.threshold.op === "<=" ? s.values[i] > spec.threshold.value : s.values[i] < spec.threshold.value);
      if (!dense) g += `<circle data-s="0" data-i="${i}" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${bad ? 3.2 : 2.5}" class="${bad ? "dot bad" : "dot"}"/>`;
      if (!dense && n <= 8) g += text(p.x, p.y - 7, fmt(s.values[i], s.unit), anchor(n, i), "lbl");
    });
    if (dense) {                 // price: invisible hit columns for taps, labels at the ends and the high
      const step = (W - 36) / (n - 1);
      pts.forEach((p, i) => { if (p && i % 3 === 0) g += `<rect data-s="0" data-i="${i}" x="${(p.x - step * 1.5).toFixed(1)}" y="${top}" width="${(step * 3).toFixed(1)}" height="${bot - top}" fill="transparent"/>`; });
      const hiI = s.values.indexOf(Math.max(...s.values.filter(num)));
      g += text(pts[hiI].x, pts[hiI].y - 6, "high " + fmt(s.values[hiI], s.unit), pts[hiI].x > W - 60 ? "end" : pts[hiI].x < 60 ? "start" : "middle", "lbl");
      const last = pts.filter(Boolean).pop();
      g += `<circle cx="${last.x.toFixed(1)}" cy="${last.y.toFixed(1)}" r="2.5" class="dot"/>`;
      g += text(18, H - 14, short(spec.x[0]), "start") + text(W - 18, H - 14, short(spec.x[n - 1]), "end");
      (spec.events || []).forEach((e, k) => {
        const x = xpos(n, e.i), y = bot + 8 + (k % 2) * 7;
        const c = { results: "var(--accent)", mgmt: "var(--watch)", governance: "var(--down)" }[e.kind] || "var(--text-2)";
        g += `<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${top}" y2="${bot}" class="evline"/>`;
        g += `<g data-e="${k}" class="ev"><rect x="${(x - 7).toFixed(1)}" y="${(y - 6).toFixed(1)}" width="14" height="12" fill="transparent"/>`
           + `<text x="${x.toFixed(1)}" y="${(y + 3).toFixed(1)}" text-anchor="middle" class="evmark" style="fill:${c}">${{ results: "R", mgmt: "M", governance: "G" }[e.kind] || "•"}</text></g>`;
      });
    } else {
      const every = n > 6 ? 2 : 1;
      spec.x.forEach((x, i) => { if (i % every === 0 || i === n - 1) g += text(xpos(n, i), H - 14, short(x), anchor(n, i)); });
    }
    return { H, g };
  }

  // ---- heat grid ----
  function heat(spec) {
    const gr = spec.grid, rows = gr.rows.length, cols = gr.cols.length, LW = 112, cw = (W - LW) / cols, ch = 18;
    const H = 22 + rows * ch + 6, max = Math.max(1, ...gr.values.flat());
    let g = "";
    gr.cols.forEach((c, j) => { g += text(LW + j * cw + cw / 2, 12, short(c)); });
    gr.rows.forEach((r, i) => {
      const hot = (gr.highlight || []).includes(r);
      g += `<text x="0" y="${(22 + i * ch + 12).toFixed(1)}" class="${hot ? "lbl watchtxt" : ""}">${esc(r.length > 20 ? r.slice(0, 19) + "…" : r)}</text>`;
      gr.cols.forEach((c, j) => {
        const v = gr.values[i][j];
        g += `<rect data-s="${i}" data-i="${j}" x="${(LW + j * cw + 1).toFixed(1)}" y="${(22 + i * ch).toFixed(1)}" width="${(cw - 2).toFixed(1)}" height="${ch - 2}" rx="3" `
           + `style="fill: color-mix(in srgb, var(--accent) ${v ? Math.round(10 + (v / max) * 70) : 0}%, var(--surface-2))"/>`;
        if (v) g += text(LW + j * cw + cw / 2, 22 + i * ch + 12, v, "middle", v / max > 0.55 ? "oninv" : "");
      });
    });
    return { H, g };
  }

  function render(spec) {
    if (!spec) return "";
    const r = spec.kind === "waterfall" ? waterfall(spec) : spec.kind === "stacked" ? stacked(spec) :
      spec.kind === "heat" ? heat(spec) : spec.kind === "bars" ? bars(spec) : lines(spec);
    return `<svg class="fchart" viewBox="0 0 ${W} ${r.H}" role="img" aria-label="${esc(spec.summary || spec.title)}">${r.g}</svg>`;
  }

  // what a tap on (series s, point i) means, with its source if the spec has one
  function describe(spec, s, i) {
    if (spec.kind === "heat") return { text: `${spec.grid.rows[s]} · ${spec.grid.cols[i]}: ${spec.grid.values[s][i]} ${spec.unit}`, source: null };
    const ser = spec.series[s];
    if (!ser) return null;
    const v = ser.values[i];
    const src = spec.sources && spec.sources[s] ? spec.sources[s][i] : null;
    return { text: `${ser.name} · ${spec.x[i]}: ${num(v) ? fmt(v, ser.unit) : "not in sources"}`, source: src || null };
  }

  // A whole chart card in one call: finding as title, the SVG, a tap readout with the point's source, a
  // "View as table" toggle and the note. A source tap dispatches "finn-evidence" for the app's evidence sheet; an
  // event marker tap follows its link. Records the render time on window.__finnChartMs (gallery and tests).
  function mount(el, spec) {
    if (!el || !spec) return;
    const t0 = performance.now();
    const fig = document.createElement("figure");
    fig.className = "card chartbox";
    const table = spec.table ? `<div class="scroll-x ct-table" hidden><table class="tbl"><thead><tr>${spec.table.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>`
      + `<tbody>${spec.table.rows.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : "";
    fig.innerHTML = `<figcaption class="small ct-title">${esc(spec.title)}</figcaption>`
      + `<div class="ct-svg mt8">${render(spec)}</div>`
      + `<div class="inset mt8 xs ct-tip" hidden role="status" aria-live="polite"><span class="ct-tiptext"></span> <button class="cite ct-src" hidden></button> <a class="cite ct-link" hidden>Open filing →</a></div>`
      + table
      + `<div class="row mt8"><span class="xs t2 grow">${esc(spec.note || "")}</span>`
      + (spec.table ? `<button type="button" class="btn quiet ct-toggle" aria-pressed="false">View as table</button>` : "") + `</div>`;
    el.replaceChildren(fig);
    const tip = fig.querySelector(".ct-tip"), src = fig.querySelector(".ct-src"), link = fig.querySelector(".ct-link");
    let srcData = null;
    fig.querySelector(".ct-svg").addEventListener("click", (e) => {
      const ev = e.target.closest("[data-e]");
      const pt = e.target.closest("[data-i]");
      srcData = null; src.hidden = true; link.hidden = true;
      if (ev) {
        const x = spec.events[+ev.dataset.e];
        fig.querySelector(".ct-tiptext").textContent = `${x.date || ""} · ${x.label}`;
        if (x.link) { link.href = x.link; link.hidden = false; }
      } else if (pt) {
        const d = describe(spec, +pt.dataset.s, +pt.dataset.i);
        if (!d) return;
        fig.querySelector(".ct-tiptext").textContent = d.text;
        if (d.source && d.source.document_id && d.source.page) {
          srcData = d.source; src.textContent = `Source: ${d.source.title || "document"}, page ${d.source.page}`; src.hidden = false;
        } else if (d.source && d.source.filing_id) {
          link.href = `#card/${d.source.filing_id}`; link.hidden = false;
        }
      } else { tip.hidden = true; return; }
      tip.hidden = false;
    });
    src.addEventListener("click", () => srcData && fig.dispatchEvent(new CustomEvent("finn-evidence", { bubbles: true, detail: srcData })));
    const tg = fig.querySelector(".ct-toggle");
    if (tg) tg.addEventListener("click", () => {
      const on = tg.getAttribute("aria-pressed") !== "true";
      tg.setAttribute("aria-pressed", String(on)); tg.textContent = on ? "View as chart" : "View as table";
      fig.querySelector(".ct-table").hidden = !on; fig.querySelector(".ct-svg").hidden = on; tip.hidden = true;
    });
    const ms = performance.now() - t0;
    window.__finnChartMs = Math.max(window.__finnChartMs || 0, ms);
    fig.dataset.ms = ms.toFixed(1);
  }

  window.FinnCharts = { render, describe, fmt, mount };
})();
