from __future__ import annotations

import calendar
import html as html_lib
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
from flask import Flask, jsonify, request, make_response

app = Flask(__name__)

IST = ZoneInfo("Asia/Kolkata")

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
# Feeds (your list)
# ----------------------------
NSE_DAILY_BUYBACK_REDEMPTION = "https://nsearchives.nseindia.com/content/RSS/Daily_Buyback.xml"
NSE_FINANCIAL_RESULTS = "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml"
NSE_INSIDER_TRADING = "https://nsearchives.nseindia.com/content/RSS/Insider_Trading.xml"
NSE_ANNOUNCEMENTS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
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
    '(NSE OR BSE OR "bonus issue" OR dividend OR buyback OR "stock split" OR rights issue) '
    '-crypto -bitcoin -ethereum when:2d'
)

FEEDS = [
    ("NSE Daily Buyback Redemption", NSE_DAILY_BUYBACK_REDEMPTION),
    ("NSE Financial Results", NSE_FINANCIAL_RESULTS),
    ("NSE Insider Trading", NSE_INSIDER_TRADING),
    ("NSE Announcements", NSE_ANNOUNCEMENTS),
    ("NSE Corporate Actions (Official)", NSE_CORPORATE_ACTIONS),
    ("LiveMint Markets", LIVEMINT_MARKETS),
    ("Economic Times Markets", ECONOMIC_TIMES_MARKETS),
    ("Google News (India equities)", GOOGLE_NEWS_RSS),
]

INCLUDE_KEYWORDS = [
    "nse", "bse", "nifty", "sensex",
    "dividend", "buyback", "split", "bonus", "rights issue",
    "ipo", "results", "earnings", "shares", "stock", "equity", "equities",
]
EXCLUDE_KEYWORDS = ["crypto", "bitcoin", "ethereum"]

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

def passes_filter(text: str) -> bool:
    t = (text or "").lower()
    if any(x in t for x in EXCLUDE_KEYWORDS):
        return False
    return any(k in t for k in INCLUDE_KEYWORDS)

# ----------------------------
# API
# ----------------------------
@app.get("/api/news")
def api_news():
    q = (request.args.get("q") or "").strip().lower()
    src = (request.args.get("src") or "").strip()

    items = []
    seen = set()

    for source_name, url in FEEDS:
        if src and src != source_name:
            continue

        try:
            feed = feedparser.parse(url, request_headers=REQUEST_HEADERS)
            if getattr(feed, "bozo", 0):
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

                # User keyword filter
                if q:
                    hay = f"{title} {summary}".lower()
                    if q not in hay:
                        continue

                pub_ts = (
                    to_ts_utc(e.get("published_parsed"))
                    or to_ts_utc(e.get("updated_parsed"))
                )

                key = (title, source_name)
                if key in seen:
                    continue
                seen.add(key)

                items.append({
                    "title": title,
                    "link": link,
                    "source": source_name,
                    "summary": summary,
                    "published": fmt_ts_ist(pub_ts),
                    "published_ts": pub_ts,
                })

        except Exception:
            continue

    # Sort newest first (best effort)
    items.sort(key=lambda x: x["published_ts"] or 0, reverse=True)

    resp = make_response(jsonify({
        "sources": [s for (s, _) in FEEDS],
        "count": len(items),
        "items": [
            {k: v for (k, v) in it.items() if k != "published_ts"}
            for it in items[:150]
        ],
        "generated_at": fmt_ts_ist(int(time.time())),
    }))

    # CDN/browser caching for 60s to reduce repeated RSS calls
    resp.headers["Cache-Control"] = "s-maxage=60, max-age=60"
    return resp
