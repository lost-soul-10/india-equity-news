# api/index.py (FULL UPDATED BACKEND) — Region gate (IN/US/ALL) + STRICT India-only + better “split” handling
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


def parse_region_param(raw: str | None) -> str:
    """
    Accepts: IN, US, ALL (case-insensitive). Defaults to IN.
    """
    r = (raw or "IN").strip().upper()
    return r if r in ("IN", "US", "ALL") else "IN"


# ----------------------------
# Feeds (split by region)
# ----------------------------
# India feeds
NSE_ANNOUNCEMENTS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_CORPORATE_ACTIONS = "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml"
NSE_BOARD_MEETINGS = "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml"

HINDUSTAN_TIMES_BUSINESS = "https://www.hindustantimes.com/feeds/rss/business/rssfeed.xml"
NDTV_PROFIT = "https://feeds.feedburner.com/ndtvprofit-latest"
LIVEMINT_MARKETS = "https://www.livemint.com/rss/markets"
ECONOMIC_TIMES_MARKETS = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"

FEEDS_IN = [
    ("NSE Announcements", NSE_ANNOUNCEMENTS),
    ("Hindustan Times Business", HINDUSTAN_TIMES_BUSINESS),
    ("NDTV Business", NDTV_PROFIT),
    ("NSE Board Meetings", NSE_BOARD_MEETINGS),
    ("NSE Corporate Actions (Official)", NSE_CORPORATE_ACTIONS),
    ("LiveMint Markets", LIVEMINT_MARKETS),
    ("Economic Times Markets", ECONOMIC_TIMES_MARKETS),
]

# US feeds (small set; easy to expand later)
REUTERS_MARKETS = "https://www.reutersagency.com/feed/?best-topics=markets&post_type=best"


FEEDS_US = [
    ("Reuters Markets", REUTERS_MARKETS)
]


def get_feeds_for_region(region: str) -> list[tuple[str, str]]:
    if region == "US":
        return FEEDS_US
    if region == "ALL":
        return FEEDS_IN + FEEDS_US
    return FEEDS_IN


# ----------------------------
# Filtering + tagging
# ----------------------------
# IMPORTANT: remove plain "split" to avoid false positives like "iOS update splits sections".
HARD_FINANCE_KEYWORDS = [
    # India markets
    "nse", "bse", "nifty", "sensex",
    # Equities / trading language
    "stock", "stocks", "share", "shares", "equity", "equities", "mcap", "market cap",
    "rally", "selloff", "sell-off", "surge", "plunge", "falls", "fall", "drops", "drop", "jumps", "jump", "gains", "losses",
    # Corporate actions / events
    "dividend", "buyback",
    "stock split", "share split", "shares split", "split shares", "split ratio",
    "bonus", "rights issue", "ipo",
    "block deal", "bulk deal",
    # Results
    "results", "earnings", "quarter", "quarterly", "q1", "q2", "q3", "q4", "fy", "guidance",
    # Macro / rates / policy
    "rbi", "sebi", "repo", "crr", "yield", "yields", "bond", "bonds", "gsec", "gilts", "treasury", "fed",
    "inflation", "cpi", "gdp", "pmi",
    # Metals / commodities
    "commodities", "commodity",
    "metals", "mining",
    "gold", "silver", "copper", "aluminium", "aluminum", "zinc", "lead", "nickel", "tin",
    "brent", "wti", "crude", "oil", "gas",
]

SOFT_CONTEXT_KEYWORDS = [
    "india", "indian", "us", "u.s.", "usa", "global", "budget", "fiscal"
]

EXCLUDE_KEYWORDS = [
    # Crypto
    "crypto", "bitcoin", "ethereum",
    # Entertainment / box office noise
    "box office", "collection", "collections", "day 1", "day 2", "day 3", "weekend collection",
    "movie", "film", "cinema", "trailer", "teaser", "song", "songs",
    "actor", "actress", "celebrity",
    "ott", "netflix", "prime video", "hotstar", "disney+",
    "bollywood", "tollywood", "kollywood", "hollywood",
]

TAG_RULES: dict[str, list[str]] = {
    "Corporate action": ["dividend", "buyback", "stock split", "share split", "bonus", "rights issue"],
    "Results": ["results", "earnings", "quarter", "q1", "q2", "q3", "q4", "fy", "guidance"],
    "Regulatory": ["sebi", "rbi", "regulator", "circular", "compliance", "penalty", "order"],
    "Macro": ["inflation", "cpi", "gdp", "pmi", "rates", "repo", "crr", "fiscal", "budget", "yield", "yields", "treasury"],
}

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome Safari"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

FINANCE_HINT_PATTERNS = [
    r"\b₹\s?\d", r"\brs\.?\s?\d", r"\b%\b",
    r"\b(q[1-4]|fy\d{2})\b",
    r"\b(mcap|market cap)\b",
]

# ----------------------------
# Region classifier (content-based)
# ----------------------------
US_EQUITY_KEYWORDS = [
    "us stocks", "u.s. stocks", "us stock market", "u.s. stock market",
    "wall street",
    "nyse", "nasdaq", "dow", "dow jones",
    "s&p", "s&p 500", "sp500", "nasdaq 100",
    "russell 2000",
    "u.s. treasury", "us treasury", "treasury yield", "treasury yields",
    "10-year treasury", "2-year treasury", "ten-year treasury",
    "fomc",
]

