import sqlite3
import requests
import feedparser
import logging
from datetime import datetime, timedelta
import json
import os
import sys
from email.mime.text import MIMEText
from email.header import Header
import smtplib

# ===========================================
# 0. Konfigurasjon
# ===========================================
KEYWORDS = [
    "bank",
    "finans",
    "teknologi",
    "digital",
    "kunstig intelligens",
    "krypto",
    "hvitvasking",
    "bærekraft",
    "eu-direktiv",
    "forordning",
    "arbeidsmiljø",
    "personvern"
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
    "User-Agent": "Lovsonar/4.2 (+https://github.com/Majac999/Lovsonar)"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ===========================================
# 1. Database
# ===========================================
def setup_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seen_items (
            item_id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            date_seen TEXT
        )
    """)
    cursor.execute("""
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
    conn.close()

def is_seen(item_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_as_seen(item_id, source, title):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO seen_items 
        (item_id, source, title, date_seen) 
        VALUES (?, ?, ?, ?)
        """,
        (item_id, source, title, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def log_weekly_hit(item):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
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
    conn.close()

# ===========================================
# 2. Felles logikk
# ===========================================
def matches_keywords(text):
    if not text:
        return False
    # Sikrer at vi jobber med tekst, selv om vi får tall
    text_lower = str(text).lower()
    return any(keyword in text_lower for keyword in KEYWORDS)

def send_email(emne, tekst):
    avsender = os.environ.get("EMAIL_USER")
    passord = os.environ.get("EMAIL_PASS")
    mottaker = os.environ.get("EMAIL_RECIPIENT", avsender)

    if not avsender or not passord or not mottaker:
        logger.warning("Mangler e-postkonfigurasjon (EMAIL_USER/PASS/RECIPIENT). Sender ikke e-post.")
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
        logger.info("E-post sendt til %s", mottaker)
        return True
    except Exception as e:
        logger.error("Feil ved sending av e-post: %s", e)
        return False

# ===========================================
# 3. RSS-kilder
# ===========================================
def check_rss_feed(source_name, url):
    logger.info("Sjekker kilde: %s ...", source_name)
    entries = []
    try:
        feed = feedparser.parse(url)
        # Ignorerer bozo-feil (XML-feil) så lenge vi får entries
        if not feed.entries and getattr(feed, "bozo", 0):
             logger.warning("RSS-advarsel for %s: %s", source_name, feed.bozo_exception)
        
        for entry in feed.entries:
            item_id = entry.get("guid") or entry.get("id") or entry.get("link")
            if not item_id or is_seen(item_id):
                continue

            title = entry.get("title", "").strip()
            description = (entry.get("description") or entry.get("summary") or "").strip()
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
        logger.error("Feil ved kilde %s: %s", source_name, e)
    return entries

# ===========================================
# 4. Stortinget API
# ===========================================
def get_current_session():
    try:
        resp = requests.get(
            "https://data.stortinget.no/eksport/sesjoner?format=json", 
            headers=HEADERS, 
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("innevaerende_sesjon", {}).get("id")
    except Exception as e:
        logger.error("Kunne ikke hente inneværende sesjon: %s", e)
        return None

def get_stortinget_api():
    session_id = get_current_session()
    if not session_id:
        return []

    logger.info("Sjekker kilde: 🏛️ Stortinget (sesjon %s) ...", session_id)
    url = f"https://data.stortinget.no/eksport/saker?sesjonid={session_id}&format=json"
    hits = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        for sak in data.get("saker_liste", []):
            # Vi konverterer til str() før .lower() for å unngå krasj ved tall
            dok_gruppe = str(sak.get("dokumentgruppe") or "").lower()
            
            # --- FILTER PÅ (PRODUKSJON) ---
            # Vi vil bare ha proposisjoner (lovforslag) og innstillinger.
            # Dette filtrerer bort spørsmål og interpellasjoner.
            if "proposisjon" not in dok_gruppe and "innstilling" not in dok_gruppe:
                 continue
            # ------------------------------

            sak_id = f"{session_id}-{sak.get('id', '')}"
            title = sak.get("tittel", "")

            if is_seen(sak_id):
                continue

            if matches_keywords(title):
                link = f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={sak.get('id', '')}"
                hit = {
                    "type": "🏛️ Stortingssak",
                    "title": title,
                    "description": str(sak.get("henvisning") or "Ingen beskrivelse"),
                    "link": link,
                    "source": "Stortinget"
                }
                hits.append(hit)
                mark_as_seen(sak_id, "stortinget_api", title)
    except Exception as e:
        logger.error("Feil ved Stortinget API: %s", e)
    
    return hits

# ===========================================
# 5. Rapport-funksjoner
# ===========================================
def fetch_report_hits(days):
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT source, title, description, link, detected_at 
        FROM weekly_hits 
        WHERE detected_at >= ? 
        ORDER BY detected_at DESC
        """, 
        (cutoff,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows

def purge_report_hits(max_age_days=30):
    cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM weekly_hits WHERE detected_at < ?", (cutoff,))
    conn.commit()
    conn.close()

def build_weekly_report(rows, days):
    if not rows:
        return f"Ingen nye treff de siste {days} dagene."
    
    headline = f"Lovsonar-rapport for de siste {days} dagene ({len(rows)} funn)\n"
    body_lines = [headline]

    for source, title, description, link, detected_at in rows:
        body_lines.append(
            f"- {source}: {title}\n"
            f"  Lenke: {link or 'Ingen lenke'}\n"
            f"  Oppdaget: {detected_at}\n"
            f"  {description}\n"
        )
    return "\n".join(body_lines)

def send_weekly_report(rows, days):
    tekst = build_weekly_report(rows, days)
    emne = f"Lovsonar – rapport ({len(rows)} funn, siste {days} dager)"
    send_email(emne, tekst)

# ===========================================
# 6. Kjøremodi
# ===========================================
def run_daily():
    logger.info("=== Starter Lovsonar (daglig modus) ===")
    setup_database()
    hits = []

    for name, url in RSS_SOURCES.items():
        hits.extend(check_rss_feed(name, url))

    hits.extend(get_stortinget_api())

    # Logges for ukesrapporten
    for item in hits:
        log_weekly_hit(item)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"count": len(hits), "items": hits}, f, ensure_ascii=False, indent=2)

    if hits:
        logger.info("Fant %d nye treff.", len(hits))
        for item in hits:
            logger.info("%s: %s", item["type"], item["title"])
    else:
        logger.info("Ingen nye treff i dag.")

def run_weekly(days):
    logger.info("=== Starter Lovsonar (rapportmodus, %d dager) ===", days)
    setup_database()

    rows = fetch_report_hits(days=days)
    if rows:
        send_weekly_report(rows, days)
    else:
        logger.info("Ingen treff å rapportere for de siste %d dagene.", days)
    
    purge_report_hits(max_age_days=30)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "count": len(rows),
                "items": [
                    {
                        "source": r[0],
                        "title": r[1],
                        "description": r[2],
                        "link": r[3],
                        "detected_at": r[4]
                    }
                    for r in rows
                ]
            }, 
            f, 
            ensure_ascii=False, 
            indent=2
        )

# ===========================================
# 7. Entrypoint
# ===========================================
if __name__ == "__main__":
    mode = os.environ.get("LOVSONAR_MODE", "daily").strip().lower()
    report_days = int(os.environ.get("REPORT_WINDOW_DAYS", DEFAULT_REPORT_DAYS))

    if mode == "weekly":
        run_weekly(report_days)
    else:
        run_daily()
