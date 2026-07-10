#!/usr/bin/env python3
"""
WC2026 M102 ticket watcher — vivalafifa (FIFA LMS + resale) + StubHub.

Each run:
  1. Renders the vivalafifa match/LMS/resale pages + StubHub event page (headless Chromium)
  2. Extracts listings from captured JSON APIs and rendered text
  3. Applies the alert rules below
  4. Rebuilds the DEAL BOARD (index.html) with ONLY currently-qualifying deals
     — sold/delisted/inflated listings drop off automatically
  5. Pushes ntfy alerts for NEW deals only (no repeat spam; a deal that vanishes
     and later returns re-pings after REPING_HOURS)

Alert rules — identical for FIFA resale and StubHub:
  FIFA LMS .................... price <  $3,500               -> ALERT (urgent < $3,000)
  Section 100-299 ............. price <= $3,500               -> ALERT (urgent <= $3,000)
  Section 300+ ................ price <  $3,000 AND row <= 12 -> ALERT
  FIFA "Cat 1/2", no section .. price <= $3,500               -> ALERT + "verify section"

Env vars:
  NTFY_TOPIC   (required for pushes; empty = dry-run to stdout)
  PAGES_URL    (deal board URL; notification click target)
  MATCH_ID=M102  STUBHUB_URL=<event url or empty to disable>
  LMS_MAX=3500 LMS_HOT=3000 RESALE_MAX=3500 RESALE_HOT=3000
  UPPER_MAX=3000 UPPER_ROW_MAX=12 REPING_HOURS=2
  STATE_FILE=state.json BOARD_FILE=index.html DEBUG_DIR=debug
  FAKE_DATA_FILE=<fixtures json, testing only>

Usage:  python3 tracker.py           # one scan
        python3 tracker.py --test    # send a test push
"""
from __future__ import annotations

import asyncio
import hashlib
import html as html_mod
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

# ----------------------------------------------------------------- config ---
BASE = "https://www.vivalafifa.com"
MATCH_ID = os.environ.get("MATCH_ID", "M102").strip()
URLS = {
    "match":  f"{BASE}/wc/2026/match/{MATCH_ID}",
    "lms":    f"{BASE}/wc/2026/tickets/lms",
    "resale": f"{BASE}/wc/2026/tickets/resale",
}
STUBHUB_URL = os.environ.get(
    "STUBHUB_URL",
    "https://www.stubhub.com/world-cup-atlanta-tickets-7-15-2026/event/153023901/",
).strip()
FIFA_FALLBACK = "https://www.fifa.com/en/tickets"
MATCH_UTC = datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc)   # 15:00 ET kickoff
IST = timezone(timedelta(hours=5, minutes=30))

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
PAGES_URL  = os.environ.get("PAGES_URL", "").strip()

LMS_MAX       = float(os.environ.get("LMS_MAX", 3500))
LMS_HOT       = float(os.environ.get("LMS_HOT", 3000))
RESALE_MAX    = float(os.environ.get("RESALE_MAX", 3500))
RESALE_HOT    = float(os.environ.get("RESALE_HOT", 3000))
UPPER_MAX     = float(os.environ.get("UPPER_MAX", 3000))
UPPER_ROW_MAX = int(os.environ.get("UPPER_ROW_MAX", 12))
REPING_HOURS  = float(os.environ.get("REPING_HOURS", 2))
MAX_PUSHES    = int(os.environ.get("MAX_PUSHES", 5))
HEARTBEAT_UTC_HOUR = int(os.environ.get("HEARTBEAT_UTC_HOUR", 3))  # 08:30 IST

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
BOARD_FILE = Path(os.environ.get("BOARD_FILE", "index.html"))
DEBUG_DIR  = Path(os.environ.get("DEBUG_DIR", "debug"))
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

PLATFORM_LABEL = {"lms": "FIFA LMS", "resale": "FIFA Resale", "stubhub": "StubHub"}

