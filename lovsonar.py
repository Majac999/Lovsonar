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
# 0. Konfigurasjon (JUKSE-VERSJON FOR TESTING)
# ===========================================
KEYWORDS = [
    # --- Segment / Varehandel & Lavpris ---
    "byggevarehandel", "byggevarehus", "byggevare", "trelast", "jernvare",
    "lavpris", "discount", "billigkjede", "gjør-det-selv", "gds", "diy",
    "maleutstyr", "isolasjon", "varehandel", "konkurransetilsynet",

    # --- Digitalisering, AI & Systemer ---
    "kunstig intelligens", "maskinlæring", "ki", "ai",
    "digitalisering", "digitaliseringsstrategi", "datadeling",
    "interoperabilitet", "api", "grensesnitt", "systemintegrasjon",
    "bim", "bygningsinformasjonsmodellering", "digital tvilling",
    "digitek", "produktdatabase", "nobbag", "efo", "byggtjeneste",
    "digitalt produktpass", "dpp", "sporbarhet",

    # --- Bærekraft & Sirkulærøkonomi ---
    "bærekraft", "miljøkrav", "sirkulær", "ombruk", "gjenvinning", 
    "avfall", "emballasje", "plastløftet", "klima", "energi",
    "taksonomi", "esg", "grønnvasking", "green claims",

    # --- Spesifikke EU/Norske Regelverk ---
    "eu-direktiv", "forordning", "forskrift", "høringsnotat",
    "åpenhetsloven", "arbeidsmiljøloven", "internkontroll",
    "ecodesign", "espr", "ppwr", "cbam", "csrd", "csddd",
    "cpr", "byggevareforordning", "reach", "clp", "eudr", 
    "epbd", "bygningsenergidirektiv",

    # --- Norske Instanser ---
    "miljødirektoratet", "direktoratet for byggkvalitet", "dibk",
    "digitaliseringsdirektoratet", "digdir", "datatilsynet"
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
    "User-Agent": "Lovsonar/5.3 (+https://github.com/Majac999/Lovsonar)"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ===========================================
# 1. Hjelpefunksjoner
# ===========================================
def clean_html(text):
    if not text: return ""
    text = unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split()).strip()

def matches_keywords(text):
    if not text: return False
    text_lower = str(text).lower()
    return any(keyword in text_lower for keyword in KEYWORDS)

def get_http_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session

def get_company_profile():
    profile = os.environ.get("COMPANY_CONTEXT")
    if not profile: return "Generisk norsk handelsbedrift."
    return profile

# ===========================================
# 2. Database
# ===========================================
@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    try: yield conn
    finally: conn.close()

def setup_database():
    with get_db_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS seen_items (item_id TEXT PRIMARY KEY, source TEXT, title TEXT, date_seen TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS weekly_hits (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, title TEXT, description TEXT, link TEXT, detected_at TEXT)")
        conn.commit()

def is_seen(item_id):
    with get_db_connection() as conn:
        return conn.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (str(item_id),)).fetchone() is not None

def mark_as_seen(item_id, source, title):
    with get_db_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)", (str(item_id), source, title, datetime.utcnow().isoformat()))
        conn.commit()

def log_weekly_hit(item):
    with get_db_connection() as conn:
        conn.execute("INSERT INTO weekly_hits (source, title, description, link, detected_at) VALUES (?, ?, ?, ?, ?)", (item["type"], item["title"], item["description"], item["link"], datetime.utcnow().isoformat()))
        conn.commit()

def purge_old_data(max_age_days=180):
    cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
    with get_db_connection() as conn:
        conn.execute("DELETE FROM seen_items WHERE date_seen < ?", (cutoff,))
        conn.execute("DELETE FROM weekly_hits WHERE detected_at < ?", (cutoff,))
        conn.commit()

