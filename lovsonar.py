# lovsonar.py
import sqlite3
import feedparser
import logging
import os
import smtplib
import time
import requests
import hashlib
import re
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.header import Header
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ===========================================
# 1. KONFIGURASJON
# ===========================================

KW_SEGMENT = [
    "byggevare", "byggevarehus", "trelast", "jernvare",
    "lavpris", "discount", "diy", "samvirkelag", "coop", "obs bygg"
]

KW_TOPIC = [
    "bærekraft", "sirkulær", "gjenvinning", "miljøkrav",
    "taksonomi", "esg", "csrd", "aktsomhet",
    "grønnvasking", "reach", "pfas", "emballasje",
    "digitalt produktpass", "sporbarhet", "epd",
    "høring", "høringsfrist"
]

KW_NOISE = ["skriv ut", "meny", "til toppen", "personvern"]

# ✅ KUN RSS SOM FAKTISK FUNGERER
RSS_SOURCES = {
    "📢 Høringer": "https://www.regjeringen.no/no/dokument/horinger/id1763/?show=rss",
    "📚 NOU": "https://www.regjeringen.no/no/dokument/nou-er/id1767/?show=rss"
}

DB_PATH = "lovsonar.db"
USER_AGENT = "LovSonar/3.0 (Compliance Monitor)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# ===========================================
# 2. HJELPEFUNKSJONER
# ===========================================

def get_http_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1,
                  status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": USER_AGENT})
    return session

def make_id(source, link, title):
    raw = f"{source}|{link}|{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def clean_text(text):
    if not text:
        return ""
    from html import unescape
    return " ".join(re.sub(r"<[^>]+>", " ", unescape(text)).split())

# ===========================================
# 3. DATABASE
# ===========================================

def setup_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                item_id TEXT PRIMARY KEY,
                source TEXT,
                title TEXT,
                date_seen TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                title TEXT,
                link TEXT,
                published TEXT,
                excerpt TEXT,
                detected_at TEXT
            )
        """)
        conn.commit()

# ===========================================
# 4. ANALYSE
# ===========================================

def analyze_item(source, title, description, link, pub_date):
    item_id = make_id(source, link, title)

    with sqlite3.connect(DB_PATH) as conn:
        if conn.execute(
            "SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,)
        ).fetchone():
            return

        text = f"{title} {description}".lower()

        if sum(1 for k in KW_NOISE if k in text) > 2:
            return

        has_segment = any(k in text for k in KW_SEGMENT)
        has_topic = any(k in text for k in KW_TOPIC)

        conn.execute(
            "INSERT INTO seen_items VALUES (?, ?, ?, ?)",
            (item_id, source, title, datetime.utcnow().isoformat())
        )

        if has_topic and (has_segment or "høring" in text):
            logger.info(f"✅ TREFF: {title}")
            conn.execute("""
                INSERT INTO hits (source, title, link, published, excerpt, detected_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                source, title, link,
                pub_date.isoformat(),
                description[:500],
                datetime.utcnow().isoformat()
            ))

        conn.commit()

# ===========================================
# 5. RSS-INNHENTING
# ===========================================

def check_rss():
    session = get_http_session()

    for name, url in RSS_SOURCES.items():
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            feed = feedparser.parse(r.text)

            for e in feed.entries:
                title = clean_text(e.get("title", ""))
                link = e.get("link", "")
                desc = clean_text(e.get("description", ""))

                if hasattr(e, "published_parsed") and e.published_parsed:
                    p_date = datetime(*e.published_parsed[:6])
                else:
                    p_date = datetime.utcnow()

                analyze_item(name, title, desc, link, p_date)

        except Exception as ex:
            logger.warning(f"RSS-feil {name}: {ex}")

# ===========================================
# 6. STORTINGET (JSON-API)
# ===========================================

def check_stortinget():
    logger.info("🏛️ Sjekker Stortinget")
    session = get_http_session()

    try:
        ses = session.get(
            "https://data.stortinget.no/eksport/sesjoner?format=json",
            timeout=20
        ).json()
        sid = ses.get("innevaerende_sesjon", {}).get("id")

        page = 1
        while page <= 5:
            url = f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&pagesize=50&page={page}&format=json"
            data = session.get(url, timeout=20).json()

            saker = data.get("saker_liste", {}).get("sak", [])
            if not saker:
                break

            for sak in saker:
                title = sak.get("tittel", "")
                tema = sak.get("tema", "")
                link = f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={sak.get('id')}"
                date_str = sak.get("sist_oppdatert") or sak.get("registrert_dato")
                p_date = datetime.fromisoformat(date_str.replace("Z", "")) if date_str else datetime.utcnow()

                analyze_item("🏛️ Stortinget", title, tema, link, p_date)

            page += 1
            time.sleep(1)

    except Exception as e:
        logger.error(f"Stortinget-feil: {e}")

# ===========================================
# 7. RAPPORT (VALGFRI)
# ===========================================

def send_weekly_report():
    user = os.getenv("EMAIL_USER")
    pwd = os.getenv("EMAIL_PASS")
    to = os.getenv("EMAIL_RECIPIENT", user)

    if not user or not pwd or not to:
        return

    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT source, title, link, published
            FROM hits WHERE detected_at >= ?
            ORDER BY published DESC
        """, (cutoff,)).fetchall()

    if not rows:
        return

    body = ["LovSonar – ukentlig oversikt\n"]
    for s, t, l, d in rows:
        body.append(f"- {t} ({s})\n  {l}")

    msg = MIMEText("\n".join(body), "plain", "utf-8")
    msg["Subject"] = Header("LovSonar – nye relevante saker", "utf-8")
    msg["From"] = user
    msg["To"] = to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(user, pwd)
        srv.send_message(msg)

# ===========================================
# 8. MAIN
# ===========================================

if __name__ == "__main__":
    setup_db()

    mode = os.getenv("LOVSONAR_MODE", "daily").lower()
    if mode == "weekly":
        send_weekly_report()
    else:
        check_rss()
        check_stortinget()
