/* FINN single-page app (Alpine.js). Hash routes: #today #alerts #research/SYM #settings #card/ID */
const ICONS = {
  today: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  alerts: '<path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  research: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  settings: '<path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/>',
};

function store(k, v) {
  try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) { return null; }
}

async function api(path, opts = {}) {
  const r = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

function b64ToUint8(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

function finn() {
  return {
    tabs: [
      { id: "today", label: "Today", icon: ICONS.today },
      { id: "alerts", label: "Alerts", icon: ICONS.alerts },
      { id: "research", label: "Research", icon: ICONS.research },
      { id: "settings", label: "Settings", icon: ICONS.settings },
    ],
    tab: "today",
    feed: null, feedWindow: "24h", routineOpen: false, focusId: null,
    loading: { feed: false },
    alerts: [], unread: 0,
    rq: { query: "", job: null, result: null, open: null, timer: null },
    recent: [],
    rules: [], ruleTypes: [], typeLabels: {},
    holdings: [],
    pushState: "off", pushMsg: "", iosHint: false,
    refreshing: false, refreshMsg: "",
    toast: "", _swipe: {},

    async init() {
      window.addEventListener("hashchange", () => this.route());
      if (navigator.serviceWorker) {
        navigator.serviceWorker.addEventListener("message", (e) => {
          if (e.data && e.data.type === "navigate") location.href = e.data.url;
        });
        navigator.serviceWorker.getRegistration().then(async (reg) => {
          const sub = reg && reg.pushManager && (await reg.pushManager.getSubscription());
          if (sub && Notification.permission === "granted") this.pushState = "on";
        }).catch(() => {});
      }
      const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
      const standalone = window.navigator.standalone || matchMedia("(display-mode: standalone)").matches;
      this.iosHint = isIOS && !standalone && !store("finn.iosHint");
      this.loadAlerts();
      this.loadRecent();
      await this.route();
      setInterval(() => this.loadAlerts(), 60000);
    },

    async route() {
      const h = (location.hash || "#today").slice(1);
      const [name, arg] = h.split("/");
      if (name === "card" && arg) {
        this.tab = "today";
        await this.focusCard(parseInt(arg, 10));
      } else if (name === "research") {
        this.tab = "research";
        if (arg && (!this.rq.result || this.rq.result.symbol !== arg.toUpperCase()) &&
            (!this.rq.job || this.rq.job.symbol !== arg.toUpperCase())) this.startResearch(arg, false);
      } else if (["today", "alerts", "settings"].includes(name)) {
        this.tab = name;
        if (name === "today" && !this.feed) this.loadFeed();
        if (name === "settings") this.loadSettings();
        if (name === "alerts") this.loadAlerts();
      } else {
        this.tab = "today"; if (!this.feed) this.loadFeed();
      }
    },
    go(t) { if (location.hash === "#" + t) this.route(); else location.hash = t; window.scrollTo(0, 0); },

    flash(msg) { this.toast = msg; clearTimeout(this._t); this._t = setTimeout(() => (this.toast = ""), 2400); },

    /* ---------- today ---------- */
    async loadFeed() {
      this.loading.feed = true;
      try { this.feed = await api(`/api/feed?window=${this.feedWindow}`); }
      catch (e) { this.flash("Couldn't load feed: " + e.message); }
      this.loading.feed = false;
    },
    setWindow(w) { this.feedWindow = w; this.loadFeed(); },
    feedHeader() {
      if (!this.feed) return "";
      const label = this.feed.note
        ? "Since " + new Date(this.feed.since).toLocaleDateString("en-IN", { day: "numeric", month: "short" })
        : { "24h": "Last 24h", "3d": "Last 3 days", "7d": "Last 7 days", "30d": "Last 30 days" }[this.feed.window];
      const c = this.feed.counts;
      return `${label} · ${c.attention} need attention · ${c.worth_a_look} worth a look · ${c.routine} routine hidden`;
    },
    async focusCard(id) {
      if (!this.feed) await this.loadFeed();
      let present = this.feed && this.feed.cards.find((c) => c.id === id);
      if (!present) {
        try {
          const c = await api(`/api/cards/${id}`);
          if (this.feed) { this.feed.cards = [c, ...this.feed.cards.filter((x) => x.id !== id)]; }
        } catch (_) { this.flash("That filing is no longer available"); return; }
      }
      this.focusId = id;
      this.$nextTick(() => {
        const el = document.getElementById("card-" + id);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    },
    async review(c) {
      const el = document.getElementById("card-" + c.id);
      if (el) { el.style.transform = "translateX(110%)"; el.style.opacity = "0"; }
      try { await api(`/api/cards/${c.id}/review`, { method: "POST" }); } catch (_) {}
      setTimeout(() => {
        this.feed.cards = this.feed.cards.filter((x) => x.id !== c.id);
        this.feed.reviewed.push(c);
        if (c.bucket === "attention") this.feed.counts.attention--; else this.feed.counts.worth_a_look--;
      }, 180);
    },
    async unreviewAll() {
      for (const c of this.feed.reviewed) await api(`/api/cards/${c.id}/review?undo=1`, { method: "POST" });
      this.loadFeed();
    },
    swipeStart(e, c) { this._swipe = { x: e.touches[0].clientX, y: e.touches[0].clientY, id: c.id, dx: 0 }; },
    swipeMove(e, c) {
      if (this._swipe.id !== c.id) return;
      const dx = e.touches[0].clientX - this._swipe.x, dy = e.touches[0].clientY - this._swipe.y;
      if (Math.abs(dx) > Math.abs(dy) && dx > 0) {
        this._swipe.dx = dx;
        const el = document.getElementById("card-" + c.id);
        if (el) { el.style.transform = `translateX(${dx}px)`; el.style.opacity = String(Math.max(0.3, 1 - dx / 300)); }
      }
    },
    swipeEnd(e, c) {
      if (this._swipe.id !== c.id) return;
      const el = document.getElementById("card-" + c.id);
      if (this._swipe.dx > 110) this.review(c);
      else if (el) { el.style.transform = ""; el.style.opacity = ""; }
      this._swipe = {};
    },

    /* ---------- alerts ---------- */
    async loadAlerts() {
      try { const r = await api("/api/alerts"); this.alerts = r.alerts; this.unread = r.unread; } catch (_) {}
    },
    async openAlert(a) {
      if (!a.read) { a.read = 1; this.unread = Math.max(0, this.unread - 1); api(`/api/alerts/${a.id}/read`, { method: "POST" }); }
      location.hash = (a.link || "#alerts").replace(/^\/?#?/, "");
    },
    async readAll() { await api("/api/alerts/read_all", { method: "POST" }); this.loadAlerts(); },

    /* ---------- research ---------- */
    openResearch(sym) { location.hash = "research/" + sym; },
    async loadRecent() { try { this.recent = (await api("/api/research/recent")).reports; } catch (_) {} },
    async startResearch(sym, setHash = true) {
      sym = (sym || "").trim().toUpperCase();
      if (!sym) return;
      this.rq.query = sym;
      if (setHash && location.hash !== "#research/" + sym) { history.replaceState(null, "", "#research/" + sym); }
      this.tab = "research";
      clearTimeout(this.rq.timer);
      this.rq.result = null; this.rq.open = null;
      try {
        const { job_id } = await api(`/research/${encodeURIComponent(sym)}`, { method: "POST" });
        this.poll(job_id);
      } catch (e) {
        this.rq.job = { symbol: sym, status: "error", error: e.message, stages: [] };
      }
    },
    async poll(id) {
      try {
        const j = await api(`/research/job/${id}`);
        this.rq.job = j;
        if (j.status === "done") { this.rq.result = j.result; this.rq.job = null; this.loadRecent(); return; }
        if (j.status === "error") return;
      } catch (e) { this.rq.job = { status: "error", error: e.message, stages: [] }; return; }
      this.rq.timer = setTimeout(() => this.poll(id), 1500);
    },
    stageState(i) {
      const j = this.rq.job; if (!j) return "todo";
      const cur = j.stages.indexOf(j.stage);
      return i < cur ? "done" : i === cur ? "now" : "todo";
    },
    verdictSummary() {
      const c = this.rq.result && this.rq.result.counts; if (!c) return "";
      return ["delivered", "partially", "missed", "pending", "unverifiable"].filter((k) => c[k]).map((k) => `${c[k]} ${k}`).join(" · ");
    },
    verdictCls(v) {
      return { delivered: "bg-up/15 text-up", partially: "bg-accent/15 text-accent", missed: "bg-down/15 text-down",
               pending: "bg-raised text-mute", unverifiable: "bg-raised text-faint" }[v] || "bg-raised text-mute";
    },
    actualText(r) {
      const f = (a) => `${this.fmt(a.value)}${a.unit === "%" ? "%" : " " + a.unit} (${a.period})`;
      if (r.actual) return "Actual: " + f(r.actual);
      if (r.progress) return "Latest: " + f(r.progress) + (r.tracking ? " · " + r.tracking : "");
      if (r.verdict === "unverifiable") return r.guidance.target_low == null ? "Vague target, can't be checked" : "Not found in sources";
      return "Not reported yet";
    },
    docLabel(id) {
      const s = this.rq.result && this.rq.result.sources.find((x) => x.id === id);
      if (!s) return id || "";
      return s.kind === "transcript" ? `Concall ${s.quarter || s.label}` : "Annual report";
    },

    /* ---------- settings ---------- */
    async loadSettings() {
      try {
        const r = await api("/api/rules");
        this.rules = r.rules.map((x) => Object.assign(x, { enabled: !!x.enabled }));
        this.ruleTypes = r.types; this.typeLabels = r.type_labels;
        this.holdings = (await api("/api/portfolio")).portfolio;
        const st = await api("/api/refresh");
        this.refreshing = st.running;
        this.refreshMsg = st.running ? "Refreshing…" : (st.error ? "Last refresh failed: " + st.error : "");
      } catch (e) { this.flash("Couldn't load settings"); }
    },
    ruleLabel(r) {
      return { volume_spike: "Volume spike", price_move: "Big price move", announcement_type: "Filing types" }[r.kind] || r.label;
    },
    hasType(r, t) { return (r.types || "").split(",").includes(t); },
    toggleType(r, t) {
      const s = new Set((r.types || "").split(",").filter(Boolean));
      s.has(t) ? s.delete(t) : s.add(t);
      r.types = [...s].join(",");
    },
    async saveRules() {
      try {
        await api("/api/rules", { method: "PUT", body: JSON.stringify(this.rules.map((r) => ({
          kind: r.kind, threshold: r.threshold, types: r.types, enabled: !!r.enabled })) ) });
        const ev = await api("/api/alerts/evaluate", { method: "POST" });
        this.flash(`Saved. ${ev.new} new alert${ev.new === 1 ? "" : "s"}`);
        this.loadAlerts();
      } catch (e) { this.flash("Save failed: " + e.message); }
    },
    async refreshData() {
      this.refreshing = true; this.refreshMsg = "Fetching prices and BSE filings…";
      await api("/api/refresh", { method: "POST" });
      const tick = async () => {
        const st = await api("/api/refresh");
        if (st.running) { setTimeout(tick, 3000); return; }
        this.refreshing = false;
        const l = st.last || {};
        this.refreshMsg = st.error ? "Refresh failed: " + st.error :
          `Done. ${(l.ingest && l.ingest.new_announcements) || 0} new filings, ${(l.alerts && typeof l.alerts === "number") ? l.alerts : 0} new alerts.`;
        this.feed = null; this.loadAlerts();
      };
      setTimeout(tick, 3000);
    },
    dismissIosHint() { this.iosHint = false; store("finn.iosHint", "1"); },
    async enablePush() {
      if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
        this.pushMsg = /iphone|ipad/i.test(navigator.userAgent)
          ? "On iPhone, add FINN to your Home Screen first, then open it from there."
          : "This browser doesn't support push notifications.";
        return;
      }
      try {
        const perm = await Notification.requestPermission();  // asked only on this tap
        if (perm !== "granted") { this.pushMsg = "Permission was not granted."; return; }
        const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
        await navigator.serviceWorker.ready;
        const { key, configured } = await api("/api/push/key");
        if (!configured) { this.pushMsg = "Server has no VAPID keys configured."; return; }
        let sub = await reg.pushManager.getSubscription();
        if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToUint8(key) });
        await api("/api/push/subscribe", { method: "POST", body: JSON.stringify(sub.toJSON()) });
        this.pushState = "on"; this.pushMsg = "You'll get a notification when an alert fires.";
      } catch (e) { this.pushMsg = "Couldn't enable notifications: " + e.message; }
    },
    async testPush() {
      try {
        const r = await api("/api/push/test", { method: "POST" });
        this.pushMsg = r.skipped ? r.skipped : `Sent to ${r.sent} device${r.sent === 1 ? "" : "s"}.` + (r.sent ? "" : " Enable notifications first.");
      } catch (e) { this.pushMsg = "Test failed: " + e.message; }
    },

    /* ---------- formatting ---------- */
    metricLabel(m) { return { revenue: "Revenue", ebitda: "EBITDA", pbt: "PBT", pat: "PAT" }[m] || m; },
    fmt(v) {
      if (v == null || isNaN(v)) return "—";
      const a = Math.abs(v);
      return v.toLocaleString("en-IN", { maximumFractionDigits: a >= 100 ? 0 : a >= 10 ? 1 : 2 });
    },
    crore(v) { return v == null ? "—" : this.fmt(v); },
    pct(v) { return v == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(1) + "%"; },
    deltaCls(v) { return v == null ? "text-faint" : v > 0 ? "text-up" : v < 0 ? "text-down" : "text-mute"; },
    score(c) { return c.score != null ? c.score.toFixed(2) : ""; },
    ago(iso) {
      if (!iso) return "";
      const d = new Date(iso), s = (Date.now() - d.getTime()) / 1000;
      if (s < 3600) return Math.max(1, Math.round(s / 60)) + "m ago";
      if (s < 86400) return Math.round(s / 3600) + "h ago";
      if (s < 7 * 86400) return Math.round(s / 86400) + "d ago";
      return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
    },
    shortDate(d) { return d ? new Date(d + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : ""; },
  };
}