# ===========================================
# 3. E-post
# ===========================================
def send_email(emne, tekst):
    avsender = os.environ.get("EMAIL_USER")
    passord = os.environ.get("EMAIL_PASS")
    mottaker = os.environ.get("EMAIL_RECIPIENT", avsender)
    
    if not avsender or not passord:
        logger.warning("🚫 E-postkonfig mangler.")
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
# 4. Logikk (RSS & API)
# ===========================================
def check_rss_feed(source_name, url):
    logger.info("📡 Sjekker RSS: %s", source_name)
    entries = []
    try:
        feed = feedparser.parse(url, request_headers=HEADERS)
        for entry in feed.entries:
            item_id = entry.get("guid") or entry.get("id") or entry.get("link")
            if not item_id or is_seen(item_id): continue

            title = clean_html(entry.get("title", ""))
            desc = clean_html(entry.get("description") or entry.get("summary") or "")
            link = entry.get("link", "")

            if matches_keywords(f"{title} {desc}"):
                hit = {"type": source_name, "title": title, "description": desc[:300], "link": link}
                entries.append(hit)
                mark_as_seen(item_id, source_name, title)
    except Exception as e:
        logger.error("❌ Feil RSS: %s", e)
    return entries

def get_stortinget_api():
    http = get_http_session()
    # Enkel sesjonssjekk
    try:
        sid = http.get("https://data.stortinget.no/eksport/sesjoner?format=json").json()["innevaerende_sesjon"]["id"]
    except: sid = "2025-2026"
    
    logger.info("🏛️ Sjekker Stortinget (%s)...", sid)
    hits = []
    try:
        data = http.get(f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&format=json").json()
        for sak in data.get("saker_liste", []):
            dg = str(sak.get("dokumentgruppe") or "").lower()
            if any(x in dg for x in ["spørsmål", "interpellasjon", "referat"]): continue
            
            sid_sak = f"{sid}-{sak.get('id')}"
            if is_seen(sid_sak): continue
            
            tittel = sak.get("tittel", "")
            if matches_keywords(tittel):
                hits.append({"type": "🏛️ Stortingssak", "title": tittel, "description": "Ny sak", "link": f"https://stortinget.no/sak/{sak.get('id')}"})
                mark_as_seen(sid_sak, "api", tittel)
    except Exception as e: logger.error("❌ API Feil: %s", e)
    return hits

def fetch_report_hits(days):
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_db_connection() as conn:
        return conn.execute("SELECT source, title, description, link, detected_at FROM weekly_hits WHERE detected_at >= ?", (cutoff,)).fetchall()

# ===========================================
# 5. Kjøring
# ===========================================
def run_daily():
    setup_database()
    purge_old_data()
    hits = []
    for n, u in RSS_SOURCES.items(): hits.extend(check_rss_feed(n, u))
    hits.extend(get_stortinget_api())
    for h in hits: log_weekly_hit(h)
    
    if hits: logger.info("✅ Fant %d treff (Lagret i DB).", len(hits))
    else: logger.info("💤 Ingen nye treff.")

def run_weekly(days):
    setup_database()
    rows = fetch_report_hits(days)
    if rows:
        tekst = f"Lovsonar-rapport ({len(rows)} saker)\n{'='*30}\n\n"
        for r in rows: tekst += f"- {r[0]}: {r[1]}\n  {r[3]}\n\n"
        
        tekst += "\n" + "="*30 + "\n--- FOR AI ANALYSE (KOPIER MED INN I CHATGPT) ---\nANALYSE-KONTEKST:\n" + get_company_profile() + "\n\nOPPGAVE: Analyser sakene over."
        
        if send_email(f"Lovsonar Rapport ({len(rows)} treff)", tekst):
            logger.info("✅ Rapport sendt!")
    else:
        logger.info("Ingen treff å rapportere.")

if __name__ == "__main__":
    mode = os.environ.get("LOVSONAR_MODE", "daily").lower()
    days = int(os.environ.get("REPORT_WINDOW_DAYS", 7))
    if mode == "weekly": run_weekly(days)
    else: run_daily()
