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


def title_tokens(norm_title: str) -> set[str]:
    """
    Turn a normalized title into a bag of tokens for fuzzy de-duplication.
    We keep this simple and fast so we can safely run it across all items.
    """
    if not norm_title:
        return set()
    # Small stopword list to avoid over-weighting glue words.
    stop = {
        "the",
        "a",
        "an",
        "of",
        "to",
        "for",
        "and",
        "on",
        "in",
        "at",
        "by",
        "from",
        "with",
    }
    # Light-weight synonym / normalization so that wording
    # differences like "falls" vs "down" or "rises" vs "up"
    # still look similar for grouping purposes.
    synonyms = {
        "falls": "fall",
        "falling": "fall",
        "down": "fall",
        "drops": "fall",
        "drop": "fall",
        "declines": "fall",
        "slides": "fall",
        "rises": "rise",
        "rise": "rise",
        "up": "rise",
        "surges": "rise",
        "jumps": "rise",
        "rs": "rs",
    }

    tokens: set[str] = set()
    for tok in norm_title.split():
        if not tok or tok in stop:
            continue
        base = synonyms.get(tok, tok)
        tokens.add(base)
    return tokens


def count_occurrences(haystack: str, needle: str) -> int:
    if not haystack or not needle:
        return 0
    return haystack.lower().count(needle.lower())


# ----------------------------
# Feeds (India only)
# ----------------------------
NSE_ANNOUNCEMENTS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_CORPORATE_ACTIONS = "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml"
NSE_BOARD_MEETINGS = "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml"

LIVEMINT_MARKETS = "https://www.livemint.com/rss/markets"
ECONOMIC_TIMES_MARKETS = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"

FEEDS = [
    ("NSE Announcements", NSE_ANNOUNCEMENTS),
    ("NSE Board Meetings", NSE_BOARD_MEETINGS),
    ("NSE Corporate Actions (Official)", NSE_CORPORATE_ACTIONS),
    ("LiveMint Markets", LIVEMINT_MARKETS),
    ("Economic Times Markets", ECONOMIC_TIMES_MARKETS),
]

# ----------------------------
# Filtering + tagging
# ----------------------------
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
    "india", "indian", "global", "budget", "fiscal"
]

EXCLUDE_KEYWORDS = [
    # Crypto
    "crypto", "bitcoin", "ethereum",
    # Entertainment / box office / lifestyle / travel noise
    "box office", "collection", "collections", "day 1", "day 2", "day 3", "weekend collection",
    "movie", "film", "cinema", "trailer", "teaser", "song", "songs",
    "actor", "actress", "celebrity",
    "valentine", "valentines", "valentine's", "T20", "IPL", "ICC"
    "tourism", "tourist", "travel", "picnic spot", "weekend getaway",
    "ott", "netflix", "prime video", "hotstar", "disney+", "nandi hills"
    "bollywood", "tollywood", "kollywood", "hollywood", "neet", "jee", "exam", "entrance test", "percentile", "cutoff", "admission", "seat",
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
    r"\b₹\s?\d",              # currency
    r"\brs\.?\s?\d",          # rupees
    r"\b(q[1-4]|fy\d{2})\b",  # quarters / financial years
    r"\b(mcap|market cap)\b",
    r"\b\d+(\.\d+)?%\s?(gain|rise|fall|drop|up|down)\b",  # % tied to price moves
]



def passes_filter(text: str) -> bool:
    t = (text or "").lower()

    if any(x in t for x in EXCLUDE_KEYWORDS):
        return False

    # Strong finance words (stocks, dividend, IPO, etc.)
    hard_hit = any(k in t for k in HARD_FINANCE_KEYWORDS)

    # Softer hints like currency / % move.
    # Require at least TWO distinct hint patterns to avoid pulling in
    # random local / lifestyle stories that just mention a rupee amount once.
    hint_hits = 0
    for p in FINANCE_HINT_PATTERNS:
        if re.search(p, t):
            hint_hits += 1
            if hint_hits >= 2:
                break

    if hard_hit:
        return True

    if hint_hits >= 2:
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

    Header fix:
    - Don't send NSE referer to non-NSE sources
    """
    try:
        headers = dict(REQUEST_HEADERS)
        if "nseindia.com" not in url:
            headers.pop("Referer", None)

        r = requests.get(url, headers=headers, timeout=15)
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

            # Always include NSE feeds; filter only non-NSE sources.
            if not source_name.startswith("NSE "):
                if not passes_filter(f"{title} {summary}"):
                    continue

            # User keyword filter (simple contains)
            if q:
                hay = f"{title} {summary}".lower()
                if q not in hay:
                    continue

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
                    "norm_title": normalize_title(title),
                }
            )

    # Total items before any de-duplication
    raw_count = len(items)

    # Group duplicates (by fuzzy-normalized title)
    if group_dupes:
        groups: list[dict] = []
        group_tokens: list[set[str]] = []

        def jaccard(a: set[str], b: set[str]) -> float:
            if not a or not b:
                return 0.0
            inter = len(a & b)
            if not inter:
                return 0.0
            union = len(a | b)
            return inter / union if union else 0.0

        # Threshold tuned to aggressively group "same story" variants
        # (wording differences, Rs/₹, etc.) without collapsing genuinely
        # different companies or events. 0.5 is deliberately a bit
        # permissive so that cross-source rewrites of the same headline
        # are very likely to be grouped together.
        SIM_THRESHOLD = 0.5

        for it in items:
            tokens = title_tokens(it.get("norm_title", ""))

            # Always group identical links together as a hard duplicate.
            best_idx = None
            best_sim = 0.0
            for idx, existing in enumerate(groups):
                if it.get("link") and it["link"] == existing.get("link"):
                    best_idx = idx
                    best_sim = 1.0
                    break

                sim = jaccard(tokens, group_tokens[idx])
                if sim > best_sim:
                    best_sim = sim
                    best_idx = idx

            if best_idx is None or best_sim < SIM_THRESHOLD:
                # New group
                it["dupe_sources"] = [it["source"]]
                it["dupe_count"] = 0
                groups.append(it)
                group_tokens.append(tokens)
            else:
                # Merge into existing group and keep the most recent as representative.
                best = groups[best_idx]
                if (it.get("published_ts") or 0) > (best.get("published_ts") or 0):
                    it["dupe_sources"] = best.get("dupe_sources", []) + [it["source"]]
                    it["dupe_count"] = best.get("dupe_count", 0) + 1
                    groups[best_idx] = it
                    group_tokens[best_idx] = tokens
                else:
                    best["dupe_sources"] = best.get("dupe_sources", []) + [it["source"]]
                    best["dupe_count"] = best.get("dupe_count", 0) + 1

        items = groups

    # Sorting
    if sort_mode == "relevance":
        items.sort(key=lambda x: (x.get("score", 0), x.get("published_ts", 0)), reverse=True)
    else:
        items.sort(key=lambda x: x.get("published_ts", 0), reverse=True)

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

    total_count = raw_count
    unique_count = len(items)
    duplicates_hidden = max(total_count - unique_count, 0) if group_dupes else 0

    resp = make_response(
        jsonify(
            {
                "sources": [s for (s, _) in FEEDS],
                "count": unique_count,
                "total_count": total_count,
                "unique_count": unique_count,
                "duplicates_hidden": duplicates_hidden,
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
