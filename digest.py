import os
import json
import html
import time
import calendar
import re
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
import feedparser
import yfinance as yf

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEND_TIME = os.environ.get("SEND_TIME", "07:30")
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Paris")
NEWS_MAX_AGE_HOURS = float(os.environ.get("NEWS_MAX_AGE_HOURS", "24"))

HOLDINGS_FILE = "holdings.json"
NEWS_PER_TICKER = 2
GLOBAL_NEWS_QUERY = "CAC 40 OR bourse Paris OR marches boursiers"
GLOBAL_NEWS_COUNT = 3
TELEGRAM_MAX_LEN = 4000  # Telegram hard limit is 4096


def load_holdings():
    with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_price_change(ticker):
    """Return (last_close, pct_change_24h) or None if unavailable."""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d")
        if len(hist) < 2:
            return None
        last_close = hist["Close"].iloc[-1]
        prev_close = hist["Close"].iloc[-2]
        pct_change = (last_close - prev_close) / prev_close * 100
        return float(last_close), float(pct_change)
    except Exception as e:
        print(f"[warn] price fetch failed for {ticker}: {e}")
        return None


def _normalize_title(title):
    """Lowercase, strip punctuation/whitespace, for dedup comparison."""
    return re.sub(r"[^\w]+", "", title.lower())


def get_news(query, count=2):
    """Free Google News RSS search, no API key required.

    Google News sorts by relevance, not date, which surfaces stale or
    evergreen articles alongside today's news (sometimes the same story
    twice via different outlets). To keep results relevant we: drop
    anything older than NEWS_MAX_AGE_HOURS, sort freshest first, split the
    "Headline - Publisher" title Google returns, and dedup near-identical
    headlines before truncating to `count`.
    """
    try:
        rss_url = f"https://news.google.com/rss/search?q={quote(query)}&hl=fr&gl=FR&ceid=FR:fr"
        feed = feedparser.parse(rss_url)

        now = time.time()
        max_age = NEWS_MAX_AGE_HOURS * 3600
        fresh = []
        for e in feed.entries:
            published = getattr(e, "published_parsed", None)
            if not published:
                continue
            age = now - calendar.timegm(published)
            if 0 <= age <= max_age:
                fresh.append((age, e))
        fresh.sort(key=lambda pair: pair[0])

        seen = set()
        results = []
        for _, e in fresh:
            title = e.title
            source = e.source.get("title") if hasattr(e, "source") else None
            if source and title.endswith(f" - {source}"):
                title = title[: -len(f" - {source}")]

            key = _normalize_title(title)
            if key in seen:
                continue
            seen.add(key)

            results.append({"title": title, "link": e.link, "source": source})
            if len(results) >= count:
                break

        return results
    except Exception as e:
        print(f"[warn] news fetch failed for '{query}': {e}")
        return []


def _format_news_line(n):
    safe_title = html.escape(n["title"])
    safe_link = html.escape(n["link"], quote=True)
    line = f'📰 <a href="{safe_link}">{safe_title}</a>'
    if n["source"]:
        line += f' — <i>{html.escape(n["source"])}</i>'
    return line


def format_message(holdings):
    # Using HTML parse mode: escape any dynamic text (names, headlines) since
    # they can contain characters like & < > that break Telegram's parser,
    # and news headlines especially can't be trusted to be "safe" text.
    lines = [f"<b>Recap matinal - {datetime.now().strftime('%d/%m/%Y')}</b>", ""]

    for h in holdings:
        ticker = h["ticker"]
        name = h.get("name", ticker)
        safe_name = html.escape(name)

        result = get_price_change(ticker)
        if result:
            price, pct = result
            emoji = "🟢" if pct >= 0 else "🔴"
            lines.append(f"{emoji} <b>{safe_name}</b> ({ticker}): {price:.2f} ({pct:+.2f}%)")
        else:
            lines.append(f"⚪ <b>{safe_name}</b> ({ticker}): donnees indisponibles")

        ticker_news = get_news(f"{name} action", NEWS_PER_TICKER)
        if not ticker_news:
            lines.append("   ℹ️ pas d'actualite recente")
        for n in ticker_news:
            lines.append(f"   {_format_news_line(n)}")
        lines.append("")

    lines.append("<b>Actualites des marches</b>")
    for n in get_news(GLOBAL_NEWS_QUERY, GLOBAL_NEWS_COUNT):
        lines.append(_format_news_line(n))

    return "\n".join(lines)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    lines = text.splitlines(keepends=True)
    chunks = []
    current = ""

    for line in lines:
        if current and len(current) + len(line) > TELEGRAM_MAX_LEN:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line

    if current:
        chunks.append(current.rstrip("\n"))

    responses = []
    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            # Raise with Telegram's actual error body baked into the message,
            # so it's guaranteed to show up in the traceback (unlike a separate
            # print(), which can get lost to stdout buffering in some containers).
            raise RuntimeError(
                f"Telegram API error {resp.status_code}: {resp.text}"
            )
        responses.append(resp.json())

    return responses


def send_digest():
    holdings = load_holdings()
    if not holdings:
        print("[warn] holdings.json is empty, nothing to send")
        return
    message = format_message(holdings)
    send_telegram_message(message)
    print("Digest sent successfully!")


def main():
    tz = ZoneInfo(TIMEZONE)
    target_hour, target_minute = (int(p) for p in SEND_TIME.split(":"))
    print(f"[info] scheduler started, will send daily at {SEND_TIME} ({TIMEZONE})")

    last_sent_date = None
    while True:
        now = datetime.now(tz)
        if (
            now.hour == target_hour
            and now.minute == target_minute
            and now.date() != last_sent_date
            and now.weekday() < 5
        ):
            try:
                send_digest()
            except Exception as e:
                print(f"[error] failed to send digest: {e}")
                try:
                    send_telegram_message(f"⚠️ Digest failed: {e}")
                except Exception as e2:
                    print(f"[error] failed to send failure alert: {e2}")
            last_sent_date = now.date()
        time.sleep(30)


if __name__ == "__main__":
    main()