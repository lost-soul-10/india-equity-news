from zoneinfo import ZoneInfo

import re
import html

def clean_text(raw: str) -> str:
    if not raw:
        return ""
    # Decode HTML entities (&nbsp;, &amp;, etc.)
    text = html.unescape(raw)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Normalize whitespace
    return " ".join(text.split())

# app.py
import time
import threading
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser
from flask import Flask, jsonify, render_template_string, request

DB_PATH = "news.db"
POLL_SECONDS = 60

# RSS FEEDS
NSE_CORPORATE_ACTIONS = "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml"
LIVEMINT_MARKETS = "https://www.livemint.com/rss/markets"
ECONOMIC_TIMES_MARKETS = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"

def google_news_rss(query: str, hl="en-IN", gl="IN", ceid="IN:en") -> str:
    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + f"&hl={hl}&gl={gl}&ceid={quote_plus(ceid)}"
    )

GOOGLE_NEWS_RSS = google_news_rss(
    '(NSE OR BSE OR "bonus issue" OR dividend OR buyback OR "stock split" OR rights) '
    '-crypto -bitcoin -ethereum when:2d'
)

FEEDS = [
    ("NSE Corporate Actions (Official)", NSE_CORPORATE_ACTIONS),
    ("LiveMint Markets", LIVEMINT_MARKETS),
    ("Economic Times Markets", ECONOMIC_TIMES_MARKETS),
    ("Google News (India equities)", GOOGLE_NEWS_RSS),
]

INCLUDE_KEYWORDS = [
    "nse", "bse", "nifty", "sensex",
    "dividend", "buyback", "split", "bonus", "rights",
    "ipo", "results", "earnings", "shares", "stock", "equity",
]
EXCLUDE_KEYWORDS = ["crypto", "bitcoin", "ethereum"]

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

# tiny in-memory status so you can see what's happening
STATUS = {
    "last_run": None,
    "per_source": {},
    "last_error": None,
}

def passes_filter(title: str) -> bool:
    t = (title or "").lower()
    if any(x in t for x in EXCLUDE_KEYWORDS):
        return False
    return any(k in t for k in INCLUDE_KEYWORDS)

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            source TEXT NOT NULL,
            summary TEXT,
            published_ts INTEGER,
            fetched_ts INTEGER NOT NULL,
            UNIQUE(title, source)
        )
    """)
    con.commit()
    con.close()

def to_ts(st):
    try:
        return int(time.mktime(st)) if st else None
    except:
        return None

IST = ZoneInfo("Asia/Kolkata")

def fmt_ts(ts):
    if not ts:
        return None
    return (
        datetime
        .fromtimestamp(ts, tz=timezone.utc)
        .astimezone(IST)
        .strftime("%d %b %Y, %I:%M %p IST")
    )

def fetch_once():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    now = int(time.time())

    inserted_total = 0
    per_source = {}

    for source, url in FEEDS:
        inserted = 0
        try:
            feed = feedparser.parse(url, request_headers=REQUEST_HEADERS)
            # If parsing fails, bozo=1 and bozo_exception tells why
            if getattr(feed, "bozo", 0):
                per_source[source] = f"bozo: {type(getattr(feed, 'bozo_exception', None)).__name__}"
                continue

            for e in feed.entries[:50]:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                summary = clean_text(e.get("summary") or e.get("description") or "")

                if not title:
                    continue

                # NSE = always include
                if source != "NSE Corporate Actions (Official)":
                    if not passes_filter(title):
                        continue

                pub = to_ts(e.get("published_parsed")) or to_ts(e.get("updated_parsed"))

                cur.execute("""
                    INSERT OR IGNORE INTO items
                    (title, link, source, summary, published_ts, fetched_ts)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (title, link, source, summary, pub, now))

                if cur.rowcount == 1:
                    inserted += 1
                    inserted_total += 1

            per_source[source] = f"ok (+{inserted})"

        except Exception as ex:
            per_source[source] = f"error: {type(ex).__name__}"
            STATUS["last_error"] = f"{source}: {repr(ex)}"

    con.commit()
    con.close()

    STATUS["last_run"] = fmt_ts(now)
    STATUS["per_source"] = per_source
    if inserted_total == 0 and not STATUS["last_error"]:
        STATUS["last_error"] = "No new items inserted (may be empty feeds or blocked requests)."

