"""Golden-path end-to-end check at 390 px, driven over the Chrome DevTools protocol (no extra dependencies: the
`websockets` package already comes with uvicorn[standard]).

    python tests/e2e/golden_path.py [--runs 5] [--base http://127.0.0.1:8000] [--chrome PATH]

Path: Today -> results card -> evidence -> "Why am I seeing this?" -> holding -> research (POLYCAB) -> every deep
dive -> a copilot question -> alerts -> settings, adding a company and removing it again. Fails on any console
error or uncaught exception, and on any step slower than 1 s, except the copilot's model answer (reported
separately; the sheet itself must open within 1 s). Not part of pytest: it needs a running server and Chrome.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

LIMIT_S = 1.0
CHROME_PATHS = [r"C:\Program Files\Google\Chrome\Application\chrome.exe", "/usr/bin/google-chrome", "/usr/bin/chromium",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]


class Page:
    def __init__(self, ws):
        self.ws, self.n, self.errors, self.pending = ws, 0, [], {}

    async def pump(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self.pending:
                self.pending.pop(msg["id"]).set_result(msg)
            elif msg.get("method") == "Runtime.exceptionThrown":
                d = msg["params"]["exceptionDetails"]
                self.errors.append("exception: " + (d.get("exception", {}).get("description") or d.get("text", ""))[:300])
            elif msg.get("method") == "Runtime.consoleAPICalled" and msg["params"]["type"] in ("error", "assert"):
                self.errors.append("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in msg["params"]["args"])[:300])
            elif msg.get("method") == "Log.entryAdded" and msg["params"]["entry"]["level"] == "error":
                e = msg["params"]["entry"]
                self.errors.append(f"log: {e.get('text', '')[:200]} {e.get('url', '')[:120]}")

    async def send(self, method, params=None):
        self.n += 1
        fut = asyncio.get_event_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        msg = await asyncio.wait_for(fut, 60)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg.get("result", {})

    async def js(self, expr):
        r = await self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"].get("text"))
        return r.get("result", {}).get("value")

    async def until(self, cond, timeout=30.0):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            if await self.js(f"!!({cond})"):
                return time.perf_counter() - t0
            await asyncio.sleep(0.05)
        raise TimeoutError(cond)


A = "Alpine.$data(document.querySelector('.app'))"


async def golden(page: Page, base: str) -> list[dict]:
    steps = []

    async def step(name, action, cond, limit=LIMIT_S, timeout=30.0):
        t0 = time.perf_counter()
        await page.js(action)
        await page.until(cond, timeout)
        dt = time.perf_counter() - t0
        steps.append({"step": name, "s": round(dt, 2), "limit": limit, "ok": limit is None or dt <= limit,
                      "errors_so_far": len(page.errors)})

    # an earlier aborted run may have left the test company in the portfolio
    try:
        urllib.request.urlopen(urllib.request.Request(f"{base}/api/v2/portfolio/HAVELLS", method="DELETE"), timeout=30)
    except Exception:  # noqa: BLE001 - not there: nothing to clean
        pass
    await page.send("Page.navigate", {"url": f"{base}/?theme=dark#today"})
    await page.until("window.Alpine && document.querySelector('.app') && " + A + ".digest", 30)
    # first load in a fresh browser installs the service worker and fetches fonts; Lighthouse measures that load.
    # The per-step limits are for interactions, so let the first load settle before timing them.
    await page.js("Promise.all([navigator.serviceWorker ? navigator.serviceWorker.ready : null, document.fonts.ready])"
                  ".then(() => new Promise(r => setTimeout(r, 1500)))")
    await page.js(A + ".setWindow('30d')")
    await page.until(A + ".digest && " + A + ".digest.window.preset === '30d'", 30)
    await step("Today", "location.hash = '#holdings'; location.hash = '#today'", A + ".route === 'today' && " + A + ".digest")
    await step("Results card", A + ".openCardSheet(" + A + ".digest.attention.find(c => c.type === 'results' && c.payload && c.payload.ok))",
               A + ".cardDetail && document.querySelector('.sheet')")
    await step("Evidence", "(() => { const c = " + A + ".cardDetail, r = c.payload.table.find(r => r.found && r.page); "
               + A + ".openEvidence(" + A + ".docOf(c), r.page, r.quote); })()", A + ".evidence && (" + A + ".evidence.text || " + A + ".evidence.error)")
    await step("Why am I seeing this", A + ".sheet = 'card'; " + A + ".openWhy(" + A + ".cardDetail)", A + ".sheet === 'why' && document.querySelector('.factor')")
    # a review must stick: the undo toast once called its own undo on every re-render (a function-valued x-show)
    await page.js(A + ".sheet = null")
    cid = await page.js(A + ".digest.attention.find(c => !c.reviewed).id")
    await step("Mark reviewed", f"{A}.review({A}.digest.attention.find(c => c.id === {cid}))",
               f"{A}.digest.attention.find(c => c.id === {cid}).reviewed")
    await asyncio.sleep(1.5)
    stuck = await page.js(f"{A}.digest.attention.find(c => c.id === {cid}).reviewed")
    server = await page.js(f"fetch('/api/v2/cards/{cid}').then(r => r.json()).then(c => c.reviewed)")
    steps.append({"step": "Review still applied 1.5 s later (page and server)", "s": 1.5, "limit": None,
                  "ok": bool(stuck and server), **({} if stuck and server else {"error": f"page {stuck}, server {server}"})})
    await page.js(f"{A}.review({A}.digest.attention.find(c => c.id === {cid}), true)")     # leave the data as found
    await step("Holding", A + ".sheet = null; location.hash = '#holding/POLYCAB'",
               A + ".holding && " + A + ".holding.symbol === 'POLYCAB' && " + A + ".hc && document.querySelector('.chartbox')")
    await step("Research", "location.hash = '#research/POLYCAB'", A + ".rq.report && " + A + ".rc")
    for rid in ("results", "cash_quality", "balance_sheet", "language_diff", "hedge_meter", "dodge_detector", "worry_map",
                "fine_print", "peer_compare"):
        await step(f"Deep dive: {rid}", A + f".rtab = 'deep'; {A}.openDeep('{rid}')", A + ".deep.data || " + A + ".deep.error")
        err = await page.js(A + ".deep.error")
        if err:
            steps[-1].update(ok=False, error=err)
    await step("Copilot opens", A + ".openCopilot()", A + ".sheet === 'copilot' && document.querySelector('#cpq')")
    t0 = time.perf_counter()
    await page.js(A + ".askCopilot('How credible has POLYCAB management been?')")
    await page.until(A + ".cp.answer || " + A + ".cp.error", 120)
    ans_err = await page.js(A + ".cp.error")
    steps.append({"step": "Copilot answer (model call; time reported, not limited)", "s": round(time.perf_counter() - t0, 2),
                  "limit": None, "ok": not ans_err, **({"error": ans_err} if ans_err else {})})
    await step("Alerts", A + ".sheet = null; location.hash = '#alerts'", A + ".route === 'alerts' && " + A + ".alerts")
    await step("Settings", "location.hash = '#settings'", A + ".route === 'settings' && " + A + ".rules && " + A + ".holdings && " + A + ".dataHealth")
    await step("Add a company", A + ".pf.symbol = 'HAVELLS'; " + A + ".addHolding()", A + ".pf.job && " + A + ".pf.job.symbol === 'HAVELLS'")
    # the ticker check and onboarding run in the background; a user can remove it once it is in the list
    await step("Onboarding: ticker checked, in the portfolio (background, not limited)",
               "(async () => { for (let k = 0; k < 120; k++) { await " + A + ".loadHoldings(); "
               "if ((" + A + ".holdings || []).some(h => h.symbol === 'HAVELLS') || " + A + ".pf.job.error) return; "
               "await new Promise(r => setTimeout(r, 500)); } })()",
               "(" + A + ".holdings || []).some(h => h.symbol === 'HAVELLS') || " + A + ".pf.job.error", limit=None, timeout=90)
    await page.js("window.confirm = () => true")
    await step("Remove it again", "(" + A + ".holdings || []).find(h => h.symbol === 'HAVELLS') && " + A + ".removeHolding("
               + A + ".holdings.find(h => h.symbol === 'HAVELLS'))", "!(" + A + ".holdings || []).some(h => h.symbol === 'HAVELLS')")
    await step("Removal stops onboarding (background, not limited)", "void 0",
               A + ".pf.job && " + A + ".pf.job.done", limit=None, timeout=120)
    chart_ms = await page.js("window.__finnChartMs || 0")
    steps.append({"step": "Slowest chart render", "s": round(chart_ms / 1000, 4), "limit": 0.05, "ok": chart_ms <= 50})
    return steps


async def one_run(chrome: str, base: str, port: int) -> dict:
    prof = tempfile.mkdtemp(prefix="finn-e2e-")
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", f"--remote-debugging-port={port}",
                             f"--user-data-dir={prof}", "--no-first-run", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
                url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.1)
        async with websockets.connect(url, max_size=2 ** 26) as ws:
            page = Page(ws)
            pump = asyncio.create_task(page.pump())
            for m in ("Runtime.enable", "Log.enable", "Page.enable"):
                await page.send(m)
            await page.send("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
            try:
                steps = await golden(page, base)
            except Exception as e:  # noqa: BLE001
                steps = [{"step": "aborted", "ok": False, "error": f"{type(e).__name__}: {e}"}]
            pump.cancel()
            return {"steps": steps, "errors": page.errors,
                    "ok": all(s["ok"] for s in steps) and not page.errors}
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        shutil.rmtree(prof, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--chrome", default=next((p for p in CHROME_PATHS if os.path.exists(p)), "chrome"))
    ap.add_argument("--json", help="write the full results here")
    a = ap.parse_args()
    results = []
    for i in range(a.runs):
        r = asyncio.run(one_run(a.chrome, a.base, 9440 + i))
        results.append(r)
        worst = [s for s in r["steps"] if not s["ok"]]
        timed = [s for s in r["steps"] if s.get("limit") == LIMIT_S]
        print(f"run {i + 1}: {'PASS' if r['ok'] else 'FAIL'}  slowest limited step "
              f"{max((s['s'] for s in timed), default=0):.2f} s  console/exception errors {len(r['errors'])}")
        for s in worst:
            print("   ✗", s)
        for e in r["errors"]:
            print("   ✗", e)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1)
    passed = sum(r["ok"] for r in results)
    print(f"{passed} of {len(results)} runs passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