# ------------------------------------------------------------- extraction ---
PRICE_KEYS = {"price", "amount", "value", "best", "bestprice", "best_price",
              "minprice", "min_price", "usd", "priceusd", "price_usd", "total",
              "currentprice", "listingprice", "displayprice", "totalprice",
              "allinprice", "buyerprice", "buyerseesprice"}
SEC_KEYS   = {"section", "sec", "block", "blk", "sectionname"}
ROW_KEYS   = {"row", "rownumber", "row_number"}
CAT_KEYS   = {"category", "cat", "ticketcategory", "ticket_category"}
MATCH_KEYS = {"match", "matchid", "match_id", "matchnumber", "match_number",
              "mid", "game", "gameid", "game_id"}
URL_KEYS   = {"url", "link", "href", "buyurl", "buy_url", "fifaurl", "fifa_url",
              "deeplink", "deep_link", "purchaseurl", "purchase_url",
              "buylink", "buy_link"}

PRICE_RE = re.compile(r"\$\s?([\d][\d,]*(?:\.\d{1,2})?)")
SEC_RE   = re.compile(r"\b(?:sec(?:tion)?|blk|block)\.?\s*#?\s*(\d{3})\b", re.I)
ROW_RE   = re.compile(r"\brow\s*#?\s*(\d{1,3})\b", re.I)
CAT_RE   = re.compile(r"\bcat(?:egory)?\.?\s*_?\s*([1-4])\b", re.I)
MATCH_RE = re.compile(rf"\bM?{re.escape(MATCH_ID.lstrip('M'))}\b")


def coerce_price(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, dict):                 # e.g. {"amount": 2850, "currency": "USD"}
        for kk in v:
            if kk.lower() in ("amount", "value", "price", "total", "amt"):
                return coerce_price(v[kk])
        return None
    if isinstance(v, (int, float)):
        p = float(v)
        return p if 20 <= p <= 100_000 else None
    if isinstance(v, str):
        m = PRICE_RE.search(v) or re.search(r"([\d][\d,]*(?:\.\d{1,2})?)", v)
        if m:
            try:
                p = float(m.group(1).replace(",", ""))
                return p if 20 <= p <= 100_000 else None
            except ValueError:
                pass
    return None


def coerce_int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"(\d{1,4})", v)
        if m:
            return int(m.group(1))
    return None


def walk_json(obj, out: list, url: str, page_name: str, inherited_match=None):
    if isinstance(obj, dict):
        lk = {k.lower().replace("-", "").replace("_", ""): k for k in obj}

        this_match = inherited_match
        for mk in MATCH_KEYS:
            k = mk.replace("_", "")
            if k in lk:
                this_match = str(obj[lk[k]])
                break

        price = None
        for pk in PRICE_KEYS:
            k = pk.replace("_", "")
            if k in lk:
                price = coerce_price(obj[lk[k]])
                if price is not None:
                    break

        has_ctx = any(sk in lk for sk in
                      {s.replace("_", "") for s in (SEC_KEYS | ROW_KEYS | CAT_KEYS)})

        if price is not None and has_ctx:
            listing = {"price": price, "section": None, "row": None,
                       "category": None, "url": None, "match": this_match,
                       "page": page_name, "from": url,
                       "raw": json.dumps(obj, default=str)[:400]}
            for sk in SEC_KEYS:
                k = sk.replace("_", "")
                if k in lk:
                    listing["section"] = coerce_int(obj[lk[k]])
                    break
            for rk in ROW_KEYS:
                k = rk.replace("_", "")
                if k in lk:
                    listing["row"] = coerce_int(obj[lk[k]])
                    break
            for ck in CAT_KEYS:
                k = ck.replace("_", "")
                if k in lk:
                    c = coerce_int(obj[lk[k]])
                    listing["category"] = c if c in (1, 2, 3, 4) else None
                    break
            for uk in URL_KEYS:
                k = uk.replace("_", "")
                if k in lk and isinstance(obj[lk[k]], str) \
                        and obj[lk[k]].startswith("http"):
                    listing["url"] = obj[lk[k]]
                    break
            out.append(listing)

        for v in obj.values():
            walk_json(v, out, url, page_name, this_match)
    elif isinstance(obj, list):
        for v in obj:
            walk_json(v, out, url, page_name, inherited_match)