def poller():
    while True:
        try:
            fetch_once()
        except Exception as ex:
            STATUS["last_error"] = f"poller: {repr(ex)}"
        time.sleep(POLL_SECONDS)

app = Flask(__name__)
init_db()
threading.Thread(target=poller, daemon=True).start()

PAGE = """
<!doctype html>
<html>
<head>
  <title>Indian Equity Markets</title>
  <meta http-equiv="refresh" content="60">
  <style>
    body { font-family: system-ui; margin: 24px; max-width: 1000px; }
    .row { display:flex; gap:10px; margin-bottom:12px; flex-wrap:wrap; }
    input, select, button { padding:8px; font-size:14px; }
    .status { background:#f6f6f6; padding:12px; border:1px solid #ddd; border-radius:10px; margin: 12px 0 18px; font-size: 13px; }
    .item { padding:14px 0; border-bottom:1px solid #ddd; }
    .src { font-size:12px; color:#555; margin-top:4px; }
    .sum { font-size:13px; color:#333; margin-top:6px; }
    a { text-decoration:none; color:#111; }
    a:hover { text-decoration:underline; }
  </style>
</head>
<body>

<h2>Indian Equity Markets</h2>

<form class="row" method="get" action="/">
  <input name="q" value="{{q}}" placeholder="Keyword (e.g., dividend, bonus, rights, buyback, reliance)" size="55">
  <select name="src">
    <option value="">All sources</option>
    {% for s in sources %}
      <option value="{{s}}" {% if s == src %}selected{% endif %}>{{s}}</option>
    {% endfor %}
  </select>
  <button type="submit">Apply</button>
  <a href="/" style="padding:8px 0; display:inline-block;">Reset</a>
</form>

<div class="status">
  <div><b>Status</b></div>
  <div>Last fetch: {{status.last_run}}</div>
  <div>Per source:</div>
  <ul>
    {% for k,v in status.per_source.items() %}
      <li>{{k}} — {{v}}</li>
    {% endfor %}
  </ul>
  {% if status.last_error %}
    <div><b>Last error:</b> {{status.last_error}}</div>
  {% endif %}
</div>

{% if not items %}
  <p>No items yet. Leave it running 1–2 minutes, then refresh.</p>
{% endif %}

{% for i in items %}
  <div class="item">
    <div>
      <a href="{{i.link}}" target="_blank" rel="noopener noreferrer">{{i.title}}</a>
    </div>
    {% if i.summary %}
      <div class="sum">{{i.summary}}</div>
    {% endif %}
    <div class="src">
      {{i.source}}{% if i.published %} • {{i.published}}{% endif %}
    </div>
  </div>
{% endfor %}

</body>
</html>
"""

@app.route("/")
def home():
    q = (request.args.get("q") or "").strip().lower()
    src = (request.args.get("src") or "").strip()

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    base = "SELECT title, link, source, summary, published_ts, fetched_ts FROM items"
    where = []
    params = []

    if q:
        where.append("(lower(title) LIKE ? OR lower(summary) LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])

    if src:
        where.append("source = ?")
        params.append(src)

    if where:
        base += " WHERE " + " AND ".join(where)

    base += " ORDER BY COALESCE(published_ts, fetched_ts) DESC LIMIT 150"

    cur.execute(base, params)
    rows = cur.fetchall()

    cur.execute("SELECT DISTINCT source FROM items ORDER BY source")
    sources = [r[0] for r in cur.fetchall()]
    con.close()

    items = [{
        "title": r[0],
        "link": r[1],
        "source": r[2],
        "summary": (r[3] or "").strip(),
        "published": fmt_ts(r[4] or r[5]),
    } for r in rows]

    return render_template_string(PAGE, items=items, sources=sources, q=q, src=src, status=STATUS)

@app.get("/api/news")
def api_news():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT title, link, source, summary, published_ts, fetched_ts
        FROM items
        ORDER BY COALESCE(published_ts, fetched_ts) DESC
        LIMIT 250
    """)
    rows = cur.fetchall()
    con.close()
    return jsonify([{
        "title": r[0], "link": r[1], "source": r[2],
        "summary": r[3], "published_ts": r[4], "fetched_ts": r[5]
    } for r in rows])

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)

