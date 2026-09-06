import hashlib
import html
import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from flask import Flask, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "wood_press.db")
SOURCES = json.load(open(os.path.join(BASE, "sources.json"), encoding="utf-8"))
HEADERS = {"User-Agent": "WoodPress/0.3 (+news aggregator)"}
app = Flask(__name__, static_folder=BASE)


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
    if not url:
        return url
    try:
        r = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True, stream=True)
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
        "https://news.google.com/rss/search?q=" + quote(q["query"]) +
        "&hl=" + q["lang"] +
        "&gl=" + ("PL" if q["country"] == "PL" else "DE") +
        "&ceid=" + ("PL:pl" if q["country"] == "PL" else "DE:de")
    )
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        for e in feed.entries:
            d = parse_date(e)
            if d and d < cutoff:
                continue
            title = clean(e.get("title", ""))
            summary = clean(e.get("summary", ""))
            source = e.get("source", {}).get("title") if isinstance(e.get("source"), dict) else None
            if save_article(title, summary, e.get("link"), source or q["name"], q["country"], d):
                added += 1
    except Exception as ex:
        print("RSS", q["name"], repr(ex))
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
        r = requests.get(s["url"], headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        page_context = clean(soup.title.get_text(" ") if soup.title else "")
        keywords = sum(SOURCES["keywords"].values(), [])
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
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
        print("WEB", s["name"], repr(ex))
    return added


def fetch_web_pages():
    total = 0
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch_one_web, s) for s in SOURCES["web_sources"]]
        for f in as_completed(futures):
            total += f.result()
    return total


def refresh_engine():
    started = datetime.now(timezone.utc)
    added = fetch_rss() + fetch_web_pages()
    return {"added": added, "updated": datetime.now(timezone.utc).isoformat(), "duration_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1)}


@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "version": "0.3"})


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
        dt = r["published"] or r["added"]
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
    return jsonify({"updated": datetime.now(timezone.utc).isoformat(), "count": len(out), "items": out})


@app.post("/api/refresh")
def api_refresh():
    return jsonify(refresh_engine())


if __name__ == "__main__":
    db()
    print("Wood Press 0.3 — http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