def parse_text(text: str, page_name: str) -> list[dict]:
    out = []
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if "$" not in ln:
            continue
        chunk = " | ".join(x for x in lines[max(0, i - 3): i + 4] if x)
        for pm in PRICE_RE.finditer(ln):
            price = coerce_price(pm.group(0))
            if price is None:
                continue
            sec = SEC_RE.search(chunk)
            row = ROW_RE.search(chunk)
            cat = CAT_RE.search(chunk)
            if not sec and row:
                sec = re.search(r"\b([1-4]\d{2})\b(?!\s*\$)", chunk)
            out.append({
                "price": price,
                "section": int(sec.group(1)) if sec else None,
                "row": int(row.group(1)) if row else None,
                "category": int(cat.group(1)) if cat else None,
                "url": None,
                "match": MATCH_ID if MATCH_RE.search(chunk) else None,
                "page": page_name, "from": f"text:{page_name}",
                "raw": chunk[:400],
            })
    return out


# ----------------------------------------------------------------- scrape ---
async def scrape():
    from playwright.async_api import async_playwright

    captured, texts, anchors = [], {}, []
    current = {"name": "match"}
    pending: list[asyncio.Task] = []

    async def grab(resp, page_name):
        try:
            if "json" not in resp.headers.get("content-type", ""):
                return
            body = await resp.json()
            captured.append((page_name, resp.url, body))
        except Exception:
            pass

    async def load(page, url):
        """Navigate with one retry."""
        for attempt in (1, 2):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                await page.wait_for_timeout(5_000)
                return True
            except Exception as e:
                if attempt == 2:
                    raise e
                await page.wait_for_timeout(3_000)
        return False

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(user_agent=UA,
                                        viewport={"width": 1280, "height": 2400})
        page = await ctx.new_page()
        page.on("response",
                lambda r: pending.append(asyncio.create_task(grab(r, current["name"]))))

        for name, url in URLS.items():
            current["name"] = name
            try:
                await load(page, url)
                texts[name] = await page.evaluate("document.body.innerText")
                try:
                    page_anchors = await page.eval_on_selector_all(
                        "a[href*='fifa.com']",
                        "els => els.map(e => {"
                        "  const root = e.closest('tr, li, article,"
                        " [class*=\"row\"], [class*=\"card\"], [class*=\"listing\"]')"
                        "    || e.parentElement || e;"
                        "  return {href: e.href,"
                        "          ctx: (root.innerText || '').slice(0, 300)};"
                        "})")
                    for a in page_anchors:
                        a["page"] = name
                    anchors.extend(page_anchors)
                except Exception:
                    pass
            except Exception as e:
                texts[name] = f"__ERROR__ {e}"

        if STUBHUB_URL:
            current["name"] = "stubhub"
            try:
                await load(page, STUBHUB_URL)
                texts["stubhub"] = await page.evaluate("document.body.innerText")
            except Exception as e:
                texts["stubhub"] = f"__ERROR__ {e}"

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await browser.close()

    listings: list[dict] = []
    for page_name, url, body in captured:
        walk_json(body, listings, url, page_name)
    for name, txt in texts.items():
        listings.extend(parse_text(txt, name))
    return listings, texts, anchors


def load_fixtures(path: str):
    data = json.loads(Path(path).read_text())
    listings: list[dict] = []
    for item in data.get("captured", []):
        walk_json(item["json"], listings, item.get("url", "fixture"),
                  item.get("page", "match"))
    texts = data.get("texts", {})
    for name, txt in texts.items():
        listings.extend(parse_text(txt, name))
    return listings, texts, data.get("anchors", [])


# ---------------------------------------------------------------- filters ---
def is_for_match(l: dict) -> bool:
    m = (l.get("match") or "")
    if m:
        return MATCH_RE.search(str(m)) is not None
    return l.get("page") in ("match", "stubhub")