INDIA_MARKET_KEYWORDS = [
    "nse", "bse", "nifty", "sensex", "sgx nifty", "gift nifty",
    "sebi", "rbi", "mcx", "ncdex",
    "dalal street",
    "rupee", "inr", "₹",
]

US_TICKER_PATTERNS = [
    r"\$[a-z]{1,5}\b",          # $aapl
    r"\b[a-z]{1,5}\.(o|n)\b",   # aapl.o / tsla.o / ibm.n
    r"\bbrk\.[ab]\b",           # brk.a / brk.b
]

US_MEGA_NAMES = [
    "apple", "microsoft", "amazon", "alphabet", "google", "meta", "nvidia", "tesla",
    "netflix", "intel", "amd", "qualcomm", "broadcom",
    "jpmorgan", "goldman", "morgan stanley",
]


def classify_item_region(title: str, summary: str) -> str:
    """
    Returns: "IN", "US", or "GLOBAL"
    """
    t = f"{title} {summary}".lower()

    # Fast-path: obvious US market phrasing
    if "us stocks" in t or "u.s. stocks" in t or "wall street" in t:
        return "US"
    if ("treasury" in t or "treasury yield" in t or "treasury yields" in t) and ("us " in t or "u.s." in t):
        return "US"

    us_score = 0
    in_score = 0

    us_score += sum(2 for k in US_EQUITY_KEYWORDS if k in t)
    in_score += sum(2 for k in INDIA_MARKET_KEYWORDS if k in t)

    if any(re.search(p, t) for p in US_TICKER_PATTERNS):
        us_score += 3

    us_score += sum(1 for n in US_MEGA_NAMES if n in t)

    if "₹" in t or "inr" in t or "rupee" in t:
        in_score += 2

    if us_score >= 4 and us_score > in_score:
        return "US"
    if in_score >= 4 and in_score >= us_score:
        return "IN"
    return "GLOBAL"


def passes_filter(text: str) -> bool:
    t = (text or "").lower()

    if any(x in t for x in EXCLUDE_KEYWORDS):
        return False

    if any(k in t for k in HARD_FINANCE_KEYWORDS):
        return True

    if any(re.search(p, t) for p in FINANCE_HINT_PATTERNS):
        return True

    return False


def tag_item(title: str, summary: str) -> list[str]:
    t = f"{title} {summary}".lower()
    tags: list[str] = []
    for tag, keys in TAG_RULES.items():
        if any(k in t for k in keys):
            tags.append(tag)
    return tags


def fetch_feed(url: str) -> feedparser.FeedParserDict | None:
    """
    feedparser bozo=1 can still have usable entries.
    Only treat as failure when entries are empty.
    """
    try:
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
        r.raise_for_status()

        feed = feedparser.parse(r.content)

        if getattr(feed, "bozo", 0) and not getattr(feed, "entries", None):
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

    sort_mode = (request.args.get("sort") or "latest").strip().lower()  # latest | relevance
    group_dupes = (request.args.get("group") or "").strip().lower() in ("1", "true", "yes", "on")

    # REGION param: IN | US | ALL (default IN)
    region = parse_region_param(request.args.get("region"))
    feeds = get_feeds_for_region(region)

    items: list[dict] = []
    feed_failures: list[str] = []

    for source_name, url in feeds:
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

            # Item-level region classification
            item_region = classify_item_region(title, summary)

            # STRICT region gate:
            # - IN shows ONLY IN
            # - US shows ONLY US
            # - ALL shows everything
            if region == "IN":
                if item_region != "IN":
                    continue
            elif region == "US":
                if item_region != "US":
                    continue
            else:
                pass

            pub_ts = (
                to_ts_utc(e.get("published_parsed"))
                or to_ts_utc(e.get("updated_parsed"))
                or 0
            )
            published = fmt_ts_ist(pub_ts)

            # Relevance scoring (simple, explainable)
            base_text = f"{title} {summary}".lower()
            score = 0

            if q:
                score += 10 * count_occurrences(base_text, q)
                if q in title.lower():
                    score += 25

            score += sum(1 for k in HARD_FINANCE_KEYWORDS if k in base_text)
            score += sum(1 for k in SOFT_CONTEXT_KEYWORDS if k in base_text)

            tags = tag_item(title, summary)

            items.append(
                {
                    "title": title,
                    "link": link,
                    "source": source_name,
                    "summary": summary,
                    "published": published,
                    "published_ts": pub_ts,
                    "score": score,
                    "tags": tags,
                    "item_region": item_region,
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
            if (it["published_ts"] or 0) > (best["published_ts"] or 0):
                it["dupe_sources"] = best.get("dupe_sources", []) + [it["source"]]
                it["dupe_count"] = best.get("dupe_count", 0) + 1
                grouped[key] = it
            else:
                best["dupe_sources"] = best.get("dupe_sources", []) + [it["source"]]
                best["dupe_count"] = best.get("dupe_count", 0) + 1

        items = list(grouped.values())

    # Sorting
    if sort_mode == "relevance":
        items.sort(key=lambda x: (x.get("score", 0), x.get("published_ts", 0)), reverse=True)
    else:
        items.sort(key=lambda x: x.get("published_ts", 0), reverse=True)

    # Output items
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
                "region": it.get("item_region"),
                "dupe_count": it.get("dupe_count", 0),
                "dupe_sources": it.get("dupe_sources", []),
            }
        )

    resp = make_response(
        jsonify(
            {
                "sources": [s for (s, _) in feeds],
                "region": region,
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
