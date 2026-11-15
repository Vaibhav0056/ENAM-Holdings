/* FINN PWA: one Alpine component. Every number on screen comes from the API (Python computed it); this file only
   formats and arranges. Hash routes: #today #holdings #holding/SYM #research #research/SYM #alerts #settings
   #card/ID #gallery. Charts are inline SVG built here from API numbers. */

const TYPE_LABEL = {
  results: "Results", mgmt_change: "Mgmt change", governance: "Governance", m_and_a: "M&A", fundraise: "Fundraise",
  order_win: "Order", credit_rating: "Rating", insider_pledge: "Insider / pledge", dividend_action: "Dividend",
  litigation_regulatory: "Legal / regulatory", investor_meet: "Investor meet", board_meeting_notice: "Board meeting",
  agm_egm: "AGM / EGM", routine_compliance: "Compliance", other: "Update",
};
const STATUS_LABEL = { intact: "Intact", watch: "Watch", at_risk: "At risk" };
const IMPACT_LABEL = { supports: "Supports thesis", watch: "Watch", threatens: "Threatens thesis" };
const VERDICT_LABEL = { delivered: "Delivered", partial: "Partial", missed: "Missed", pending: "Pending", unverifiable: "Unverifiable" };
const KPI_LABEL = { revenue_growth_yoy: "Revenue growth YoY", ebitda_margin: "EBITDA margin", pat_growth_yoy: "PAT growth YoY", pat_margin: "PAT margin" };
const WINDOWS = [["since_last", "Since last check"], ["24h", "24h"], ["3d", "3 days"], ["7d", "7 days"], ["30d", "30 days"]];
const DEEP = [["results", "Results history"], ["cash_quality", "Cash quality"], ["balance_sheet", "Balance sheet"], ["language_diff", "Language diff"],
              ["hedge_meter", "Hedge-meter"], ["dodge_detector", "Dodge detector"], ["worry_map", "Worry map"],
              ["fine_print", "Fine print"], ["peer_compare", "Peer compare"]];
const WAKING_MS = 2500;
const IST = { timeZone: "Asia/Kolkata" };

