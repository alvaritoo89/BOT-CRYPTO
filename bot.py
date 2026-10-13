#!/usr/bin/env python3
"""
Bot de noticias crypto: 3 noticias del día de BTC, ETH y XRP + precio de cada una.

- Noticias: RSS públicos (sin API key).
- Precios: CoinGecko (sin API key).
- Envía el resumen a Telegram y guarda docs/data.json para la web.

Variables de entorno (en GitHub: Settings > Secrets and variables > Actions):
  TELEGRAM_TOKEN    token de tu bot (BotFather)
  TELEGRAM_CHAT_ID  id del chat/canal donde quieres recibirlo
Si no están definidas, el script no envía nada pero sí genera data.json (modo prueba).
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ----------------------------------------------------------------- CONFIG
NEWS_PER_COIN = 3
MAX_AGE_HOURS = 24

COINS = {
    "BTC": {"name": "Bitcoin", "gecko": "bitcoin", "emoji": "🟠", "words": [r"bitcoin", r"btc"]},
    "ETH": {"name": "Ethereum", "gecko": "ethereum", "emoji": "🔷", "words": [r"ethereum", r"ether", r"eth"]},
    "XRP": {"name": "XRP", "gecko": "ripple", "emoji": "⚪", "words": [r"xrp", r"ripple"]},
}

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://cryptoslate.com/feed/",
    "https://bitcoinmagazine.com/.rss/full/",
]

# Palabras que suben la "importancia" de una noticia
BOOST = [
    "etf", "sec", "approval", "approved", "lawsuit", "ruling", "court", "hack", "exploit",
    "record", "all-time high", "ath", "crash", "plunge", "surge", "soars", "fed", "regulation",
    "ban", "upgrade", "halving", "whale", "billion", "institutional", "treasury",
]

UA = {"User-Agent": "Mozilla/5.0 (compatible; crypto-news-bot/1.0)"}


# ----------------------------------------------------------------- HTTP
def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ----------------------------------------------------------------- PRECIOS
def get_prices():
    ids = ",".join(c["gecko"] for c in COINS.values())
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={ids}&vs_currencies=usd,eur&include_24hr_change=true"
    )
    try:
        data = json.loads(http_get(url))
    except Exception as e:  # noqa: BLE001
        print(f"[aviso] no se pudieron obtener precios: {e}", file=sys.stderr)
        return {}
    prices = {}
    for sym, c in COINS.items():
        d = data.get(c["gecko"])
        if d:
            prices[sym] = {
                "usd": d.get("usd"),
                "eur": d.get("eur"),
                "change24h": d.get("usd_24h_change"),
            }
    return prices


# ----------------------------------------------------------------- NOTICIAS
def strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def parse_feed(xml_bytes):
    """Devuelve lista de dicts {title, link, summary, date, source}."""
    items = []
    root = ET.fromstring(xml_bytes)
    channel_title = ""
    ch = root.find("channel")
    if ch is not None:
        channel_title = (ch.findtext("title") or "").strip()
    for it in root.iter("item"):
        title = strip_tags(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        summary = strip_tags(it.findtext("description"))[:400]
        pub = it.findtext("pubDate")
        try:
            date = parsedate_to_datetime(pub) if pub else None
            if date and date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            date = None
        if title and link:
            items.append(
                {"title": title, "link": link, "summary": summary, "date": date, "source": channel_title}
            )
    return items


def fetch_all_news():
    all_items = []
    for url in FEEDS:
        try:
            all_items.extend(parse_feed(http_get(url)))
        except Exception as e:  # noqa: BLE001
            print(f"[aviso] feed fallido {url}: {e}", file=sys.stderr)
    return all_items


def score(item, patterns):
    title = item["title"].lower()
    text = (item["title"] + " " + item["summary"]).lower()
    s = 0
    for p in patterns:
        rx = re.compile(rf"\b{p}\b")
        if rx.search(title):
            s += 3
        elif rx.search(text):
            s += 1
    if s == 0:
        return 0
    for w in BOOST:
        if re.search(rf"\b{re.escape(w)}\b", title):
            s += 1
    return s


def pick_news(items, sym, now=None):
    now = now or datetime.now(timezone.utc)
    limit = now - timedelta(hours=MAX_AGE_HOURS)
    patterns = COINS[sym]["words"]
    scored = []
    for it in items:
        if it["date"] and it["date"] < limit:
            continue
        sc = score(it, patterns)
        if sc > 0:
            scored.append((sc, it["date"] or limit, it))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)

    chosen, seen = [], set()
    for _, _, it in scored:
        key = re.sub(r"\W+", "", it["title"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        chosen.append(it)
        if len(chosen) == NEWS_PER_COIN:
            break
    return chosen


# ----------------------------------------------------------------- FORMATO
def fmt_price(p, cur):
    if p is None:
        return "n/d"
    sym = "$" if cur == "usd" else "€"
    if p >= 100:
        return f"{p:,.0f}{sym}".replace(",", ".")
    return f"{p:,.4f}{sym}".replace(",", "X").replace(".", ",").replace("X", ".")


def build_message(prices, news_by_coin, now):
    esc = html.escape
    lines = [f"📰 <b>Resumen crypto — {now.astimezone().strftime('%d/%m/%Y')}</b>", ""]
    for sym, c in COINS.items():
        p = prices.get(sym, {})
        ch = p.get("change24h")
        ch_txt = ""
        if ch is not None:
            arrow = "🟢" if ch >= 0 else "🔴"
            ch_txt = f" {arrow} {ch:+.2f}% (24h)"
        lines.append(
            f"{c['emoji']} <b>{c['name']} ({sym})</b> — {fmt_price(p.get('usd'), 'usd')} / "
            f"{fmt_price(p.get('eur'), 'eur')}{ch_txt}"
        )
        items = news_by_coin.get(sym, [])
        if not items:
            lines.append("   • Sin noticias relevantes en las últimas 24h")
        for it in items:
            lines.append(f"   • <a href=\"{esc(it['link'], quote=True)}\">{esc(it['title'])}</a>")
        lines.append("")
    return "\n".join(lines).strip()


# ----------------------------------------------------------------- SALIDAS
def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[info] Sin TELEGRAM_TOKEN/TELEGRAM_CHAT_ID: no se envía mensaje. Vista previa:\n")
        print(text)
        return False
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, headers=UA
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        ok = json.loads(r.read()).get("ok")
    print("[ok] Mensaje enviado a Telegram" if ok else "[error] Telegram no aceptó el mensaje")
    return ok


def save_json(prices, news_by_coin, now):
    os.makedirs("docs", exist_ok=True)
    data = {
        "updated": now.isoformat(),
        "prices": prices,
        "news": {
            sym: [
                {"title": i["title"], "link": i["link"], "source": i["source"],
                 "date": i["date"].isoformat() if i["date"] else None}
                for i in items
            ]
            for sym, items in news_by_coin.items()
        },
    }
    with open("docs/data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("[ok] docs/data.json guardado")


# ----------------------------------------------------------------- MAIN
def main():
    now = datetime.now(timezone.utc)
    prices = get_prices()
    items = fetch_all_news()
    print(f"[info] {len(items)} noticias leídas de {len(FEEDS)} feeds")
    news_by_coin = {sym: pick_news(items, sym, now) for sym in COINS}
    save_json(prices, news_by_coin, now)
    send_telegram(build_message(prices, news_by_coin, now))


if __name__ == "__main__":
    main()
