from __future__ import annotations

import calendar
import html as html_lib
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests
from flask import Flask, jsonify, make_response, request, send_from_directory

# ----------------------------
# Paths
# ----------------------------
BASE_DIR = Path(__file__).resolve().parents[1]  # News/
PUBLIC_DIR = BASE_DIR / "public"

# Serve static files from /public at the site root (e.g., /index.html, /styles.css)
app = Flask(__name__, static_folder=str(PUBLIC_DIR), static_url_path="")

IST = ZoneInfo("Asia/Kolkata")

# ----------------------------
# Routes: UI
# ----------------------------
@app.get("/")
def home():
    # Explicitly serve the homepage at /
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.get("/favicon.ico")
def favicon():
    # Optional: silence favicon 404 noise
    return ("", 204)

# ----------------------------
# Helpers
# ----------------------------
def clean_text(raw: str) -> str:
    if not raw:
        return ""
    text = html_lib.unescape(raw)
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(text.split())


def to_ts_utc(st) -> int | None:
    """
    feedparser gives published_parsed/updated_parsed as time.struct_time.
    Treat it as UTC safely using calendar.timegm (NOT time.mktime).
    """
    try:
        return int(calendar.timegm(st)) if st else None
    except Exception:
        return None


def fmt_ts_ist(ts: int | None) -> str | None:
    if not ts:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .astimezone(IST)
        .strftime("%d %b %Y, %I:%M %p IST")
    )

# ----------------------------
# Feeds
# ----------------------------
NSE_FINANCIAL_RESULTS = "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml"
NSE_ANNOUNCEMENTS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_CORPORATE_ACTIONS = "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml"

LIVEMINT_MARKETS = "https://www.livemint.com/rss/markets"
ECONOMIC_TIMES_MARKETS = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"


def google_news_rss(query: str, hl: str = "en-IN", gl: str = "IN", ceid: str = "IN:en") -> str:
    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + f"&hl={hl}&gl={gl}&ceid={quote_plus(ceid)}"
    )


GOOGLE_NEWS_RSS = google_news_rss(
    '(NSE OR BSE OR "bonus issue" OR dividend OR buyback OR "stock split" OR rights issue) '
    "-crypto -bitcoin -ethereum when:2d"
)

FEEDS = [
    ("NSE Financial Results", NSE_FINANCIAL_RESULTS),
    ("NSE Announcements", NSE_ANNOUNCEMENTS),
    ("NSE Corporate Actions (Official)", NSE_CORPORATE_ACTIONS),
    ("LiveMint Markets", LIVEMINT_MARKETS),
    ("Economic Times Markets", ECONOMIC_TIMES_MARKETS),
    ("Google News (India equities)", GOOGLE_NEWS_RSS),
]

INCLUDE_KEYWORDS = [
    "nse",
    "bse",
    "nifty",
    "sensex",
    "dividend",
    "buyback",
    "split",
    "bonus",
    "rights issue",
    "ipo",
    "results",
    "earnings",
    "shares",
    "stock",
    "equity",
    "equities",
    "gold",
    "silver",
    "commodities",
]
EXCLUDE_KEYWORDS = ["crypto", "bitcoin", "ethereum"]

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome Safari"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}


def passes_filter(text: str) -> bool:
    t = (text or "").lower()
    if any(x in t for x in EXCLUDE_KEYWORDS):
        return False
    return any(k in t for k in INCLUDE_KEYWORDS)


def fetch_feed(url: str) -> feedparser.FeedParserDict | None:
    """
    Fetch feed content with requests so headers/timeouts are reliable (Vercel-friendly),
    then parse with feedparser.
    """
    try:
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        if getattr(feed, "bozo", 0):
            return None
        return feed
    except Exception:
        return None

# ----------------------------
# API
# ----------------------------
@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/news")
def api_news():
    q = (request.args.get("q") or "").strip().lower()
    src = (request.args.get("src") or "").strip()

    items: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for source_name, url in FEEDS:
        if src and src != source_name:
            continue

        feed = fetch_feed(url)
        if not feed:
            continue

        for e in feed.entries[:50]:
            title = clean_text((e.get("title") or "").strip())
            link = (e.get("link") or "").strip()
            summary = clean_text(e.get("summary") or e.get("description") or "")

            if not title:
                continue

            # Always include NSE feeds; filter only non-NSE sources
            if not source_name.startswith("NSE "):
                if not passes_filter(f"{title} {summary}"):
                    continue

            # User keyword filter (simple contains)
            if q:
                hay = f"{title} {summary}".lower()
                if q not in hay:
                    continue

            pub_ts = to_ts_utc(e.get("published_parsed")) or to_ts_utc(e.get("updated_parsed"))

            # Prefer link for dedupe when available
            key = (link or title, source_name)
            if key in seen:
                continue
            seen.add(key)

            items.append(
                {
                    "title": title,
                    "link": link,
                    "source": source_name,
                    "summary": summary,
                    "published": fmt_ts_ist(pub_ts),
                    "published_ts": pub_ts,
                }
            )

    # Sort newest first (best effort)
    items.sort(key=lambda x: x["published_ts"] or 0, reverse=True)

    resp = make_response(
        jsonify(
            {
                "sources": [s for (s, _) in FEEDS],
                "count": len(items),
                "items": [{k: v for (k, v) in it.items() if k != "published_ts"} for it in items[:150]],
                "generated_at": fmt_ts_ist(int(time.time())),
            }
        )
    )

    # CDN/browser caching for 60s to reduce repeated RSS calls
    resp.headers["Cache-Control"] = "s-maxage=60, max-age=60"
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
