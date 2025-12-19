import sqlite3
import requests
import feedparser
import logging
import json
import os
import sys
import smtplib
import re
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.header import Header
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from contextlib import contextmanager
from html import unescape

# ===========================================
# 0. Konfigurasjon
# ===========================================
KEYWORDS = [
    # Økonomi & Tech
    "bank", "finans", "teknologi", "digital", "kunstig intelligens",
    "krypto", "hvitvasking", "personvern", "ai act",
    
    # Bærekraft & Miljø (Utvidet)
    "bærekraft", "eu-direktiv", "forordning", "klima", "energi",
    "taksonomi", "biodiversitet", "naturavtale", "utslipp",
    "sirkulær", "ombruk", "gjenvinning", "miljøkrav",
    
    # Handel & Bygg
    "arbeidsmiljø", "emballasje", "avfall", "forbruker",
    "byggevare", "trelast", "kjemikalie", "esg", "csrd", "åpenhetsloven"
]

RSS_SOURCES = {
    "🇪🇺 EØS-notat": "https://www.regjeringen.no/no/dokument/eos-notater/rss/",
    "📚 NOU (Utredning)": "https://www.regjeringen.no/no/dokument/nou-er/rss/",
    "📢 Høring": "https://www.regjeringen.no/no/dokument/horinger/rss/",
    "📜 Proposisjon": "https://www.regjeringen.no/no/dokument/proposisjoner/rss/"
}

DB_PATH = "lovsonar_seen.db"
OUTPUT_FILE = "nye_treff.json"
DEFAULT_REPORT_DAYS = 7

HEADERS = {
    "User-Agent": "Lovsonar/5.1 (+https://github.com/Majac999/Lovsonar)"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ===========================================
# 1. Hjelpefunksjoner (Tekst & Nettverk)
# ===========================================
def clean_html(text):
    """Fjerner HTML-tags og rydder i whitespace for bedre søk."""
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split()).strip()

def matches_keywords(text):
    """Sjekker om teksten inneholder noen av nøkkelordene."""
    if not text:
        return False
    text_lower = str(text).lower()
    return any(keyword in text_lower for keyword in KEYWORDS)

def get_http_session():
    """Lager en sesjon med automatisk retry ved feil (Robusthet)."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session

# ===========================================
# 2. Database (Sikker håndtering)
# ===========================================
@contextmanager
def get_db_connection():
    """Context manager for sikker database-lukking."""
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()

def setup_database():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                item_id TEXT PRIMARY KEY,
                source TEXT,
                title TEXT,
                date_seen TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weekly_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                title TEXT,
                description TEXT,
                link TEXT,
                detected_at TEXT
            )
        """)
        conn.commit()

def is_seen(item_id):
    with get_db_connection() as conn:
        cursor = conn.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (str(item_id),))
        return cursor.fetchone() is not None