def classify_source(l: dict) -> str:
    if l.get("page") == "stubhub":
        return "stubhub"
    frm = (l.get("from") or "").lower()
    if frm.startswith("http"):
        if "lms" in frm or "lastminute" in frm or "last-minute" in frm:
            return "lms"
        if "resale" in frm:
            return "resale"
    if l.get("page") == "lms":
        return "lms"
    if l.get("page") == "resale":
        return "resale"
    if l.get("section") is not None:
        return "resale"
    raw = (l.get("raw") or "").lower()
    if "resale" in raw:
        return "resale"
    return "lms"


def evaluate(l: dict) -> tuple[bool, str, bool]:
    price = l.get("price")
    if price is None:
        return False, "", False
    src = classify_source(l)
    l["source"] = src

    if src == "lms":
        if price < LMS_MAX:
            return True, "FIFA Last-Minute Sale", price < LMS_HOT
        return False, "", False

    sec, row, cat = l.get("section"), l.get("row"), l.get("category")

    if sec is not None and sec >= 300:
        if price < UPPER_MAX and row is not None and row <= UPPER_ROW_MAX:
            return True, f"Upper bowl Sec {sec}, Row {row} (front rows)", False
        return False, "", False

    if sec is not None and 100 <= sec < 300:
        if price <= RESALE_MAX:
            return True, f"Lower/mid bowl Sec {sec}" + (f", Row {row}" if row else ""), \
                   price <= RESALE_HOT
        return False, "", False

    if cat in (1, 2) and price <= RESALE_MAX:
        return True, f"Cat {cat} — section unknown, VERIFY it's not a 3xx before paying", \
               cat == 1
    return False, "", False


def find_anchor(l: dict, anchors: list[dict]) -> str | None:
    price = l.get("price")
    if price is None:
        return None
    p = int(price)
    variants = {f"{p:,}", str(p)}
    sec, row = l.get("section"), l.get("row")
    weak = None
    for a in anchors:
        ctx = a.get("ctx") or ""
        if not any(v in ctx for v in variants):
            continue
        if sec is not None and str(sec) not in ctx:
            weak = weak or a.get("href")
            continue
        if row is not None and re.search(rf"\brow\s*#?\s*{row}\b", ctx, re.I):
            return a.get("href")
        weak = a.get("href")
    return weak


def buy_url(l: dict, anchors: list[dict]) -> str:
    if l.get("url"):
        return l["url"]
    if l.get("source") == "stubhub":
        return STUBHUB_URL or FIFA_FALLBACK
    return find_anchor(l, anchors) or FIFA_FALLBACK


# ------------------------------------------------------------------ state ---
def key_of(l: dict) -> str:
    s = f"{l.get('source')}|{l.get('category')}|{l.get('section')}|{l.get('row')}|{int(l['price'])}"
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def load_state() -> dict:
    st = {"deals": {}, "last_heartbeat": "", "last_fail_ping": "", "zero_runs": 0}
    if STATE_FILE.exists():
        try:
            old = json.loads(STATE_FILE.read_text())
            st.update({k: old[k] for k in st if k in old})
            if "seen" in old and not st["deals"]:            # migrate v1 -> v2
                st["deals"] = {k: {"first": v, "last": v}
                               for k, v in old["seen"].items()}
        except Exception:
            pass
    return st


def save_state(st: dict):
    STATE_FILE.write_text(json.dumps(st, indent=1))


# ----------------------------------------------------------------- notify ---
def push(title: str, body: str, urgent: bool, click: str, buy: str | None = None):
    if not NTFY_TOPIC:
        print(f"[DRY-RUN PUSH] {'URGENT ' if urgent else ''}{title}\n"
              f"  {body}\n  click: {click}" + (f"\n  buy:   {buy}" if buy else ""))
        return
    req = Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode(), method="POST")
    req.add_header("Title", title)
    req.add_header("Priority", "urgent" if urgent else "high")
    req.add_header("Tags", "rotating_light,soccer" if urgent else "tickets,soccer")
    req.add_header("Click", click)
    actions = []
    if buy:
        actions.append(f"view, Buy now, {buy}")
    if PAGES_URL and click != PAGES_URL:
        actions.append(f"view, Deal board, {PAGES_URL}")
    elif PAGES_URL and buy:
        actions.append(f"view, Deal board, {PAGES_URL}")
    if actions:
        req.add_header("Actions", "; ".join(actions[:3]))
    try:
        urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"!! ntfy push failed: {e}", file=sys.stderr)


