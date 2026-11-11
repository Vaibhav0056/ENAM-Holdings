/* FINN PWA: one Alpine component. Every number on screen comes from the API (Python computed it); this file only
   formats and arranges. Hash routes: #today #holdings #holding/SYM #research #research/SYM #alerts #settings
   #card/ID #gallery */

const TYPE_LABEL = {
  results: "Results", mgmt_change: "Management", governance: "Governance", m_and_a: "M&A", fundraise: "Fundraise",
  order_win: "Order", credit_rating: "Rating", insider_pledge: "Insider / pledge", dividend_action: "Dividend",
  litigation_regulatory: "Legal / regulatory", investor_meet: "Investor meet", board_meeting_notice: "Board meeting",
  agm_egm: "AGM / EGM", routine_compliance: "Compliance", other: "Update",
};
const STATUS_LABEL = { intact: "Intact", watch: "Watch", at_risk: "At risk" };
const VERDICT_LABEL = { delivered: "Delivered", partial: "Partial", missed: "Missed", pending: "Pending", unverifiable: "Unverifiable" };
const KPI_LABEL = { revenue_growth_yoy: "Revenue growth YoY", ebitda_margin: "EBITDA margin", pat_growth_yoy: "PAT growth YoY", pat_margin: "PAT margin" };
const WINDOWS = [["24h", "24h"], ["3d", "3 days"], ["7d", "7 days"], ["30d", "30 days"], ["since_last", "Since last check"]];
const WAKING_MS = 2500;

function store(key, val) {           // per-viewer conveniences only; never required for correctness
  try { if (val === undefined) return JSON.parse(localStorage.getItem(key)); localStorage.setItem(key, JSON.stringify(val)); }
  catch (e) { return null; }
}