function store(key, val) {           // per-viewer conveniences only; never required for correctness
  try { if (val === undefined) return JSON.parse(localStorage.getItem(key)); localStorage.setItem(key, JSON.stringify(val)); }
  catch (e) { return null; }
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function finn() {
  return {
    route: ((location.hash || "#today").slice(1).split("/")[0]) || "today", param: null,
    theme: store("finn.theme") || "system",
    net: { waking: false, error: null, stale: false }, warm: false,
    win: store("finn.window") || "24h", custom: { from: "", to: "" }, pick: false,
    digest: null, catalysts: [], catalystsLoaded: false, routineOpen: false, worthAll: false,
    holdings: null, holding: null, editing: false, draft: null, appState: null,
    logos: {}, alerts: null, unread: 0, unreadOlder: 0, rules: null, alertsAll: false,
    rq: { ticker: "", job: null, report: null, stage: null, progress: 0, stages: [], error: null, open: {}, st: {}, dur: {}, t0: 0 },
    rtab: "ledger", recent: store("finn.recent") || [], tick: Date.now(),
    sheet: null, prevSheet: null, cardDetail: null, evidence: null, whyCard: null, menuCard: null,
    snoozed: store("finn.snooze") || {},
    push: { supported: "serviceWorker" in navigator && "PushManager" in window, state: "", msg: "" },
    isIOS: /iphone|ipad|ipod/i.test(navigator.userAgent), standalone: matchMedia("(display-mode: standalone)").matches,
    toast: "", toastUndo: null,
    gal: { digest: null, sw: true, sl: 2.0, tab: "Ledger" },
    hc: null, rc: null, ac: {}, gc: null,           // chart specs: holding, research, alerts, gallery
    pf: { symbol: "", busy: false, job: null },      // add-a-holding form and its onboarding job
    featureFlags: null,                             // GET /api/v2/flags (finn/flags.py)
    ruleTypes: ["results", "mgmt_change", "governance", "m_and_a", "fundraise", "order_win", "credit_rating", "litigation_regulatory", "insider_pledge"],
    deepRecipes: DEEP,

    init() {
      const q = new URLSearchParams(location.search).get("theme");
      if (q === "light" || q === "dark") this.theme = q; else this.applyTheme();
      this.loadLogos();
      window.addEventListener("hashchange", () => this.go());
      window.addEventListener("finn-evidence", (e) => this.openEvidence(e.detail.document_id, e.detail.page, e.detail.quote));
      this.api("/api/v2/flags").then((f) => (this.featureFlags = f)).catch(() => { this.net.error = null; });
      this.go();
      this.loadAlerts(true);
      setInterval(() => { if (this.rq.job && !this.rq.report) this.tick = Date.now(); }, 1000);
      // warm the browser's number and date formatters once, while idle, so the first chart is not the one to pay
      (window.requestIdleCallback || setTimeout)(() => { try { (1234.5).toLocaleString("en-IN"); new Date().toLocaleDateString("en-IN", { day: "numeric", month: "short" }); window.FinnCharts && FinnCharts.render({ kind: "spark", x: ["a", "b"], series: [{ name: "w", values: [1, 2], as: "line", unit: "" }] }); } catch (e) {} });
      if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
        navigator.serviceWorker.addEventListener("message", (e) => {
          if (e.data?.type === "navigate" && e.data.url) location.hash = new URL(e.data.url).hash || "#alerts";
        });
      }
    },

    // until the flags arrive, assume only the features that ship off are off (no flash of a hidden feature)
    flag(name) { return this.featureFlags ? !!this.featureFlags[name] : !["audio_brief", "web_push"].includes(name); },

    // ---------- plumbing ----------
    async api(path, opts = {}) {
      const t = setTimeout(() => { if (!this.warm) this.net.waking = true; }, WAKING_MS);   // cold start only
      try {
        const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
        if (!r.ok) {
          let msg = `${r.status}`;
          try { msg = (await r.json()).detail || msg; } catch (e) {}
          throw new Error(msg);
        }
        this.net.error = null; this.net.stale = false; this.warm = true;
        return await r.json();
      } catch (e) {
        this.net.error = e.message || "Network error";
        throw e;
      } finally { clearTimeout(t); this.net.waking = false; }
    },
    say(msg, undo = null) {
      this.toast = msg; this.toastUndo = undo;
      clearTimeout(this._t); this._t = setTimeout(() => { this.toast = ""; this.toastUndo = null; }, undo ? 4000 : 2600);
    },
    runUndo() { const u = this.toastUndo; this.toast = ""; this.toastUndo = null; if (u) u(); },
    retry() { this.net.error = null; this.go(); },

    go() {
      const h = (location.hash || "#today").slice(1);
      const [name, ...rest] = h.split("/");
      const param = rest.join("/") || null;
      this.sheet = null; this.pick = false;
      if (name === "card" && param) {
        if (!["today", "gallery", "holding"].includes(this.route)) this.route = "today";
        if (!this.digest) this.loadToday();
        this.openCard(+param); return;
      }
      this.route = ["today", "holdings", "holding", "research", "alerts", "settings", "gallery"].includes(name) ? name : "today";
      this.param = param;
      window.scrollTo(0, 0);
      if (this.route === "today") this.loadToday();
      if (this.route === "holdings") this.loadHoldings();
      if (this.route === "holding" && param) this.loadHolding(param);
      if (this.route === "research") { if (!this.holdings) this.loadHoldings(); if (param) this.openResearch(param); else this.rq.report = null; }
      if (this.route === "alerts") { this.loadAlerts(); if (!this.rules) this.loadRules(); }
      if (this.route === "settings") { this.loadRules(); this.loadSched(); this.loadHoldings(); this.loadState(); this.loadDataHealth(); }
      if (this.route === "gallery") this.loadGallery();
    },
    nav(hash) { location.hash = hash; },
    back() { if (history.length > 1) history.back(); else this.nav("#today"); },
    backable() { return ["holding", "settings", "gallery"].includes(this.route) || (this.route === "research" && this.param); },
    title() {
      return { holdings: "Holdings", holding: this.holding?.symbol || this.param, alerts: "Alerts", settings: "Settings", gallery: "Gallery",
               research: this.param ? (this.rq.report?.symbol || this.param) : "Research" }[this.route] || "";
    },
    eyebrow() {
      if (this.route === "holding" && this.holding) return this.holding.name + (this.holding.sector ? " · " + this.holding.sector : "");
      if (this.route === "research" && this.param) return this.rq.report?.name || "Research";
      if (this.route === "gallery") return "QA";
      return "";
    },
    jump(id) { this.$nextTick(() => document.getElementById(id)?.scrollIntoView({ behavior: reduced() ? "auto" : "smooth", block: "start" })); },

    applyTheme() {
      if (this.theme === "system") document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", this.theme);
      store("finn.theme", this.theme);
    },

    // ---------- Today ----------
    loadToday() { this.loadDigest(); this.loadCatalysts(); this.loadHoldings(); this.loadState(); },
    async loadDigest() {
      const qs = this.win === "custom" ? `from=${this.custom.from}&to=${this.custom.to}` : `window=${this.win}`;
      try { this.digest = await this.api(`/digest?${qs}`); }
      catch (e) { if (this.digest) this.net.stale = true; }
    },
    async loadState() { try { this.appState = await this.api("/api/v2/state"); } catch (e) { this.net.error = null; } },
    windows() { return WINDOWS; },
    winLabel() {
      if (this.win === "custom") return this.custom.from && this.custom.to ? `${this.day(this.custom.from)} – ${this.day(this.custom.to)}` : "Custom";
      return (WINDOWS.find(([w]) => w === this.win) || [0, "24h"])[1];
    },
    setWindow(w) {
      if (w === "custom") { this.sheet = "range"; return; }
      this.win = w; store("finn.window", w); this.digest = null; this.worthAll = false; this.loadDigest();
    },
    today() { return new Date().toLocaleDateString("en-CA", IST); },
    applyCustom() {
      const { from, to } = this.custom;
      if (!from || !to) return this.say("Pick both dates");
      if (from > to) return this.say("The start date is after the end date");
      if ((new Date(to) - new Date(from)) / 864e5 > 90) return this.say("Pick a range of 90 days or less");
      this.win = "custom"; this.sheet = null; this.digest = null; this.loadDigest();
    },
    async loadCatalysts() {
      try { this.catalysts = (await this.api("/api/v2/catalysts?days=7")).events; } catch (e) { this.net.error = null; }
      this.catalystsLoaded = true;
    },
    async markChecked() { await this.api("/api/state/checked", { method: "POST" }); this.say("Marked as your last check"); this.loadState(); },
    dateEyebrow() { return new Date().toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", ...IST }); },
    freshLabel() {
      const h = this.appState?.freshness?.hours_ago;
      if (h == null) return "";
      return "Data updated " + (h < 1 ? `${Math.max(1, Math.round(h * 60))}m` : h < 48 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`) + " ago";
    },
    sourceName(s) {
      return { bse_announcements: "BSE announcements", nse_api_announcements: "NSE announcements", nse_rss_announcements: "NSE RSS feed",
               yfinance_prices: "Prices (Yahoo Finance)", nse_bhavcopy_prices: "Prices (NSE bhavcopy)", bse_events: "BSE calendar",
               nse_events: "NSE event calendar", derived_events: "Events from filings", bse_snapshot: "BSE results summaries" }[s] || s;
    },
    dataHealth: null,
    async loadDataHealth() { try { this.dataHealth = await this.api("/health/data"); } catch (e) { this.net.error = null; } },
    pulseAsOf() { return this.appState?.latest_price_date ? "Day change · close " + this.day(this.appState.latest_price_date) : "Day change"; },
    statusCount(s) { return (this.holdings || []).filter((h) => h.status === s).length; },
    isSnoozed(c) { const u = this.snoozed[c.id]; return !!u && new Date(u) > new Date(); },
    snoozedCount() { return this.digest ? [...this.digest.attention, ...this.digest.worth_a_look].filter((c) => this.isSnoozed(c)).length : 0; },
    clearSnooze() { this.snoozed = {}; store("finn.snooze", {}); },
    snooze(c) {
      const t = new Date(); t.setHours(24, 0, 0, 0);
      if (this.route === "gallery") return this.say("Gallery: snooze not saved");
      this.snoozed = { ...this.snoozed, [c.id]: t.toISOString() }; store("finn.snooze", this.snoozed);
      this.say("Snoozed until tomorrow", () => { const s = { ...this.snoozed }; delete s[c.id]; this.snoozed = s; store("finn.snooze", s); });
    },
    feedAttention() {
      if (this.route === "gallery") return this.galleryPicks().attention;
      return this.digest ? this.digest.attention.filter((c) => !this.isSnoozed(c)) : [];
    },
    feedWorth() {
      if (this.route === "gallery") return this.galleryPicks().worth;
      return this.digest ? this.digest.worth_a_look.filter((c) => !this.isSnoozed(c)) : [];
    },
    caughtUp() {
      if (!this.digest) return false;
      const all = [...this.digest.attention, ...this.digest.worth_a_look];
      return all.length > 0 && all.every((c) => c.reviewed || this.isSnoozed(c));
    },
    nextEvent() {
      const c = this.catalysts[0];
      return c ? `Next: ${c.symbol} ${c.title.toLowerCase()} · ${this.day(c.date)}` : "No dated events in sources for the next 7 days.";
    },
    async review(card, undo = false) {
      if (this.route === "gallery") { card.reviewed = !undo; return this.say("Gallery: not saved"); }
      card.reviewed = !undo;
      try { await this.api(`/api/v2/cards/${card.id}/review${undo ? "?undo=true" : ""}`, { method: "POST" }); }
      catch (e) { card.reviewed = undo; return; }
      if (!undo) this.say("Reviewed", () => this.review(card, true));
      if (this.digest) this.digest.caught_up = this.caughtUp();
    },
    // Swipe right = reviewed (teal reveal), swipe left = snooze (slate reveal), long-press = context menu.
    gesture(el, card) {
      const body = el.querySelector(".card"), rr = el.querySelector(".reveal.r"), rl = el.querySelector(".reveal.l");
      let x0 = null, y0 = 0, dx = 0, horiz = null, moved = false, timer = null;
      const reset = () => { body.style.transform = ""; rr.style.opacity = 0; rl.style.opacity = 0; el.classList.remove("dragging"); };
      el.addEventListener("pointerdown", (e) => {
        if (e.button > 0) return;
        x0 = e.clientX; y0 = e.clientY; dx = 0; horiz = null; moved = false;
        timer = setTimeout(() => { if (!moved) { moved = true; x0 = null; this.openMenu(card); } }, 550);
      });
      el.addEventListener("pointermove", (e) => {
        if (x0 === null) return;
        dx = e.clientX - x0; const dy = e.clientY - y0;
        if (Math.abs(dx) > 8 || Math.abs(dy) > 8) { moved = true; clearTimeout(timer); }
        if (horiz === null && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) horiz = Math.abs(dx) > Math.abs(dy);
        if (!horiz) return;
        el.classList.add("dragging");
        const x = Math.max(-140, Math.min(140, dx));
        body.style.transform = `translateX(${x}px)`;
        rr.style.opacity = x > 0 ? Math.min(1, x / 90) : 0; rl.style.opacity = x < 0 ? Math.min(1, -x / 90) : 0;
      });
      const end = () => {
        clearTimeout(timer);
        if (x0 !== null && horiz) {
          if (dx > 90) this.review(card, card.reviewed);
          else if (dx < -90) this.snooze(card);
        }
        x0 = null; reset();
      };
      el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end); el.addEventListener("pointerleave", end);
      el.addEventListener("click", (e) => { if (moved) { e.preventDefault(); e.stopPropagation(); moved = false; } }, true);
    },
    openMenu(c) { if (c) { this.menuCard = c; this.prevSheet = null; this.sheet = "menu"; } },
    openWhy(c) {
      if (!c) return;
      this.prevSheet = this.sheet === "card" ? "card" : null; this.whyCard = c; this.sheet = "why";
    },
    whyFactors() {
      const w = this.whyCard?.why || {};
      return [["Type weight", w.type_weight], ["Magnitude", w.magnitude], ["Novelty", w.novelty], ["Thesis multiplier", w.thesis_multiplier]]
        .filter((f) => f[1] != null);
    },
    bucketName(b) { return { attention: "Needs attention (≥ 0.70)", worth_a_look: "Worth a look (≥ 0.30)" }[b] || "Routine (< 0.30)"; },

    // ---------- card helpers ----------
    type(t) { return TYPE_LABEL[t] || t; },
    impact(c) { return IMPACT_LABEL[c?.thesis_impact] || ""; },
    flags(c) { return (c?.payload?.flags || c?.payload?.governance_flags || []).slice().sort((a, b) => (b.severity === "high") - (a.severity === "high")); },
    isGov(c) { return c.type === "governance" || (c.type !== "mgmt_change" && this.flags(c).some((f) => f.severity === "high")); },
    lowConf(c) { return !!(c.payload?.ok && ((c.payload.notes || []).length || c.payload.confidence === "low")); },
    firstCmp(c) { return c.payload?.results_day?.comparisons?.[0] || null; },
    cmpCls(v) { return /on track|deliver|beat|ahead|met/i.test(v || "") ? "delivered" : /miss|below|behind/i.test(v || "") ? "missed" : "partial"; },
    resultRows(c) { return (c.payload?.table || []).filter((r) => r.found); },
    resultsNote(c) {
      if (c.payload.from_bse_summary) return "BSE's own figures · ₹ crore · the filing PDF could not be read";
      const b = c.payload.basis ? c.payload.basis[0].toUpperCase() + c.payload.basis.slice(1) : "Basis not stated";
      const checked = (c.payload.bse_check || {}).match || (c.payload.bse_check || {}).explained_by_basis ? " · checked against BSE" : "";
      return `${b} · ₹ crore · computed in code${checked}` + (this.resultRows(c).some((r) => r.derived) ? " · *derived" : "");
    },
    isIn(p) { return /appoint|join|induct|elevat|promot|designat|inducted/i.test(p.direction || ""); },
    initials(n) {
      return (n || "").replace(/^(dr|mr|mrs|ms|shri|smt|prof)\.?\s+/i, "").split(/\s+/).filter((w) => /[a-z]/i.test(w))
        .map((w) => w[0]).slice(0, 2).join("").toUpperCase();
    },
    // Lead with what happened: structured cards keep FINN's computed headline; others use the filing's extracted
    // one-liner instead of the raw exchange subject line, unless that one-liner is boilerplate.
    boiler(t) { return /^the (company|exchange|board)\b.*\b(submitted|informed|intimated|regulation|in accordance)/i.test(t || ""); },
    headline(c) {
      const structured = ["results", "mgmt_change"].includes(c.type) || c.payload?.people?.length;
      return !structured && c.one_liner && !this.boiler(c.one_liner) ? c.one_liner : c.headline;
    },
    showOneLiner(c) {
      if (!c.one_liner || this.headline(c) === c.one_liner || this.boiler(c.one_liner) || c.type === "results") return false;
      return !(c.payload?.people?.length) && !this.flags(c).length;
    },
    clip(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s; },

    // ---------- card sheet + evidence ----------
    openCardSheet(c) {
      // open at once with what the feed already has; the full detail (people, flags, document) fills in after
      this.cardDetail = c; this.sheet = "card"; this.evidence = null;
      if (c.id > 0) this.openCard(c.id, true);
    },
    async openCard(id, quiet = false) {
      if (!quiet) { this.sheet = "card"; this.cardDetail = null; this.evidence = null; }
      try {
        const d = await this.api(`/api/v2/cards/${id}`);
        if (this.sheet === "card" && (!this.cardDetail || this.cardDetail.id === id)) this.cardDetail = d;
      } catch (e) { this.net.error = null; if (!quiet) { this.sheet = null; this.say("Card not found"); } }
    },
    closeSheet() {
      this.sheet = null; this.evidence = null;
      if (location.hash.startsWith("#card/")) history.replaceState(null, "", "#today");
    },
    docOf(c) { return c?.document_id || c?.payload?.document_id || null; },   // feed cards carry it in the payload
    async openEvidence(docId, page, quote) {
      if (!docId || !page) return this.say("That page isn't stored for this source.");
      if (this.sheet && this.sheet !== "evidence") this.prevSheet = this.sheet;
      this.sheet = "evidence"; this.evidence = { loading: true, quote };
      try { this.evidence = { ...(await this.api(`/api/v2/documents/${docId}/pages/${page}`)), quote }; }
      catch (e) { this.net.error = null; this.evidence = { error: "That page isn't stored yet. Open the PDF instead." }; }
    },
    norm(s) { return String(s || "").toLowerCase().replace(/[^a-z0-9%.]+/g, " ").trim(); },
    quoteOnPage() {
      const q = this.norm(this.evidence?.quote).slice(0, 60);
      return !!q && this.norm(this.evidence?.text).includes(q);
    },
    highlight(text, quote) {
      let out = esc(text);
      const words = String(quote || "").split(/\s+/).filter(Boolean).slice(0, 10).map((w) => esc(w).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
      for (let n = words.length; n >= 3; n--) {          // longest run of the quote's opening words found on the page
        const re = new RegExp(words.slice(0, n).join("\\s+"), "i");
        if (re.test(out)) return out.replace(re, (m) => `<mark>${m}</mark>`);
      }
      return out;
    },

    // ---------- Holdings ----------
    loadLogos() { fetch("/api/v2/logos").then((r) => (r.ok ? r.json() : {})).then((d) => { this.logos = d.files || {}; }).catch(() => {}); },
    async loadHoldings() { try { this.holdings = (await this.api("/api/v2/holdings")).holdings; } catch (e) {} },
    async loadHolding(sym) {
      this.holding = null; this.editing = false; this.hc = null;
      if (this.flag("charts")) this.api(`/api/v2/charts/holding/${sym}`).then((c) => { if (this.param === sym) this.hc = c; }).catch(() => { this.net.error = null; });
      else this.hc = { kpis: [] };
      try { this.holding = await this.api(`/api/v2/holdings/${sym}`); this.loadJournal(sym); } catch (e) {}
    },
    holdingStatus() { return this.holding?.timeline?.[0]?.status || "intact"; },
    lastPrice() { return this.holding?.prices?.at(-1)?.close ?? null; },
    holdingDay() {
      const p = this.holding?.prices || [];
      return p.length > 1 && p.at(-2).close ? +(100 * (p.at(-1).close / p.at(-2).close - 1)).toFixed(2) : null;
    },
    priceRange() {
      const s = this.hc?.price?.x, p = this.holding?.prices || [];
      if (s?.length) return `${this.dm(s[0])} – ${this.dm(s.at(-1))}`;
      return p.length ? `${this.dm(p[0].date)} – ${this.dm(p.at(-1).date)}` : "";
    },
    kpi(m) { return KPI_LABEL[m] || m; },
    kpiRow(k) { return (this.holding?.kpi_check?.rows || []).find((r) => r.metric === k.metric && r.op === k.op && r.threshold === Number(k.threshold)) || null; },
    kpiBar(k) {
      const r = this.kpiRow(k); if (!r) return { v: 0, t: 0 };
      const max = Math.max(Math.abs(r.value), Math.abs(r.threshold), 1) * 1.25;
      return { v: Math.max(0, Math.min(100, (r.value / max) * 100)), t: Math.max(0, Math.min(100, (r.threshold / max) * 100)) };
    },
    startEdit() {
      const t = this.holding.thesis || { statement: "", kpis: [], breakers: [] };
      this.draft = { statement: t.statement, breakers: (t.breakers || []).map((b) => b.text).join("\n"),
                     kpis: JSON.parse(JSON.stringify(t.kpis || [])) };
      this.editing = true;
    },
    addKpi() { this.draft.kpis.push({ metric: "ebitda_margin", op: ">=", threshold: 10, why: "" }); },
    async saveThesis() {
      const body = { statement: this.draft.statement, breakers: this.draft.breakers.split("\n"),
                     kpis: this.draft.kpis.map((k) => ({ ...k, threshold: Number(k.threshold) })) };
      try {
        await this.api(`/api/v2/holdings/${this.holding.symbol}/thesis`, { method: "PUT", body: JSON.stringify(body) });
        this.say("Thesis saved"); this.loadHolding(this.holding.symbol);
      } catch (e) { this.say(this.net.error); this.net.error = null; }
    },

    // ---------- Research ----------
    startResearch() {
      const t = (this.rq.ticker || "").trim().toUpperCase();
      if (!/^[A-Z0-9&-]{1,20}$/.test(t)) return this.say("Enter an NSE symbol, e.g. POLYCAB");
      this.nav(`#research/${t}`);
    },
    async openResearch(sym, force = false) {
      Object.assign(this.rq, { ticker: sym, job: null, report: null, stage: null, progress: 0, stages: [], error: null, open: {}, st: {}, dur: {}, t0: Date.now() });
      this.rtab = "ledger"; this.rc = null; Object.assign(this.deep, { recipe: null, data: null, error: null });
      this.recent = [sym, ...this.recent.filter((s) => s !== sym)].slice(0, 5); store("finn.recent", this.recent);
      try {
        const r = await this.api(`/api/v2/research/${sym}${force ? "?force=true" : ""}`, { method: "POST" });
        if (r.cached) { this.rq.report = r.report; this.loadResearchCharts(sym); return; }
        this.rq.job = r.job_id; this.follow(r.job_id);
      } catch (e) { this.rq.error = this.net.error; this.net.error = null; }
    },
    onStage(d) {
      const now = Date.now();
      if (d.stage && d.stage !== this.rq.stage) {
        if (this.rq.stage && this.rq.st[this.rq.stage]) this.rq.dur[this.rq.stage] = (now - this.rq.st[this.rq.stage]) / 1000;
        this.rq.st[d.stage] = now;
      }
      Object.assign(this.rq, { stage: d.stage, progress: d.progress, stages: d.stages || this.rq.stages });
    },
    follow(jobId) {
      const done = async () => {
        try { const j = await this.api(`/api/v2/research/job/${jobId}`); if (j.report) { this.rq.report = j.report; this.loadResearchCharts(j.report.symbol); } else this.rq.error = j.error; }
        catch (e) { this.rq.error = this.net.error; this.net.error = null; }
      };
      if (!window.EventSource) return this.poll(jobId);
      const es = new EventSource(`/api/v2/research/job/${jobId}/events`);
      es.addEventListener("stage", (e) => this.onStage(JSON.parse(e.data)));
      es.addEventListener("done", () => { es.close(); done(); });
      es.addEventListener("failed", (e) => { es.close(); this.rq.error = JSON.parse(e.data).error || "Research failed"; });
      es.onerror = () => { es.close(); this.poll(jobId); };
    },
    async poll(jobId) {
      for (let i = 0; i < 400; i++) {
        let j; try { j = await this.api(`/api/v2/research/job/${jobId}`); } catch (e) { return; }
        this.onStage(j);
        if (j.status === "done") { this.rq.report = j.report; return; }
        if (j.status === "failed") { this.rq.error = j.error; return; }
        await new Promise((r) => setTimeout(r, 1500));
      }
    },
    stageState(s) {
      const i = this.rq.stages.indexOf(s), cur = this.rq.stages.indexOf(this.rq.stage);
      if (!this.rq.stages.length) return "now";
      return i < cur ? "done" : i === cur ? "now" : "";
    },
    stageTime(s) {
      if (this.rq.dur[s] != null) return this.rq.dur[s].toFixed(1) + " s";
      if (s === this.rq.stage && this.rq.st[s]) return Math.max(0, Math.round((this.tick - this.rq.st[s]) / 1000)) + " s";
      return "";
    },
    rqElapsed() { return this.rq.t0 ? Math.max(0, Math.round((this.tick - this.rq.t0) / 1000)) + " s" : ""; },
    source(id) { return (this.rq.report?.sources || []).find((s) => s.id === id) || {}; },
    verdict(v) { return VERDICT_LABEL[v] || v; },
    verdictCounts() { return Object.entries(this.rq.report?.credibility?.counts || {}).filter(([, n]) => n > 0); },
    credLine() {
      const c = this.rq.report.credibility, vg = c.vagueness_pct != null ? ` · vagueness ${c.vagueness_pct}%` : "";
      return c.score != null
        ? `credibility out of 100<br><span style="color:var(--text)">${c.measured} of ${c.total} promises matured</span>${vg}`
        : `needs ${c.min_measured} matured promises to score<br><span style="color:var(--text)">${c.measured} of ${c.total} promises matured so far</span>`;
    },
    strongEvidence(l) { return !!(l.actual || (l.also_said_in && l.also_said_in.length)); },
    actualLine(l) {
      const u = (x) => (x.unit && x.unit !== "%" ? " " + x.unit : x.unit === "%" ? "%" : "");
      if (l.actual) return `Actual ${l.actual.value}${u(l.actual)} (${l.actual.period})`;
      if (l.latest) return `Latest ${l.latest.value}${u(l.latest)} (${l.latest.period})`;
      if (l.verdict === "unverifiable") return "Too vague to check";
      return l.horizon ? `Not yet due: ${l.horizon}` : "Not in sources";
    },
    segs() { return (this.rq.report?.snapshot?.segments || []).filter((s) => s.share_pct > 0).sort((a, b) => b.share_pct - a.share_pct); },
    refOpen(id) {
      const r = this.rq.report || {};
      const x = [...(r.ledger || []), ...(Array.isArray(r.actuals) ? r.actuals : []), ...(r.snapshot?.segments || []), ...(r.snapshot?.risks || [])]
        .find((i) => i.id === id);
      if (!x) return this.say("That source isn't in this report.");
      this.openEvidence(this.source(x.doc).document_id || x.doc_id, x.page, x.quote);
    },
    receiptLine() {
      const r = this.rq.report.receipt;
      return `Receipt · ${r.sources_read} sources · ${r.seconds} s · $${r.cost_usd} · ${r.unverified_claims_shown} unverified claims · ${this.when(r.generated_at)}`;
    },

    // ---------- deep dives ----------
    deep: { recipe: null, data: null, loading: false, error: null },
    async loadResearchCharts(sym) {
      this.rc = null;
      if (!this.flag("charts")) return;
      try { this.rc = await this.api(`/api/v2/charts/research/${sym}`); } catch (e) { this.net.error = null; }
    },
    async openDeep(recipe) {
      Object.assign(this.deep, { recipe, data: null, loading: true, error: null });
      if (recipe === "results") {     // results history: the same verified quarter series as the holding page
        try {
          const t0 = performance.now(), c = await this.api(`/api/v2/charts/holding/${this.param}`);
          this.deep.data = { recipe, charts: [c.results, c.bridge].filter(Boolean), finding: c.results?.title,
                             receipt: { llm_calls: 0, seconds: +((performance.now() - t0) / 1000).toFixed(1) },
                             method: "from FINN's verified results cards, computed in Python" };
        } catch (e) { this.deep.error = this.net.error; this.net.error = null; }
        finally { this.deep.loading = false; }
        return;
      }
      try { this.deep.data = await this.api(`/api/v2/deep/${this.param}/${recipe}`); }
      catch (e) { this.deep.error = this.net.error; this.net.error = null; }
      finally { this.deep.loading = false; }
    },
    deepName(r) { return (DEEP.find(([k]) => k === r) || [0, r])[1]; },
    deepFinding() {
      const d = this.deep.data, c = d.current || {};
      if (d.unavailable) return "";
      switch (d.recipe) {
        case "results": return d.finding || "No verified results figures yet";
        case "cash_quality": return `Free cash flow ${this.cr(c.fcf)}` + (c.ocf_to_pat != null ? ` · OCF/PAT ${c.ocf_to_pat}` : "") + ` (${d.report})`;
        case "balance_sheet": return c.net_debt == null ? "Net debt: not in sources" : (c.net_debt < 0 ? `Net cash ${this.cr(-c.net_debt)}` : `Net debt ${this.cr(c.net_debt)}`) + (c.interest_cover != null ? ` · interest cover ${c.interest_cover}×` : "");
        case "hedge_meter": return d.latest_vs_earlier_pct == null ? "Not enough calls to compare" : `Cautious language ${this.pct(d.latest_vs_earlier_pct)} on the latest call vs the earlier average`;
        case "dodge_detector": { const l = d.calls?.[0]; return l ? `${l.call}: ${l.deflected} of ${l.judged} answers deflected (${l.deflection_rate_pct}%)` : "No question-answer pairs found"; }
        case "worry_map": return d.rising?.length ? `Rising on the latest call: ${d.rising.join(", ")}` : `${d.questions} analyst questions; no topic rising`;
        case "language_diff": return `${d.new.length} new statements, ${d.dropped.length} dropped (${d.older} → ${d.newer})`;
        case "fine_print": { const x = d.contingent_liabilities_cr; return x?.change_pct != null ? `Contingent liabilities ${this.pct(x.change_pct)} year on year` : "Contingent liabilities: change not in sources"; }
        case "peer_compare": return `Promise ledgers side by side with ${d.peer || "the research peer"}`;
      }
      return "";
    },
    deepRows(obj) { return Object.entries(obj || {}).filter(([, v]) => typeof v === "number" || v === null); },
    label(k) { return k.replace(/_/g, " ").replace(/\bocf\b/i, "OCF").replace(/\bfcf\b/i, "FCF").replace(/\bpat\b/i, "PAT").replace(/\bebitda\b/i, "EBITDA"); },
    fmt(v) { return v == null ? "Not in sources" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }); },

    // ---------- replay, audio brief, journal, feedback ----------
    rp: { daysAgo: 14, data: null, loading: false },
    replayDate() { return new Date(Date.now() - this.rp.daysAgo * 864e5).toISOString().slice(0, 10); },
    async loadReplay() {
      this.rp.loading = true; this.rp.data = null;
      try { this.rp.data = await this.api(`/api/v2/replay?as_of=${this.replayDate()}&window=7d`); }
      catch (e) { this.say(this.net.error); this.net.error = null; }
      finally { this.rp.loading = false; }
    },
    speaking: false,
    briefText() {
      if (!this.digest) return "";
      const lines = [`FINN brief. ${this.digest.window.label}.`];
      const att = this.digest.attention.filter((c) => !c.reviewed).slice(0, 5);
      if (!att.length) lines.push("Nothing needs your attention.");
      att.forEach((c) => lines.push(`${c.company || c.symbol}. ${c.headline}. ${c.why_it_matters || ""}`));
      if (this.digest.worth_a_look.length) lines.push(`${this.digest.worth_a_look.length} more worth a look.`);
      let words = 0; const out = [];
      for (const l of lines) { words += l.split(/\s+/).length; if (words > 160) break; out.push(l); }   // ~60 s at 160 wpm
      return out.join(" ").replace(/₹/g, "rupees ").replace(/\bcr\b/g, "crore").replace(/\bYoY\b/g, "year on year").replace(/\bQoQ\b/g, "quarter on quarter");
    },
    toggleBrief() {
      if (!("speechSynthesis" in window)) return this.say("This browser can't read aloud.");
      if (this.speaking) { speechSynthesis.cancel(); this.speaking = false; return; }
      if (!this.digest) return this.say("The feed is still loading.");
      const u = new SpeechSynthesisUtterance(this.briefText());
      u.lang = "en-IN"; u.rate = 1.0; u.onend = () => (this.speaking = false);
      speechSynthesis.speak(u); this.speaking = true;
    },
    jr: { action: "Acting on this", reason: "" },
    async saveJournal(card) {
      try {
        await this.api("/api/v2/journal", { method: "POST", body: JSON.stringify({ symbol: card.symbol, filing_id: card.id, action: this.jr.action, reason: this.jr.reason }) });
        this.jr.reason = ""; this.say("Saved to your journal");
      } catch (e) { this.say(this.net.error); this.net.error = null; }
    },
    journal: [],
    async loadJournal(sym) { try { this.journal = (await this.api(`/api/v2/journal?symbol=${sym}`)).entries; } catch (e) {} },
    async sendFeedback(card, signal) {
      if (this.route === "gallery") return this.say("Gallery: feedback not saved");
      try {
        const r = await this.api(`/api/v2/cards/${card.id}/feedback`, { method: "POST", body: JSON.stringify({ signal }) });
        this.say(`Thanks. ${this.type(r.type)} weight now ${r.adjustment >= 0 ? "+" : ""}${r.adjustment} (bounded ±${r.bound})`);
      } catch (e) { this.say(this.net.error); this.net.error = null; }
    },

    // ---------- copilot ----------
    cp: { q: "", steps: [], answer: null, busy: false, error: null },
    copilotSymbol() { return this.route === "holding" ? this.holding?.symbol || this.param : this.route === "research" ? this.param : null; },
    copilotContext() {
      const s = this.copilotSymbol();
      return s ? `${s} · ${this.route === "holding" ? "holding" : "research report"}` : "Your portfolio";
    },
    suggestions() {
      const s = this.copilotSymbol();
      return s ? [`Why did ${s}'s margins move?`, `What did ${s} promise on capex?`, `Has ${s}'s CFO changed before?`, `How credible has ${s}'s management been?`]
               : ["Which holdings are At risk and why?", "Which holdings reported results this month?", "Any governance red flags in my portfolio?"];
    },
    openCopilot(q) {
      Object.assign(this.cp, { steps: [], answer: null, error: null });
      this.sheet = "copilot"; if (q) this.askCopilot(q);
    },
    askCopilot(q) {
      q = (q || this.cp.q || "").trim();
      if (q.length < 3 || this.cp.busy) return;
      Object.assign(this.cp, { q, steps: [], answer: null, busy: true, error: null });
      const sym = this.copilotSymbol();
      const es = new EventSource(`/api/v2/copilot?q=${encodeURIComponent(q)}${sym ? "&symbol=" + sym : ""}`);
      es.addEventListener("step", (e) => this.cp.steps.push(JSON.parse(e.data)));
      es.addEventListener("note", (e) => this.cp.steps.push({ note: JSON.parse(e.data).text }));
      es.addEventListener("answer", (e) => { this.cp.answer = JSON.parse(e.data); this.cp.busy = false; es.close(); });
      es.addEventListener("error", (e) => {
        try { this.cp.error = JSON.parse(e.data).error; } catch (_) { this.cp.error = "The assistant didn't answer. Try again."; }
        this.cp.busy = false; es.close();
      });
    },
    toolLabel(t) {
      return { search_docs: "Searching documents", get_page: "Reading a page", get_metric: "Looking up figures",
               get_filings: "Checking filings", get_thesis: "Reading your thesis", get_ledger: "Reading the promise ledger" }[t] || t;
    },
    citeOpen(id) {
      const e = this.cp.answer?.evidence?.find((x) => x.id === id);
      if (!e) return;
      if (e.document_id && e.page) this.openEvidence(e.document_id, e.page, e.text.replace(/^\[[^\]]*\]\s*/, "").slice(0, 160));
      else if (e.filing_id) this.openCard(e.filing_id);
      else if (e.url) window.open(e.url, "_blank", "noopener");
    },

    // ---------- alerts ----------
    async loadAlerts(quiet = false) {
      try { const r = await this.api("/api/v2/alerts"); this.alerts = r.alerts; this.unread = r.unread; this.unreadOlder = r.unread_older || 0; }
      catch (e) { if (quiet) this.net.error = null; }
    },
    alertGroups() {
      const g = [];
      for (const a of (this.alerts || []).slice(0, this.alertsAll ? 9999 : 30)) {
        let grp = g.find((x) => x.day === a.date);
        if (!grp) g.push((grp = { day: a.date, label: this.dayLabel(a.date), items: [] }));
        grp.items.push(a);
      }
      return g;
    },
    alertIcon(a) {
      const t = (a.title || "").split(": ").slice(1).join(": "), k = a.kind || "";
      if (k === "volume_spike") return this.ic("activity");
      if (k === "price_move") return this.ic(/ down /.test(" " + t + " ") ? "trending-down" : "trending-up");
      if (k === "peer_read_across") return this.ic("git-compare");
      if (k.includes("promise")) return this.ic("flag");
      if (/insolven/i.test(t)) return this.ic("alert-triangle");
      return this.ic({ Results: "bar-chart-3", "Management change": "user-round", "Mgmt change": "user-round",
                       Governance: "scale", "M&A": "handshake" }[t] || "file-text");
    },
    ic(n) { return "/static/icons.svg#" + n; },
    statusIcon(s) { return this.ic({ intact: "shield-check", watch: "eye", at_risk: "shield-alert" }[s] || "shield-check"); },
    verdictIcon(v) { return this.ic({ delivered: "check", partial: "minus", missed: "x", pending: "clock", unverifiable: "help-circle" }[v] || "help-circle"); },
    // company logos: a bundled file from static/logos/ when one exists, else a monogram tile
    mono(sym) { return String(sym || "?").replace(/[^A-Z0-9]/gi, "").slice(0, 2).toUpperCase() || "?"; },
    logoSrc(sym) { return (this.logos || {})[String(sym || "").toUpperCase()] || null; },
    logoStyle(sym) {
      if (this.logoSrc(sym)) return "background:#F2F4F3";
      let x = 0; for (const ch of String(sym || "")) x = (x * 31 + ch.charCodeAt(0)) >>> 0;
      return `background:hsl(${x % 360} 30% 30%);color:#fff`;
    },
    nameOf(sym) { return (this.holdings || []).find((h) => h.symbol === sym)?.name || sym; },
    // 30-day sparkline path; nothing is drawn below 5 points
    sparkPath(v, w, hgt) {
      v = (v || []).filter((x) => x != null); if (v.length < 5) return "";
      const lo = Math.min(...v), hi = Math.max(...v), r = hi - lo || 1;
      return v.map((y, i) => (i ? "L" : "M") + (i * (w - 2) / (v.length - 1) + 1).toFixed(1) + " " + (hgt - 1 - (y - lo) / r * (hgt - 2)).toFixed(1)).join(" ");
    },
    sparkCls(v) { v = v || []; return v.length && v.at(-1) >= v[0] ? "up" : "down"; },
    sparkLabel(sym, v) { v = v || []; if (v.length < 5) return ""; const c = (v.at(-1) / v[0] - 1) * 100;
      return `${sym} 30-day price trend: ${c >= 0 ? "up" : "down"} ${Math.abs(c).toFixed(1)}%`; },
    // tiny QoQ / YoY bars, scaled to the largest change on the same card (decoration; the number stays as text)
    dbar(c, v) {
      if (v == null) return "display:none";
      const m = Math.max(1, ...this.resultRows(c).flatMap((r) => [Math.abs(r.qoq || 0), Math.abs(r.yoy || 0)]));
      return `width:${Math.max(2, Math.round(16 * Math.abs(v) / m))}px`;
    },
    async openAlert(a) {
      if (!a.read) {
        a.read = true;
        const recent = (Date.now() - new Date(a.date)) / 864e5 <= 7;
        if (recent) this.unread = Math.max(0, this.unread - 1); else this.unreadOlder = Math.max(0, this.unreadOlder - 1);
        this.api(`/api/v2/alerts/${a.id}/read`, { method: "POST" }).catch(() => {});
      }
      if (a.link) location.hash = a.link.replace(/^\/?#?/, "#");
    },
    async toggleAlertChart(a) {
      if (this.ac[a.id]) { const c = { ...this.ac }; delete c[a.id]; this.ac = c; return; }
      const id = a.kind === "volume_spike" ? a.id : (a.also || []).find((x) => x.kind === "volume_spike")?.alert_id;
      try {
        const r = await this.api(`/api/v2/charts/alert/${id}`);
        if (r.volume) this.ac = { ...this.ac, [a.id]: r.volume }; else this.say("Not enough trading days stored for this chart.");
      } catch (e) { this.net.error = null; this.say("Chart unavailable right now."); }
    },
    async readAll() { await this.api("/api/v2/alerts/read_all", { method: "POST" }); (this.alerts || []).forEach((a) => (a.read = true)); this.unread = 0; this.unreadOlder = 0; },
    ruleChip(r) {
      const p = r.params, off = r.enabled ? "" : " · off";
      if (p.multiple !== undefined) return `Volume ≥ ${Number(p.multiple).toFixed(1)}× avg` + off;
      if (p.pct !== undefined) return `Price ±${Number(p.pct).toFixed(1)}%` + off;
      if (r.kind === "announcement_type") return `Types: ${p.types.slice(0, 3).map((t) => this.type(t)).join(", ")}${p.types.length > 3 ? " +" + (p.types.length - 3) : ""}` + off;
      if (r.kind === "new_promise") return "New or dropped promises" + off;
      if (r.kind === "peer_read_across") return "Peer filings" + off;
      return r.label + off;
    },
    rulesLine() {
      const v = this.rules?.find((r) => r.kind === "volume_spike"), p = this.rules?.find((r) => r.kind === "price_move");
      return v && p ? `Your thresholds: volume ${Number(v.params.multiple).toFixed(1)}× the 20-day average, price ±${Number(p.params.pct).toFixed(1)}%. Change them in Settings.`
                    : "Set thresholds in Settings.";
    },
    pushBanner() {
      if (this.push.state === "on" || !this.flag("web_push")) return false;
      if (this.isIOS && !this.standalone) return true;
      return this.push.supported && typeof Notification !== "undefined" && Notification.permission !== "granted";
    },

    // ---------- settings ----------
    async addHolding() {
      const sym = (this.pf.symbol || "").trim().toUpperCase();
      if (!/^[A-Z0-9&-]{1,20}$/.test(sym)) return this.say("Enter an NSE symbol, e.g. HAVELLS");
      if ((this.holdings || []).some((h) => h.symbol === sym)) return this.say(`${sym} is already a holding`);
      this.pf.busy = true;
      try {
        this.pf.job = await this.api("/api/v2/portfolio", { method: "POST", body: JSON.stringify({ symbol: sym }) });
        this.pf.symbol = "";
        this.loadHoldings();
        this.pollHolding(sym);
      } catch (e) { this.say(this.net.error); this.net.error = null; }
      finally { this.pf.busy = false; }
    },
    pollHolding(sym) {
      clearInterval(this._pfT);
      this._pfT = setInterval(async () => {
        try { this.pf.job = await this.api(`/api/v2/portfolio/jobs/${sym}`); } catch (e) { this.net.error = null; }
        if (!this.pf.job || this.pf.job.done) { clearInterval(this._pfT); this.loadHoldings(); }
      }, 3000);
    },
    pfStage(i) {
      const j = this.pf.job; if (!j) return "";
      const cur = j.stages.indexOf(j.stage);
      if (j.done && !j.error) return "done";
      return i < cur ? "done" : i === cur ? (j.error ? "" : "now") : "";
    },
    async removeHolding(h) {
      if (!confirm(`Remove ${h.symbol} from the portfolio? Its history is kept; adding it again restores it.`)) return;
      const before = this.holdings;
      if (this.pf.job?.symbol === h.symbol) {        // stop its add-job poller (it would re-list it) and close the card
        clearInterval(this._pfT);
        if (!this.pf.job.done) this.pf.job = { ...this.pf.job, done: true, error: "Removed while it was being added; onboarding stopped" };
      }
      this.holdings = (this.holdings || []).filter((x) => x.symbol !== h.symbol);     // optimistic
      try {
        await this.api(`/api/v2/portfolio/${h.symbol}`, { method: "DELETE" });
        this.say(`${h.symbol} removed`, () => { this.pf.symbol = h.symbol; this.addHolding(); });
      } catch (e) { this.holdings = before; this.say(`Couldn't remove ${h.symbol}: ${this.net.error}`); this.net.error = null; }
    },
    async loadRules() { try { this.rules = (await this.api("/api/v2/rules")).rules; } catch (e) {} },
    async saveRule(r) {
      try { await this.api(`/api/v2/rules/${r.kind}`, { method: "PUT", body: JSON.stringify({ params: r.params, enabled: r.enabled }) }); this.say("Rule saved"); }
      catch (e) { this.say(this.net.error); this.net.error = null; this.loadRules(); }
    },
    toggleType(r, t) {
      const i = r.params.types.indexOf(t);
      if (i >= 0) r.params.types.splice(i, 1); else r.params.types.push(t);
      this.saveRule(r);
    },
    async enablePush() {
      if (!this.push.supported) return (this.push.msg = "This browser can't receive web push.");
      if (this.isIOS && !this.standalone) return (this.push.msg = "On iPhone, add FINN to your Home Screen first, then enable notifications from there.");
      const perm = await Notification.requestPermission();
      if (perm !== "granted") return (this.push.msg = "Notifications are blocked for this site.");
      const { key, configured } = await this.api("/api/v2/push/key");
      if (!configured) return (this.push.msg = "The server has no VAPID keys configured.");
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
      await this.api("/api/v2/push/subscribe", { method: "POST", body: JSON.stringify(sub.toJSON()) });
      this.push.state = "on"; this.push.msg = "Notifications are on."; this.say("Notifications are on");
    },
    async testPush() {
      try { const r = await this.api("/api/v2/push/test", { method: "POST" });
            this.push.msg = r.sent ? `Sent to ${r.sent} device${r.sent > 1 ? "s" : ""}.` : (r.skipped || "No subscribed device yet."); }
      catch (e) { this.push.msg = this.net.error; this.net.error = null; }
    },
    sched: null,
    async loadSched() { try { this.sched = await this.api("/api/v2/scheduler"); } catch (e) {} },
    async refreshNow() {
      const r = await this.api("/api/v2/refresh", { method: "POST" });
      this.say(r.started ? "Refreshing filings, prices and alerts…" : r.reason);
      await this.loadSched();
      const t = setInterval(async () => {
        await this.loadSched();
        if (this.sched && !this.sched.running) {
          clearInterval(t); this.say("Refresh done"); this.loadAlerts(); this.loadState(); this.loadDataHealth();
          if (this.route === "today") { this.digest = null; this.loadDigest(); }
        }
      }, 4000);
    },
    async runPipeline() {
      this.say("Checking for new alerts…");
      try { await this.api("/api/v2/alerts/evaluate", { method: "POST" }); await this.loadAlerts(); this.say(`Alerts checked · ${this.unread} unread`); }
      catch (e) { this.say(this.net.error); this.net.error = null; }
    },

    // ---------- gallery: real cards, one per variant ----------
    galleryCharts() {
      const g = this.gc;
      if (!g) return [];
      return [g.holding?.results, g.holding?.bridge, ...(g.holding?.kpis || []), g.holding?.price, g.research?.hit_rate,
              g.research?.drift, ...(g.deep || []), g.volume].filter(Boolean);
    },
    chartMs() { return (window.__finnChartMs || 0).toFixed(1); },
    async loadGalleryCharts(sym = "POLYCAB") {
      const get = (u) => this.api(u).catch(() => { this.net.error = null; return null; });
      const [holding, research, cash, worry, hedge] = await Promise.all([
        get(`/api/v2/charts/holding/${sym}`), get(`/api/v2/charts/research/${sym}`), get(`/api/v2/deep/${sym}/cash_quality`),
        get(`/api/v2/deep/${sym}/worry_map`), get(`/api/v2/deep/${sym}/hedge_meter`)]);
      const vol = (this.alerts || []).find((a) => a.kind === "volume_spike");
      const volume = vol ? (await get(`/api/v2/charts/alert/${vol.id}`))?.volume : null;
      this.gc = { symbol: sym, holding, research, volume,
                  deep: [cash, worry, hedge].flatMap((d) => (d && d.charts) || []) };
    },
    async loadGallery() {
      this.loadCatalysts(); if (!this.rules) this.loadRules();
      if (!this.alerts) await this.loadAlerts(true);
      if (!this.gc) this.loadGalleryCharts();
      if (!this.gal.digest) { try { this.gal.digest = await this.api("/digest?window=30d"); } catch (e) {} }
    },
    galleryPicks() {
      const d = this.gal.digest;
      if (!d) return { attention: [], worth: [] };
      if (this._picks && this._picksFrom === d) return this._picks;
      const all = [...d.attention, ...d.worth_a_look], used = new Set(), pick = (f) => { const c = all.find((x) => !used.has(x.id) && f(x)); if (c) used.add(c.id); return c; };
      const attention = [
        pick((c) => c.type === "results" && c.payload?.ok && c.payload.results_day?.comparisons?.length),
        pick((c) => c.type === "results" && c.payload?.ok && !(c.payload.notes || []).length),
        pick((c) => c.type === "results" && c.payload?.ok && (c.payload.notes || []).length),
        pick((c) => c.type === "results" && c.payload?.ok === false),
        pick((c) => c.type === "mgmt_change" && c.payload?.people?.length > 1),
        pick((c) => c.type === "mgmt_change" && c.payload?.people?.length),
        pick((c) => this.isGov(c)),
        pick((c) => c.payload?.kind === "order"),
        pick((c) => !c.payload && c.one_liner),
      ].filter(Boolean);
      const worth = d.worth_a_look.filter((c) => !used.has(c.id)).slice(0, 3);
      this._picksFrom = d; this._picks = { attention, worth };
      return this._picks;
    },

    // ---------- formatting ----------
    status(s) { return STATUS_LABEL[s] || s; },
    cr(n) { return n == null ? "Not in sources" : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: Math.abs(n) >= 100 ? 0 : 1 }) + " cr"; },
    rupee(n) { return n == null ? "–" : "₹" + Number(n).toLocaleString("en-IN", { minimumFractionDigits: n < 10000 ? 2 : 0, maximumFractionDigits: n < 10000 ? 2 : 0 }); },
    whole(n) { return n == null ? "–" : Math.round(n).toLocaleString("en-IN"); },
    pct(n) { return n == null ? "–" : (n > 0 ? "+" : "") + Number(n).toFixed(1) + "%"; },
    arrow(n) { return n == null ? "–" : (n > 0 ? "▲ " : n < 0 ? "▼ " : "") + Math.abs(n).toFixed(1); },
    cls(n) { return n == null ? "t2" : n > 0 ? "up" : n < 0 ? "down" : "t2"; },
    when(iso) {
      if (!iso) return "";
      const d = new Date(iso), mins = (Date.now() - d) / 60000;
      if (mins < 60) return `${Math.max(1, Math.round(mins))}m ago`;
      if (mins < 60 * 24) return `${Math.round(mins / 60)}h ago`;
      if (mins < 60 * 24 * 7) return `${Math.round(mins / 1440)}d ago`;
      return d.toLocaleDateString("en-IN", { day: "numeric", month: "short", ...IST });
    },
    day(iso) { return iso ? new Date(iso).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", ...IST }) : ""; },
    weekday(iso) { return this.day(iso); },
    dm(iso) { return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", ...IST }); },
    dayLabel(iso) {
      const t = this.today(), y = new Date(Date.now() - 864e5).toLocaleDateString("en-CA", IST);
      return iso === t ? "Today" : iso === y ? "Yesterday" : this.day(iso);
    },
    metricName(m) { return { revenue: "Revenue", ebitda: "EBITDA", pbt: "PBT", pat: "PAT" }[m] || m; },
  };
}

function b64ToBytes(b64) {
  const s = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((b64.length + 3) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}
