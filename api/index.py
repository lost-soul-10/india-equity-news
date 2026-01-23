# app.py (updated)
from __future__ import annotations

import calendar
import html as html_lib
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
from flask import Flask, jsonify, make_response, request, send_from_directory

# ----------------------------
# Paths
# ----------------------------
BASE_DIR = Path(__file__).resolve().parents[1]  # News/
PUBLIC_DIR = BASE_DIR / "public"

app = Flask(__name__, static_folder=str(PUBLIC_DIR), static_url_path="")

IST = ZoneInfo("Asia/Kolkata")

# ----------------------------
# Routes: UI
# ----------------------------
@app.get("/")
def home():
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.get("/favicon.ico")
def favicon():
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


def normalize_title(title: str) -> str:
    t = (title or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^\w\s]", "", t)  # drop punctuation
    return t


def count_occurrences(haystack: str, needle: str) -> int:
    if not haystack or not needle:
        return 0
    return haystack.lower().count(needle.lower())


# ----------------------------
# Feeds
# ----------------------------
NSE_ANNOUNCEMENTS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_CORPORATE_ACTIONS = "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml"
NSE_BOARD_MEETINGS = "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml"

HINDUSTAN_TIMES_BUSINESS = "https://www.hindustantimes.com/feeds/rss/business/rssfeed.xml"
NDTV_PROFIT = "https://feeds.feedburner.com/ndtvprofit-latest"
LIVEMINT_MARKETS = "https://www.livemint.com/rss/markets"
ECONOMIC_TIMES_MARKETS = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"

FEEDS = [
    ("NSE Announcements", NSE_ANNOUNCEMENTS),
    ("Hindustan Times Business", HINDUSTAN_TIMES_BUSINESS),
    ("NDTV Business", NDTV_PROFIT),
    ("NSE Board Meetings", NSE_BOARD_MEETINGS),
    ("NSE Corporate Actions (Official)", NSE_CORPORATE_ACTIONS),
    ("LiveMint Markets", LIVEMINT_MARKETS),
    ("Economic Times Markets", ECONOMIC_TIMES_MARKETS),
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
    "budget",
    "rbi",
    "india",
    "management",
    "quarterly",
    "bonds",
    "bond",
    "sovereign",
    "block deal",
    "bulk deal",
]
EXCLUDE_KEYWORDS = ["crypto", "bitcoin", "ethereum"]

# Lightweight “signal” tags (80/20 keyword rules)
TAG_RULES: dict[str, list[str]] = {
    "Corporate action": ["dividend", "buyback", "split", "bonus", "rights issue"],
    "Results": ["results", "earnings", "quarter", "q1", "q2", "q3", "q4", "fy"],
    "Regulatory": ["sebi", "rbi", "regulator", "circular", "compliance", "penalty", "order"],
    "Macro": ["inflation", "cpi", "gdp", "pmi", "rates", "repo", "crr", "fiscal", "budget", "yield"],
}

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


def tag_item(title: str, summary: str) -> list[str]:
    t = f"{title} {summary}".lower()
    tags: list[str] = []
    for tag, keys in TAG_RULES.items():
        if any(k in t for k in keys):
            tags.append(tag)
    return tags


def fetch_feed(url: str) -> feedparser.FeedParserDict | None:
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

    # New params
    sort_mode = (request.args.get("sort") or "latest").strip().lower()  # latest | relevance
    group_dupes = (request.args.get("group") or "").strip().lower() in ("1", "true", "yes", "on")

    items: list[dict] = []
    feed_failures: list[str] = []

    for source_name, url in FEEDS:
        if src and src != source_name:
            continue

        feed = fetch_feed(url)
        if not feed:
            feed_failures.append(source_name)
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
            published = fmt_ts_ist(pub_ts)

            # Relevance scoring (simple, explainable)
            base_text = f"{title} {summary}"
            score = 0
            if q:
                score += 10 * count_occurrences(base_text, q)
            # small boost for “signal” keywords regardless
            score += sum(1 for k in INCLUDE_KEYWORDS if k in base_text.lower())

            tags = tag_item(title, summary)

            items.append(
                {
                    "title": title,
                    "link": link,
                    "source": source_name,
                    "summary": summary,
                    "published": published,
                    "published_ts": pub_ts or 0,
                    "score": score,
                    "tags": tags,
                    "norm_title": normalize_title(title),
                }
            )

    # Group duplicates (by normalized title)
    if group_dupes:
        grouped: dict[str, dict] = {}
        for it in items:
            key = it["norm_title"] or (it["link"] or it["title"])
            if key not in grouped:
                it["dupe_sources"] = [it["source"]]
                it["dupe_count"] = 0
                grouped[key] = it
                continue

            best = grouped[key]
            # keep newest item as the primary
            if (it["published_ts"] or 0) > (best["published_ts"] or 0):
                # carry over dupe info
                it["dupe_sources"] = best.get("dupe_sources", []) + [it["source"]]
                it["dupe_count"] = best.get("dupe_count", 0) + 1
                grouped[key] = it
            else:
                best["dupe_sources"] = best.get("dupe_sources", []) + [it["source"]]
                best["dupe_count"] = best.get("dupe_count", 0) + 1

        items = list(grouped.values())

    # Sorting
    if sort_mode == "relevance":
        # If no query, relevance is less meaningful; still works due to keyword boosts.
        items.sort(key=lambda x: (x.get("score", 0), x.get("published_ts", 0)), reverse=True)
    else:
        items.sort(key=lambda x: x.get("published_ts", 0), reverse=True)

    # Trim + drop internal fields
    out_items = []
    for it in items[:150]:
        out_items.append(
            {
                "title": it["title"],
                "link": it["link"],
                "source": it["source"],
                "summary": it["summary"],
                "published": it["published"],
                "tags": it.get("tags", []),
                "dupe_count": it.get("dupe_count", 0),
                "dupe_sources": it.get("dupe_sources", []),
            }
        )

    resp = make_response(
        jsonify(
            {
                "sources": [s for (s, _) in FEEDS],
                "count": len(items),
                "items": out_items,
                "generated_at": fmt_ts_ist(int(time.time())),
                "feed_failures": feed_failures,
                "sort": sort_mode,
                "group": group_dupes,
            }
        )
    )

    resp.headers["Cache-Control"] = "s-maxage=900, max-age=900"
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