def mark_as_seen(item_id, source, title):
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO seen_items 
            (item_id, source, title, date_seen) 
            VALUES (?, ?, ?, ?)
            """,
            (str(item_id), source, title, datetime.utcnow().isoformat())
        )
        conn.commit()

def log_weekly_hit(item):
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO weekly_hits 
            (source, title, description, link, detected_at) 
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                item["type"], 
                item["title"], 
                item["description"], 
                item["link"], 
                datetime.utcnow().isoformat()
            )
        )
        conn.commit()

def purge_old_data(max_age_days=180):
    """Rydder opp i gamle data (både 'sett'-logg og rapport-hits)."""
    cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
    with get_db_connection() as conn:
        conn.execute("DELETE FROM seen_items WHERE date_seen < ?", (cutoff,))
        conn.execute("DELETE FROM weekly_hits WHERE detected_at < ?", (cutoff,))
        conn.commit()
    logger.info("🧹 Ryddet opp data eldre enn %d dager.", max_age_days)

# ===========================================
# 3. E-postvarsling
# ===========================================
def send_email(emne, tekst):
    avsender = os.environ.get("EMAIL_USER")
    passord = os.environ.get("EMAIL_PASS")
    mottaker = os.environ.get("EMAIL_RECIPIENT", avsender)

    if not avsender or not passord:
        logger.warning("🚫 E-postkonfig mangler. Hopper over sending.")
        return False

    msg = MIMEText(tekst, "plain", "utf-8")
    msg["Subject"] = Header(emne, "utf-8")
    msg["From"] = avsender
    msg["To"] = mottaker

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
        server.login(avsender, passord)
        server.send_message(msg)
        server.quit()
        logger.info("📧 E-post sendt til %s", mottaker)
        return True
    except Exception as e:
        logger.error("❌ Feil ved sending av e-post: %s", e)
        return False

# ===========================================
# 4. RSS-kilder (Med HTML-vask)
# ===========================================
def check_rss_feed(source_name, url):
    logger.info("📡 Sjekker RSS: %s", source_name)
    entries = []
    try:
        feed = feedparser.parse(url, request_headers=HEADERS)
        
        if not feed.entries and getattr(feed, "bozo", 0):
             logger.warning("⚠️ RSS-advarsel for %s: %s", source_name, feed.bozo_exception)

        for entry in feed.entries:
            item_id = entry.get("guid") or entry.get("id") or entry.get("link")
            if not item_id or is_seen(item_id):
                continue

            # HTML-vask før søk
            raw_title = entry.get("title", "").strip()
            raw_desc = (entry.get("description") or entry.get("summary") or "").strip()
            
            title = clean_html(raw_title)
            description = clean_html(raw_desc)
            link = entry.get("link", "")

            if matches_keywords(f"{title} {description}"):
                hit = {
                    "type": source_name,
                    "title": title,
                    "description": (description[:300] + "...") if description else "",
                    "link": link,
                    "source": "Regjeringen.no"
                }
                entries.append(hit)
                mark_as_seen(item_id, source_name, title)
    except Exception as e:
        logger.error("❌ Feil ved RSS %s: %s", source_name, e)
    return entries

# ===========================================
# 5. Stortinget API (Smartere filter)
# ===========================================
def get_current_session(session):
    try:
        resp = session.get("https://data.stortinget.no/eksport/sesjoner?format=json", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("innevaerende_sesjon", {}).get("id")
    except Exception as e:
        logger.error("❌ Kunne ikke hente sesjon ID: %s", e)
        return "2025-2026" # Fallback

def get_stortinget_api():
    http = get_http_session()
    session_id = get_current_session(http)
    
    logger.info("🏛️ Sjekker Stortinget (Sesjon %s)...", session_id)
    url = f"https://data.stortinget.no/eksport/saker?sesjonid={session_id}&format=json"
    hits = []

    try:
        resp = http.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        for sak in data.get("saker_liste", []):
            try:
                # --- FILTERING ---
                dok_gruppe = str(sak.get("dokumentgruppe") or "").lower()
                status = str(sak.get("status") or "").lower()
                
                # 1. Fjern støy
                if any(x in dok_gruppe for x in ["dokument 12", "spørsmål", "interpellasjon", "referat"]):
                    continue

                # 2. Inkluder det viktige (Prop, Innst, Melding, Dok 8, Lovsak)
                relevante_typer = ["proposisjon", "innstilling", "melding", "dokument 8", "dok 8", "lovsak"]
                if not any(x in dok_gruppe for x in relevante_typer) and "lovsak" not in status:
                    continue
                # ---------------

                sak_id = f"{session_id}-{sak.get('id', '')}"
                title = sak.get("tittel", "") or ""
                korttittel = sak.get("korttittel", "") or ""

                if is_seen(sak_id):
                    continue

                if matches_keywords(f"{title} {korttittel}"):
                    link = f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={sak.get('id', '')}"
                    hit = {
                        "type": "🏛️ Stortingssak",
                        "title": title.strip(),
                        "description": korttittel.strip() or "Ingen beskrivelse",
                        "link": link,
                        "source": "Stortinget"
                    }
                    hits.append(hit)
                    mark_as_seen(sak_id, "stortinget_api", title)
            except Exception as inner_e:
                logger.warning("Feil ved parsing av enkeltsak: %s", inner_e)
                continue

    except Exception as e:
        logger.error("❌ Feil ved Stortinget API: %s", e)

    return hits

# ===========================================
# 6. Rapport & Kjøring
# ===========================================
def fetch_report_hits(days):
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            SELECT source, title, description, link, detected_at 
            FROM weekly_hits 
            WHERE detected_at >= ? 
            ORDER BY detected_at DESC
            """, 
            (cutoff,)
        )
        return cursor.fetchall()

def run_daily():
    logger.info("=== 🟢 Starter Lovsonar (Daglig) ===")
    setup_database()
    purge_old_data() # Rydder nå opp både seen og weekly_hits

    hits = []

    for name, url in RSS_SOURCES.items():
        hits.extend(check_rss_feed(name, url))

    hits.extend(get_stortinget_api())

    for item in hits:
        log_weekly_hit(item)

    # Lagre JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"count": len(hits), "items": hits}, f, ensure_ascii=False, indent=2)

    if hits:
        logger.info("✅ Fant %d nye treff.", len(hits))
    else:
        logger.info("💤 Ingen nye treff i dag.")

def run_weekly(days):
    logger.info("=== 🔵 Starter Lovsonar (Ukesrapport, %d dager) ===", days)
    setup_database()

    rows = fetch_report_hits(days=days)
    if rows:
        tekst = f"Lovsonar-rapport ({len(rows)} funn, siste {days} dager)\n\n"
        for row in rows:
            tekst += f"- {row[0]}: {row[1]}\n  Lenke: {row[3]}\n\n"
        
        send_email(f"Lovsonar Ukesblikk ({len(rows)} saker)", tekst)
        logger.info("Rapport sendt.")
    else:
        logger.info("Ingen treff å rapportere denne uken.")

# ===========================================
# 7. Main
# ===========================================
if __name__ == "__main__":
    mode = os.environ.get("LOVSONAR_MODE", "daily").strip().lower()
    try:
        report_days = int(os.environ.get("REPORT_WINDOW_DAYS", DEFAULT_REPORT_DAYS))
    except ValueError:
        report_days = 7

    if mode == "weekly":
        run_weekly(report_days)
    else:
        run_daily()
