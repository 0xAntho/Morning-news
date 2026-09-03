import os
import json
from datetime import datetime
from urllib.parse import quote

import requests
import feedparser
import yfinance as yf

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

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


def get_news(query, count=2):
    """Free Google News RSS search, no API key required."""
    try:
        rss_url = f"https://news.google.com/rss/search?q={quote(query)}&hl=fr&gl=FR&ceid=FR:fr"
        feed = feedparser.parse(rss_url)
        return [{"title": e.title, "link": e.link} for e in feed.entries[:count]]
    except Exception as e:
        print(f"[warn] news fetch failed for '{query}': {e}")
        return []


def format_message(holdings):
    lines = [f"*Recap matinal - {datetime.now().strftime('%d/%m/%Y')}*", ""]

    for h in holdings:
        ticker = h["ticker"]
        name = h.get("name", ticker)

        result = get_price_change(ticker)
        if result:
            price, pct = result
            emoji = "🟢" if pct >= 0 else "🔴"
            lines.append(f"{emoji} *{name}* ({ticker}): {price:.2f} ({pct:+.2f}%)")
        else:
            lines.append(f"⚪ *{name}* ({ticker}): donnees indisponibles")

        for n in get_news(f"{name} action", NEWS_PER_TICKER):
            lines.append(f"   📰 [{n['title']}]({n['link']})")
        lines.append("")

    lines.append("*Actualites des marches*")
    for n in get_news(GLOBAL_NEWS_QUERY, GLOBAL_NEWS_COUNT):
        lines.append(f"📰 [{n['title']}]({n['link']})")

    return "\n".join(lines)


def send_telegram_message(text):
    if len(text) > TELEGRAM_MAX_LEN:
        text = text[:TELEGRAM_MAX_LEN] + "\n\n...(message tronque)"

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def main():
    holdings = load_holdings()
    if not holdings:
        print("[warn] holdings.json is empty, nothing to send")
        return
    message = format_message(holdings)
    send_telegram_message(message)
    print("Digest sent successfully!")


if __name__ == "__main__":
    main()