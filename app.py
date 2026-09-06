import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from flask import Flask, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "wood_press.db")
SOURCES = json.load(open(os.path.join(BASE, "sources.json"), encoding="utf-8"))
HEADERS = {"User-Agent": "WoodPress/0.3.2 (+news aggregator)"}
app = Flask(__name__, static_folder=BASE)

refresh_state = {
    "running": False,
    "started": None,
    "finished": None,
    "added": 0,
    "error": None,
    "duration_seconds": 0,
}
refresh_lock = threading.Lock()


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS articles (
      id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      summary TEXT,
      url TEXT NOT NULL UNIQUE,
      source TEXT,
      country TEXT,
      published TEXT,
      categories TEXT,
      added TEXT
    )""")
    c.commit()
    return c


def clean(s):
    return re.sub(r"\s+", " ", BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)).strip()


def parse_date(entry):
    for key in ("published", "updated", "created"):
        v = entry.get(key)
        if v:
            try:
                d = dateparser.parse(v)
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                return d.astimezone(timezone.utc)
            except Exception:
                pass
    for key in ("published_parsed", "updated_parsed"):
        v = entry.get(key)
        if v:
            try:
                return datetime(*v[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def categories(title, summary, country):
    text = (title + " " + summary).lower()
    out = []
    for cat, words in SOURCES["keywords"].items():
        if any(w.lower() in text for w in words):
            out.append(cat)
    if country == "PL":
        out.append("Polska")
    if country == "DE":
        out.append("Niemcy")
    return list(dict.fromkeys(out)) or (["Polska"] if country == "PL" else ["Niemcy"])


def original_url(url):
    """Resolve only Google News redirect links.

    Direct publisher URLs are returned untouched. This avoids an extra HTTP request
    for every normal article and makes refresh much faster.
    """
    if not url:
        return url
    try:
        host = (urlparse(url).hostname or "").lower()
        if host not in {"news.google.com", "www.news.google.com"}:
            return url
        r = requests.get(url, headers=HEADERS, timeout=3, allow_redirects=True, stream=True)
        return r.url
    except requests.RequestException:
        return url


def save_article(title, summary, url, source, country, published):
    if not title or not url:
        return False
    url = original_url(url)
    aid = hashlib.sha256(url.encode()).hexdigest()[:24]
    cats = categories(title, summary, country)
    c = db()
    try:
        cur = c.execute("""INSERT OR IGNORE INTO articles
          (id,title,summary,url,source,country,published,categories,added)
          VALUES (?,?,?,?,?,?,?,?,?)""", (
            aid, clean(title), clean(summary)[:900], url, source, country,
            published.isoformat() if published else None,
            json.dumps(cats, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
        ))
        c.commit()
        return cur.rowcount == 1
    finally:
        c.close()


def fetch_one_rss(q):
    added = 0
    url = (
        "https://news.google.com/rss/search?q=" + quote(q["query"] + " when:1d") +
        "&hl=" + q["lang"] +
        "&gl=" + ("PL" if q["country"] == "PL" else "DE") +
        "&ceid=" + ("PL:pl" if q["country"] == "PL" else "DE:de")
    )
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        # Keep refresh bounded. Google News feeds normally contain a small set of recent results.
        for e in feed.entries[:15]:
            d = parse_date(e)
            # Strict 24h mode: an item without a trustworthy publication date is
            # not allowed into the feed. Also reject stale/future timestamps.
            if d is None or d < cutoff or d > datetime.now(timezone.utc) + timedelta(hours=2):
                continue
            title = clean(e.get("title", ""))
            summary = clean(e.get("summary", ""))
            source = e.get("source", {}).get("title") if isinstance(e.get("source"), dict) else None
            if save_article(title, summary, e.get("link"), source or q["name"], q["country"], d):
                added += 1
    except Exception as ex:
        print("RSS", q["name"], repr(ex), flush=True)
    return added


def fetch_rss():
    total = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(fetch_one_rss, q) for q in SOURCES["rss_queries"]]
        for f in as_completed(futures):
            total += f.result()
    return total


def fetch_one_web(s):
    added = 0
    try:
        r = requests.get(s["url"], headers=HEADERS, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        page_context = clean(soup.title.get_text(" ") if soup.title else "")
        keywords = sum(SOURCES["keywords"].values(), [])
        for a in soup.find_all("a", href=True):
            title = clean(a.get_text(" ", strip=True))
            if len(title) < 25 or len(title) > 220:
                continue
            if not any(w.lower() in (title + " " + page_context).lower() for w in keywords):
                continue
            u = urljoin(s["url"], a["href"])
            if u.startswith(("javascript:", "mailto:", "#")) or u == s["url"]:
                continue
            if save_article(title, "", u, s["name"], s["country"], None):
                added += 1
    except Exception as ex:
        print("WEB", s["name"], repr(ex), flush=True)
    return added


def fetch_web_pages():
    """Direct homepage scraping is intentionally disabled in strict 24h mode.

    A homepage often contains archive links from 2018/2022/etc. and usually does
    not expose a reliable publication date next to every link. Treating the time
    we discovered a link as its publication time was the cause of old articles
    appearing as fresh. Source-specific Google News queries in sources.json cover
    these publishers without inventing dates.
    """
    return 0


def cleanup_legacy_articles():
    """Remove legacy undated rows created by versions <=0.3.1."""
    c = db()
    try:
        c.execute("DELETE FROM articles WHERE published IS NULL OR published = ''")
        c.commit()
    finally:
        c.close()


def refresh_engine():
    started = time.monotonic()
    cleanup_legacy_articles()
    added = fetch_rss() + fetch_web_pages()
    return {
        "added": added,
        "updated": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 1),
    }


def run_refresh_background():
    global refresh_state
    try:
        result = refresh_engine()
        with refresh_lock:
            refresh_state.update({
                "running": False,
                "finished": datetime.now(timezone.utc).isoformat(),
                "added": result["added"],
                "error": None,
                "duration_seconds": result["duration_seconds"],
            })
    except Exception as ex:
        print("REFRESH", repr(ex), flush=True)
        with refresh_lock:
            refresh_state.update({
                "running": False,
                "finished": datetime.now(timezone.utc).isoformat(),
                "error": str(ex),
            })


@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "version": "0.3.2", "refresh_running": refresh_state["running"]})


@app.get("/api/news")
def news():
    try:
        hours = min(max(int(request.args.get("hours", 24)), 1), 168)
    except ValueError:
        hours = 24
    cat = request.args.get("category", "Wszystkie")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    c = db()
    rows = c.execute("SELECT * FROM articles ORDER BY COALESCE(published,added) DESC LIMIT 1000").fetchall()
    c.close()
    out = []
    for r in rows:
        dt = r["published"]
        if not dt:
            continue
        try:
            d = dateparser.parse(dt)
            d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if d < cutoff:
            continue
        cats = json.loads(r["categories"])
        if cat != "Wszystkie" and cat not in cats:
            continue
        out.append({**dict(r), "categories": cats})
    return jsonify({
        "updated": datetime.now(timezone.utc).isoformat(),
        "count": len(out),
        "items": out,
        "refresh_running": refresh_state["running"],
    })


@app.get("/api/refresh/status")
def refresh_status():
    with refresh_lock:
        return jsonify(dict(refresh_state))


@app.post("/api/refresh")
def api_refresh():
    global refresh_state
    with refresh_lock:
        if refresh_state["running"]:
            return jsonify({"started": False, "running": True}), 202
        refresh_state = {
            "running": True,
            "started": datetime.now(timezone.utc).isoformat(),
            "finished": None,
            "added": 0,
            "error": None,
            "duration_seconds": 0,
        }
        threading.Thread(target=run_refresh_background, daemon=True).start()
    return jsonify({"started": True, "running": True}), 202


if __name__ == "__main__":
    db()
    print("Wood Press 0.3.2 — http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