function finn() {
  return {
    // route from the URL at construction: starting on "today" and switching after init shifted the whole page
    route: ((location.hash || "#today").slice(1).split("/")[0]) || "today", param: null, theme: store("finn.theme") || "system",
    net: { waking: false, error: null, stale: false },
    win: store("finn.window") || "24h", custom: { from: "", to: "" }, showCustom: false,
    digest: null, catalysts: [], routineOpen: false,
    holdings: null, holding: null, editing: false, draft: null,
    alerts: null, unread: 0, rules: null,
    rq: { ticker: "", job: null, report: null, stage: null, progress: 0, stages: [], error: null, open: {} },
    sheet: null, cardDetail: null, evidence: null,
    push: { supported: "serviceWorker" in navigator && "PushManager" in window, state: "", msg: "" },
    isIOS: /iphone|ipad|ipod/i.test(navigator.userAgent), standalone: matchMedia("(display-mode: standalone)").matches,
    toast: "", gallery: null,

    init() {
      this.applyTheme();
      window.addEventListener("hashchange", () => this.go());
      this.go();
      this.loadAlerts(true);
      if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
        navigator.serviceWorker.addEventListener("message", (e) => {
          if (e.data?.type === "navigate" && e.data.url) location.hash = new URL(e.data.url).hash || "#alerts";
        });
      }
    },

    // ---------- plumbing ----------
    async api(path, opts = {}) {
      const t = setTimeout(() => (this.net.waking = true), WAKING_MS);
      try {
        const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
        if (!r.ok) {
          let msg = `${r.status}`;
          try { msg = (await r.json()).detail || msg; } catch (e) {}
          throw new Error(msg);
        }
        this.net.error = null; this.net.stale = false;
        return await r.json();
      } catch (e) {
        this.net.error = e.message || "Network error";
        throw e;
      } finally { clearTimeout(t); this.net.waking = false; }
    },
    say(msg) { this.toast = msg; clearTimeout(this._t); this._t = setTimeout(() => (this.toast = ""), 2600); },

    go() {
      const h = (location.hash || "#today").slice(1);
      const [name, ...rest] = h.split("/");
      const param = rest.join("/") || null;
      this.sheet = null;
      if (name === "card" && param) { this.openCard(+param); if (!this.digest) this.route = "today", this.loadDigest(); return; }
      this.route = ["today", "holdings", "holding", "research", "alerts", "settings", "gallery"].includes(name) ? name : "today";
      this.param = param;
      window.scrollTo(0, 0);
      if (this.route === "today") { this.loadDigest(); this.loadCatalysts(); }
      if (this.route === "holdings") this.loadHoldings();
      if (this.route === "holding" && param) this.loadHolding(param);
      if (this.route === "research" && param) this.openResearch(param);
      if (this.route === "alerts") this.loadAlerts();
      if (this.route === "settings") this.loadRules();
      if (this.route === "gallery") this.gallery = galleryData();
    },
    nav(hash) { location.hash = hash; },

    applyTheme() {
      const t = this.theme;
      if (t === "system") document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", t);
      store("finn.theme", t);
    },

    // ---------- Today ----------
    async loadDigest() {
      const qs = this.win === "custom" ? `from=${this.custom.from}&to=${this.custom.to}` : `window=${this.win}`;
      try { this.digest = await this.api(`/digest?${qs}`); }
      catch (e) { if (this.digest) this.net.stale = true; }
    },
    setWindow(w) {
      if (w === "custom") { this.showCustom = true; return; }
      this.win = w; store("finn.window", w); this.digest = null; this.loadDigest();
    },
    applyCustom() {
      if (!this.custom.from || !this.custom.to) return this.say("Pick both dates");
      this.win = "custom"; this.showCustom = false; this.digest = null; this.loadDigest();
    },
    async loadCatalysts() { try { this.catalysts = (await this.api("/api/v2/catalysts?days=7")).events; } catch (e) {} },
    async markChecked() { await this.api("/api/state/checked", { method: "POST" }); this.say("Marked as checked"); },
    async review(card, undo = false) {
      card.reviewed = !undo;
      try { await this.api(`/api/v2/cards/${card.id}/review${undo ? "?undo=true" : ""}`, { method: "POST" }); }
      catch (e) { card.reviewed = undo; return; }
      if (!undo) this.say("Reviewed");
      if (this.digest) {
        const all = [...this.digest.attention, ...this.digest.worth_a_look];
        this.digest.caught_up = all.every((c) => c.reviewed);
      }
    },
    swipe(el, card) {      // swipe right on a card = mark reviewed
      let x0 = null, dx = 0;
      el.addEventListener("pointerdown", (e) => { x0 = e.clientX; dx = 0; });
      el.addEventListener("pointermove", (e) => {
        if (x0 === null) return; dx = e.clientX - x0;
        if (dx > 0) el.style.transform = `translateX(${Math.min(dx, 120)}px)`;
      });
      const end = () => {
        if (x0 !== null && dx > 90 && !card.reviewed) this.review(card);
        el.style.transform = ""; x0 = null;
      };
      el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end); el.addEventListener("pointerleave", end);
    },
    unreviewedCount() {
      if (!this.digest) return 0;
      return [...this.digest.attention, ...this.digest.worth_a_look].filter((c) => !c.reviewed).length;
    },

    // ---------- card sheet ----------
    async openCard(id) {
      this.sheet = "card"; this.cardDetail = null; this.evidence = null;
      try { this.cardDetail = await this.api(`/api/v2/cards/${id}`); } catch (e) { this.sheet = null; this.say("Card not found"); }
    },
    closeSheet() {
      this.sheet = null; this.evidence = null;
      if (location.hash.startsWith("#card/")) history.replaceState(null, "", "#today");
    },
    async openEvidence(docId, page, quote) {
      if (!docId || !page) return;
      this.evidence = { loading: true, quote };
      try { this.evidence = { ...(await this.api(`/api/v2/documents/${docId}/pages/${page}`)), quote }; }
      catch (e) { this.evidence = { error: "That page isn't stored yet. Open the PDF instead." }; }
    },
    highlight(text, quote) {
      const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
      if (!quote) return esc(text);
      const words = quote.split(/\s+/).filter((w) => w.length > 3).slice(0, 6).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
      let out = esc(text);
      if (words.length) {
        const first = words[0];
        out = out.replace(new RegExp(`(${first})`, "i"), "<mark>$1</mark>");
      }
      return out;
    },

    // ---------- Holdings ----------
    async loadHoldings() { try { this.holdings = (await this.api("/api/v2/holdings")).holdings; } catch (e) {} },
    async loadHolding(sym) {
      this.holding = null; this.editing = false;
      try { this.holding = await this.api(`/api/v2/holdings/${sym}`); this.loadJournal(sym); } catch (e) {}
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
      } catch (e) { this.say(this.net.error); }
    },

    // ---------- Research ----------
    startResearch() {
      const t = (this.rq.ticker || "").trim().toUpperCase();
      if (!/^[A-Z0-9&-]{1,20}$/.test(t)) return this.say("Enter an NSE symbol, e.g. POLYCAB");
      this.nav(`#research/${t}`);
    },
    async openResearch(sym, force = false) {
      Object.assign(this.rq, { ticker: sym, job: null, report: null, stage: null, progress: 0, error: null, open: {} });
      try {
        const r = await this.api(`/api/v2/research/${sym}${force ? "?force=true" : ""}`, { method: "POST" });
        if (r.cached) { this.rq.report = r.report; return; }
        this.rq.job = r.job_id; this.follow(r.job_id);
      } catch (e) { this.rq.error = this.net.error; }
    },
    follow(jobId) {
      const done = async () => {
        try { const j = await this.api(`/api/v2/research/job/${jobId}`); if (j.report) this.rq.report = j.report; else this.rq.error = j.error; }
        catch (e) { this.rq.error = this.net.error; }
      };
      if (!window.EventSource) return this.poll(jobId);
      const es = new EventSource(`/api/v2/research/job/${jobId}/events`);
      es.addEventListener("stage", (e) => { const d = JSON.parse(e.data); Object.assign(this.rq, { stage: d.stage, progress: d.progress, stages: d.stages }); });
      es.addEventListener("done", () => { es.close(); done(); });
      es.addEventListener("failed", (e) => { es.close(); this.rq.error = JSON.parse(e.data).error || "Research failed"; });
      es.onerror = () => { es.close(); this.poll(jobId); };
    },
    async poll(jobId) {
      for (let i = 0; i < 400; i++) {
        let j; try { j = await this.api(`/api/v2/research/job/${jobId}`); } catch (e) { return; }
        Object.assign(this.rq, { stage: j.stage, progress: j.progress, stages: j.stages });
        if (j.status === "done") { this.rq.report = j.report; return; }
        if (j.status === "failed") { this.rq.error = j.error; return; }
        await new Promise((r) => setTimeout(r, 1500));
      }
    },
    stageState(s) {
      const i = this.rq.stages.indexOf(s), cur = this.rq.stages.indexOf(this.rq.stage);
      return i < cur ? "done" : i === cur ? "now" : "";
    },
    source(id) { return (this.rq.report?.sources || []).find((s) => s.id === id) || {}; },
    gaugeDash(score) { const c = 2 * Math.PI * 30; return `${(c * (score || 0)) / 100} ${c}`; },

    // ---------- Phase 10: replay, audio brief, journal, feedback ----------
    rp: { open: false, daysAgo: 14, data: null, loading: false },
    replayDate() { const d = new Date(Date.now() - this.rp.daysAgo * 86400000); return d.toISOString().slice(0, 10); },
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
      let words = 0, out = [];
      for (const l of lines) { words += l.split(/\s+/).length; if (words > 160) break; out.push(l); }   // ~60 s at 160 wpm
      return out.join(" ").replace(/₹/g, "rupees ").replace(/cr/g, "crore").replace(/YoY/g, "year on year");
    },
    toggleBrief() {
      if (!("speechSynthesis" in window)) return this.say("This browser can't read aloud.");
      if (this.speaking) { speechSynthesis.cancel(); this.speaking = false; return; }
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
      try { const r = await this.api(`/api/v2/cards/${card.id}/feedback`, { method: "POST", body: JSON.stringify({ signal }) });
            this.say(`Thanks. ${this.type(r.type)} weight now ${r.adjustment >= 0 ? "+" : ""}${r.adjustment} (bounded ±${r.bound})`); }
      catch (e) { this.say(this.net.error); this.net.error = null; }
    },
    strongEvidence(l) { return !!(l.actual || (l.also_said_in && l.also_said_in.length)); },

    // ---------- Copilot ----------
    cp: { q: "", steps: [], answer: null, busy: false, error: null },
    copilotSymbol() { return this.route === "holding" ? this.holding?.symbol || this.param : this.route === "research" ? this.param : null; },
    suggestions() {
      const s = this.copilotSymbol();
      return s ? [`What has ${s}'s management promised for this year?`, `What did ${s} say about competition?`,
                  `How credible has ${s}'s management been?`, `Why is my ${s} thesis on ${this.status(this.holding?.timeline?.[0]?.status || "intact")}?`]
               : ["Which holdings reported results this month?", "Any governance red flags in my portfolio?"];
    },
    openCopilot(q) { this.sheet = "copilot"; if (q) this.askCopilot(q); },
    askCopilot(q) {
      q = (q || this.cp.q || "").trim();
      if (q.length < 3) return;
      Object.assign(this.cp, { q, steps: [], answer: null, busy: true, error: null });
      const sym = this.copilotSymbol();
      const es = new EventSource(`/api/v2/copilot?q=${encodeURIComponent(q)}${sym ? "&symbol=" + sym : ""}`);
      es.addEventListener("step", (e) => this.cp.steps.push(JSON.parse(e.data)));
      es.addEventListener("note", (e) => this.cp.steps.push({ note: JSON.parse(e.data).text }));
      es.addEventListener("answer", (e) => { this.cp.answer = JSON.parse(e.data); this.cp.busy = false; es.close(); });
      es.addEventListener("error", (e) => { try { this.cp.error = JSON.parse(e.data).error; } catch (_) { this.cp.error = "The assistant didn't answer."; } this.cp.busy = false; es.close(); });
    },
    toolLabel(t) { return { search_docs: "Searching documents", get_page: "Reading a page", get_metric: "Looking up figures",
                            get_filings: "Checking filings", get_thesis: "Reading your thesis", get_ledger: "Reading the promise ledger" }[t] || t; },
    citeOpen(id) {
      const e = this.cp.answer?.evidence?.find((x) => x.id === id);
      if (!e) return;
      if (e.document_id && e.page) { this.openEvidence(e.document_id, e.page, e.text.replace(/^\[[^\]]*\]\s*/, "").slice(0, 120)); this.sheet = "evidence"; }
      else if (e.filing_id) this.nav("#card/" + e.filing_id);
      else if (e.url) window.open(e.url, "_blank", "noopener");
    },

    // ---------- Deep dives ----------
    deep: { recipe: null, data: null, loading: false, error: null },
    deepRecipes: [["cash_quality", "Cash quality"], ["balance_sheet", "Balance sheet"], ["language_diff", "Language diff"],
                  ["hedge_meter", "Hedge-meter"], ["dodge_detector", "Dodge detector"], ["worry_map", "Worry map"],
                  ["fine_print", "Fine print"], ["peer_compare", "Peer compare"]],
    async openDeep(recipe) {
      Object.assign(this.deep, { recipe, data: null, loading: true, error: null });
      try { this.deep.data = await this.api(`/api/v2/deep/${this.param}/${recipe}`); }
      catch (e) { this.deep.error = this.net.error; this.net.error = null; }
      finally { this.deep.loading = false; }
    },
    deepRows(obj) { return Object.entries(obj || {}).filter(([k, v]) => typeof v === "number" || v === null); },
    label(k) { return k.replace(/_/g, " ").replace(/\bocf\b/i, "OCF").replace(/\bfcf\b/i, "FCF").replace(/\bpat\b/i, "PAT").replace(/\bebitda\b/i, "EBITDA"); },
    fmt(v) { return v == null ? "Not in sources" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }); },

    // ---------- Alerts ----------
    async loadAlerts(quiet = false) {
      try { const r = await this.api("/api/v2/alerts"); this.alerts = r.alerts; this.unread = r.unread; }
      catch (e) { if (quiet) this.net.error = null; }
    },
    async openAlert(a) {
      if (!a.read) { a.read = true; this.unread = Math.max(0, this.unread - 1); this.api(`/api/v2/alerts/${a.id}/read`, { method: "POST" }).catch(() => {}); }
      if (a.link) location.hash = a.link.replace(/^\/?#?/, "#");
    },
    async readAll() { await this.api("/api/v2/alerts/read_all", { method: "POST" }); this.alerts.forEach((a) => (a.read = true)); this.unread = 0; },

    // ---------- Settings ----------
    async loadRules() { try { this.rules = (await this.api("/api/v2/rules")).rules; } catch (e) {} },
    async saveRule(r) {
      try { await this.api(`/api/v2/rules/${r.kind}`, { method: "PUT", body: JSON.stringify({ params: r.params, enabled: r.enabled }) }); this.say("Rule saved"); }
      catch (e) { this.say(this.net.error); this.loadRules(); }
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
      this.push.state = "on"; this.push.msg = "Notifications are on.";
    },
    async testPush() {
      const r = await this.api("/api/v2/push/test", { method: "POST" });
      this.push.msg = r.sent ? `Sent to ${r.sent} device${r.sent > 1 ? "s" : ""}.` : (r.skipped || "No subscribed device yet.");
    },
    async runPipeline() { this.say("Checking for new alerts…"); const r = await this.api("/api/v2/alerts/evaluate", { method: "POST" }); this.loadAlerts(); this.say(`Done: ${JSON.stringify(r).length ? "alerts updated" : ""}`); },

    // ---------- formatting ----------
    type(t) { return TYPE_LABEL[t] || t; },
    status(s) { return STATUS_LABEL[s] || s; },
    verdict(v) { return VERDICT_LABEL[v] || v; },
    kpi(m) { return KPI_LABEL[m] || m; },
    windows() { return WINDOWS; },
    cr(n) { return n == null ? "–" : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: n >= 100 ? 0 : 1 }) + " cr"; },
    pct(n) { return n == null ? "–" : (n > 0 ? "+" : "") + Number(n).toFixed(1) + "%"; },
    cls(n) { return n == null ? "mute" : n > 0 ? "up" : n < 0 ? "down" : "mute"; },
    when(iso) {
      if (!iso) return "";
      const d = new Date(iso), now = new Date(), mins = (now - d) / 60000;
      if (mins < 60) return `${Math.max(1, Math.round(mins))}m ago`;
      if (mins < 60 * 24) return `${Math.round(mins / 60)}h ago`;
      return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
    },
    day(iso) { return iso ? new Date(iso).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" }) : ""; },
    metricName(m) { return { revenue: "Revenue", ebitda: "EBITDA", pbt: "PBT", pat: "PAT" }[m] || m; },
  };
}