def fmt(l: dict) -> str:
    bits = [f"${l['price']:,.0f}"]
    if l.get("category"):
        bits.append(f"Cat {l['category']}")
    if l.get("section"):
        bits.append(f"Sec {l['section']}")
    if l.get("row"):
        bits.append(f"Row {l['row']}")
    return " · ".join(bits)


# ------------------------------------------------------------- deal board ---
def write_board(active: list[dict], now: datetime):
    """active: [{deal fields + 'buy' + 'reason' + 'urgent' + 'first_seen'}] sorted by price."""
    ist = now.astimezone(IST).strftime("%d %b, %I:%M %p IST")
    left = MATCH_UTC - now
    if left.total_seconds() > 0:
        d, rem = divmod(int(left.total_seconds()), 86400)
        h, m = divmod(rem // 60, 60)
        countdown = f"Kickoff in {d}d {h}h {m}m"
    else:
        countdown = "Match underway / finished"

    cards = []
    for a in active:
        badge = PLATFORM_LABEL.get(a.get("source", ""), a.get("source", "?"))
        hot = ' style="border-color:#e0a020"' if a.get("urgent") else ""
        seat = " · ".join(x for x in [
            f"Cat {a['category']}" if a.get("category") else "",
            f"Sec {a['section']}" if a.get("section") else "",
            f"Row {a['row']}" if a.get("row") else ""] if x) or "Seat details on site"
        since = ""
        if a.get("first_seen"):
            try:
                fs = datetime.fromisoformat(a["first_seen"]).astimezone(IST)
                since = f"first seen {fs.strftime('%I:%M %p IST')}"
            except Exception:
                pass
        note = html_mod.escape(a.get("reason", ""))
        cards.append(f"""
  <div class="card"{hot}>
    <div class="top"><span class="badge">{html_mod.escape(badge)}</span>
      <span class="price">${a['price']:,.0f}</span></div>
    <div class="seat">{html_mod.escape(seat)}</div>
    <div class="note">{note}{(' · ' + since) if since else ''}</div>
    <a class="buy" href="{html_mod.escape(a['buy'])}" target="_blank" rel="noopener">Buy now →</a>
  </div>""")

    body = "\n".join(cards) if cards else (
        '<div class="empty">No qualifying deals right now.<br>'
        '<small>This page prunes itself — sold, delisted or overpriced listings '
        'drop off automatically. New deals also arrive as phone alerts.</small></div>')

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>{MATCH_ID} Deal Board</title>
<style>
 body{{background:#101214;color:#e8e8e8;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
      margin:0;padding:16px;max-width:560px;margin-inline:auto}}
 h1{{font-size:20px;margin:4px 0}} .sub{{color:#9aa0a6;font-size:13px;margin-bottom:14px}}
 .stale{{display:none;background:#5c1f1f;color:#ffd7d7;padding:8px 12px;border-radius:8px;
        font-size:13px;margin-bottom:12px}}
 .card{{background:#1a1d20;border:1px solid #2a2e33;border-radius:12px;
       padding:14px;margin-bottom:12px}}
 .top{{display:flex;justify-content:space-between;align-items:center}}
 .badge{{background:#24432e;color:#8fd6a5;font-size:12px;padding:3px 8px;border-radius:6px}}
 .price{{font-size:22px;font-weight:700}}
 .seat{{font-size:16px;margin:8px 0 2px}} .note{{color:#9aa0a6;font-size:12px}}
 .buy{{display:block;text-align:center;background:#2f6b3f;color:#fff;text-decoration:none;
      padding:10px;border-radius:8px;margin-top:10px;font-weight:600}}
 .empty{{text-align:center;color:#9aa0a6;padding:48px 12px;line-height:1.6}}
 .foot{{color:#6b7075;font-size:11px;margin-top:18px;line-height:1.5}}
</style></head><body>
<h1>🎟️ {MATCH_ID} — Semifinal, Atlanta</h1>
<div class="sub">Wed Jul 15, 15:00 ET · {countdown}<br>
Updated {ist} · auto-refreshes every 60s</div>
<div class="stale" id="stale">⚠️ Data is over 30 min old — the watcher may be stuck.
Check GitHub Actions.</div>
{body}
<div class="foot">Rules: FIFA LMS &lt; $3,500 (urgent &lt; $3,000) · Sec 100–299 ≤ $3,500
(urgent ≤ $3,000) · Sec 300+ needs Row ≤ {UPPER_ROW_MAX} and &lt; $3,000.<br>
StubHub prices include fees. Always verify seat details on the seller page before paying.</div>
<script>
 var gen = new Date("{now.isoformat()}");
 if ((Date.now() - gen.getTime()) > 30*60*1000)
   document.getElementById('stale').style.display = 'block';
</script>
</body></html>"""
    BOARD_FILE.write_text(html)


# ------------------------------------------------------------------- main ---
def main():
    now = datetime.now(timezone.utc)

    if "--test" in sys.argv:
        push(f"✅ {MATCH_ID} watcher: test alert",
             f"Push pipeline works. Rules: LMS < ${LMS_MAX:,.0f} | Sec 1xx/2xx ≤ "
             f"${RESALE_MAX:,.0f} | Sec 300+ needs Row ≤ {UPPER_ROW_MAX} and "
             f"< ${UPPER_MAX:,.0f}.",
             urgent=False, click=PAGES_URL or URLS["match"],
             buy=None)
        print("Test push sent." if NTFY_TOPIC else "Set NTFY_TOPIC to actually push.")
        return

    st = load_state()

    # ---- scrape (fail-loud but never crash silently) ----
    fake = os.environ.get("FAKE_DATA_FILE")
    try:
        if fake:
            listings, texts, anchors = load_fixtures(fake)
        else:
            listings, texts, anchors = asyncio.run(scrape())
        scrape_ok = True
    except Exception as e:
        listings, texts, anchors = [], {}, []
        scrape_ok = False
        print(f"SCRAPE FAILED: {e}", file=sys.stderr)
        last = st.get("last_fail_ping") or "1970-01-01T00:00:00+00:00"
        try:
            gap_ok = (now - datetime.fromisoformat(last)) > timedelta(hours=3)
        except Exception:
            gap_ok = True
        if gap_ok:
            push(f"⚠️ {MATCH_ID} watcher: scan failed",
                 f"Error: {str(e)[:180]}\nWill keep retrying every cycle. "
                 f"If this repeats, check the GitHub Actions log.",
                 urgent=False, click=PAGES_URL or URLS["match"])
            st["last_fail_ping"] = now.isoformat()
        save_state(st)
        return          # keep previous board untouched — better stale than wrong

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    (DEBUG_DIR / "last_texts.json").write_text(json.dumps(texts, indent=1)[:2_000_000])
    (DEBUG_DIR / "last_listings.json").write_text(
        json.dumps(listings, indent=1, default=str)[:2_000_000])
    (DEBUG_DIR / "last_anchors.json").write_text(
        json.dumps(anchors, indent=1, default=str)[:1_000_000])

    mine = [l for l in listings if is_for_match(l)]

    # ---- current qualifying deals (deduped) ----
    uniq: dict[str, dict] = {}
    for l in mine:
        ok, reason, urgent = evaluate(l)
        if not ok:
            continue
        k = key_of(l)
        if k not in uniq:
            l["reason"], l["urgent"], l["buy"] = reason, urgent, buy_url(l, anchors)
            uniq[k] = l

    deals_state: dict = st.setdefault("deals", {})
    reping_gap = timedelta(hours=REPING_HOURS)
    to_notify: list[tuple[str, dict]] = []

    for k, l in uniq.items():
        rec = deals_state.get(k)
        if rec is None:
            deals_state[k] = {"first": now.isoformat(), "last": now.isoformat()}
            to_notify.append((k, l))
        else:
            try:
                gone_since = now - datetime.fromisoformat(rec["last"])
            except Exception:
                gone_since = timedelta(0)
            if gone_since > reping_gap:                 # vanished, now relisted
                rec["first"] = now.isoformat()
                to_notify.append((k, l))
            rec["last"] = now.isoformat()
        l["first_seen"] = deals_state[k]["first"]

    # prune state records not seen for 48h (keeps file tiny)
    cutoff = now - timedelta(hours=48)
    for k in list(deals_state):
        try:
            if datetime.fromisoformat(deals_state[k]["last"]) < cutoff:
                del deals_state[k]
        except Exception:
            del deals_state[k]

    # ---- board: ONLY what's qualifying right now ----
    active = sorted(uniq.values(), key=lambda x: x["price"])
    write_board(active, now)

    # ---- notifications ----
    board = PAGES_URL or URLS["match"]
    for k, l in to_notify[:MAX_PUSHES]:
        push(f"🎟️ {PLATFORM_LABEL[l['source']]}: {fmt(l)}",
             f"{l['reason']}\n{MATCH_ID} Semifinal · Atlanta · Wed Jul 15, 15:00 ET\n"
             f"Buy: {l['buy']}",
             urgent=l["urgent"], click=board, buy=l["buy"])
    overflow = to_notify[MAX_PUSHES:]
    if overflow:
        lines = "\n".join(f"• {fmt(l)} ({PLATFORM_LABEL[l['source']]})"
                          for _, l in overflow[:12])
        push(f"🎟️ {MATCH_ID}: {len(overflow)} more deals on the board", lines,
             urgent=False, click=board)

    # ---- zero-listing sanity warning (FIFA side) ----
    fifa_count = sum(1 for l in mine if l.get("page") != "stubhub")
    if fifa_count == 0:
        st["zero_runs"] = st.get("zero_runs", 0) + 1
        if st["zero_runs"] == 3:
            push(f"⚠️ {MATCH_ID} watcher: 0 FIFA listings 3 runs in a row",
                 "vivalafifa may have changed markup, or the site is down. "
                 "FIFA alerts may be missed until fixed — check debug artifacts.",
                 urgent=False, click=board)
    else:
        st["zero_runs"] = 0

    sh_count = sum(1 for l in mine if l.get("page") == "stubhub")
    if STUBHUB_URL and sh_count == 0 and not fake:
        sh_text = (texts.get("stubhub") or "").lower()
        hint = ("captcha/anti-bot page detected"
                if any(w in sh_text for w in ("captcha", "denied", "robot", "unusual"))
                else "0 listings parsed")
        print(f"WARN: StubHub {hint} — datacenter IPs are often blocked; "
              f"run locally on the Mac for StubHub coverage.", file=sys.stderr)

    # ---- daily heartbeat ----
    today = now.strftime("%Y-%m-%d")
    if now.hour == HEARTBEAT_UTC_HOUR and st.get("last_heartbeat") != today:
        cheapest = min((l["price"] for l in mine if l.get("price")), default=None)
        push(f"🫀 {MATCH_ID} watcher alive",
             f"{len(active)} deal(s) on the board. {len(mine)} listings scanned; "
             f"cheapest {'$%s' % format(cheapest, ',.0f') if cheapest else 'n/a'}.",
             urgent=False, click=board)
        st["last_heartbeat"] = today

    save_state(st)
    print(f"{stamp} | listings={len(listings)} for_{MATCH_ID}={len(mine)} "
          f"(stubhub={sh_count}) on_board={len(active)} new_alerts={len(to_notify)}")


if __name__ == "__main__":
    main()