function b64ToBytes(b64) {
  const s = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((b64.length + 3) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

// Sample data for #gallery: every card kind and every state, rendered with the real templates.
function galleryData() {
  const base = { symbol: "TCS", company: "Tata Consultancy Services Ltd", filed_at: new Date().toISOString(), reviewed: false,
                 why: { reasons: [], score: 0.8, type_weight: 1, magnitude: 1, novelty: 1, thesis_multiplier: 1 } };
  return {
    results: { ...base, id: -1, type: "results", bucket: "attention", headline: "Q2 FY27 results: Revenue ₹73,188 cr (+11.2% YoY), PAT ₹13,934 cr (+14.9% YoY)",
      symbol: "SAMPLECO", company: "Sample Company Ltd (fictional)",
      payload: { ok: true, basis: "consolidated", confidence: "high", period: "Q2 FY27", table: [
        { metric: "revenue", found: true, current: 73188, qoq: 1.3, yoy: 11.2 }, { metric: "ebitda", found: true, current: 19882, qoq: 2.0, yoy: 9.8 },
        { metric: "pbt", found: true, current: 18638, qoq: 3.9, yoy: 16.0 }, { metric: "pat", found: true, current: 13934, qoq: 3.8, yoy: 14.9 }],
        results_day: { comparisons: [{ metric: "EBITDA margin", promised: "26-28%", delivered: 27.2, verdict: "on track" }] } },
      thesis_impact: "watch", why_it_matters: "Quarterly results: check them against the thesis." },
    lowconf: { ...base, id: -2, symbol: "SAMPLECO", type: "results", bucket: "attention", headline: "Un-Audited Financial Results For The Quarter Ended 30 September 2026",
      payload: { ok: false, reason: "figures are 100× off BSE's own numbers (unit or digit misread); not shown" } },
    mgmt: { ...base, id: -3, symbol: "SAMPLECO", type: "mgmt_change", bucket: "attention", headline: "A. Sample, Managing Director and Chief Executive Officer: resigns",
      payload: { people: [{ name: "A. Sample", role: "Managing Director and Chief Executive Officer", direction: "resigned", senior: true,
        profile_quote: "A fictional profile quote.", quote: "A. Sample will step down as MD & CEO", page: 1 }],
        governance_flags: [{ kind: "senior_exit", severity: "high", summary: "A. Sample (MD & CEO) resigned" }] }, thesis_impact: "watch",
      why_it_matters: "Senior leadership change: management quality is part of every thesis." },
    governance: { ...base, id: -4, symbol: "SAMPLECO", type: "governance", bucket: "attention", headline: "Resignation of Statutory Auditor (fictional example)",
      payload: { kind: "governance", ok: true, flags: [{ kind: "auditor_resignation", severity: "high", summary: "The statutory auditor resigned before completing its term." }] },
      thesis_impact: "threatens", why_it_matters: "An auditor resigning mid-term is a classic integrity red flag." },
    worth: { ...base, id: -5, symbol: "SAMPLECO", type: "order_win", bucket: "worth_a_look", headline: "Sample Company wins orders (Large*) (fictional example)",
      one_liner: "A fictional order win, to show the compact row.", thesis_impact: "supports", why_it_matters: null },
  };
}
