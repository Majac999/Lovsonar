import sqlite3
import feedparser
import logging
import json
import os
import sys
import smtplib
import time
import requests
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.header import Header
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import re

# Sjekker om pdf_leser.py ligger i mappen
try:
    import pdf_leser
except ImportError:
    pdf_leser = None
    print("⚠️ ADVARSEL: Fant ikke pdf_leser.py. PDF-analyse vil ikke fungere.")

# ===========================================
# 1. KONFIGURASJON & NØKKELORD
# ===========================================

KW_SEGMENT = ["byggevare", "byggevarehus", "trelast", "jernvare", "lavpris", "discount", "billigkjede", "gds", "diy", "ombruk", "materialbank", "produktdatabase", "byggtjeneste", "varehandel", "samvirkelag", "coop", "obs bygg", "detaljhandel", "kjøpesenter"]
KW_TOPIC = ["bærekraft", "sirkulær", "gjenvinning", "miljøkrav", "taksonomi", "esg", "espr", "ecodesign", "ppwr", "cbam", "csrd", "csddd", "aktsomhet", "green claims", "grønnvasking", "reach", "clp", "pfas", "eudr", "epbd", "byggevareforordning", "cpr", "emballasje", "plastløftet", "merking", "etikett", "dokumentasjon", "digitalt produktpass", "dpp", "sporbarhet", "produktpass", "qr-kode", "epd", "miljødeklarasjon", "farlige stoffer", "biocid", "voc", "løsemiddel", "torv", "naturmangfold", "avskoging", "tropisk", "impregnert", "overtredelsesgebyr", "tvangsmulkt", "klimaavgift", "høringsnotat", "høringsfrist"]
KW_NOISE = ["skriv ut", "verktøylinje", "del paragraf", "meny", "til toppen", "personvern", "tilgjengelighet"]

RSS_SOURCES = {
    "🇪🇺 EØS-notat": "https://www.regjeringen.no/no/dokument/eos-notater/rss/",
    "📚 NOU (Utredning)": "https://www.regjeringen.no/no/dokument/nou-er/rss/",
    "📢 Høring": "https://www.regjeringen.no/no/dokument/horinger/rss/",
    "📜 Lovforslag/Prop": "https://www.regjeringen.no/no/dokument/proposisjoner/rss/"
}

DB_PATH = "lovsonar_seen.db"
USER_AGENT = "LovSonar/2.5 (Strategic Compliance Tool)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# ===========================================
# 2. NETTVERK & LOGIKK
# ===========================================

def get_http_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session

def clean_text(text):
    if not text: return ""
    from html import unescape
    return " ".join(re.sub(r"<[^>]+>", " ", unescape(text)).split()).strip()

def matches_composite_logic(text):
    if not text: return False
    t = text.lower()
    if sum(1 for k in KW_NOISE if k in t) > 3: return False
    has_segment = any(k in t for k in KW_SEGMENT)
    has_topic = any(k in t for k in KW_TOPIC)
    is_critical = any(k in t for k in ["høringsfrist", "forslag til endring", "høringsnotat"])
    return (has_segment and has_topic) or is_critical

# ===========================================
# 3. DATABASE (Med Purge-funksjon)
# ===========================================

def setup_database():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS seen_items (item_id TEXT PRIMARY KEY, source TEXT, title TEXT, date_seen TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS weekly_hits (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, title TEXT, description TEXT, link TEXT, pub_date TEXT, excerpt TEXT, detected_at TEXT)")
        conn.commit()

def purge_old_data(days=180):
    """Codex Pkt 2: Rydder i databasen for å spare plass og holde farten oppe."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM seen_items WHERE date_seen < ?", (cutoff,))
            conn.execute("DELETE FROM weekly_hits WHERE detected_at < ?", (cutoff,))
            conn.commit()
            logger.info(f"🧹 Vaktmester: Slettet rader eldre enn {days} dager.")
    except Exception as e:
        logger.error(f"Feil ved rydding i DB: {e}")

# ===========================================
# 4. ANALYSE & INNSAMLING
# ===========================================

def analyze_item(source_name, title, description, link, pub_date, item_id):
    item_id = str(item_id or "").strip()
    if not item_id or pub_date < (datetime.utcnow() - timedelta(days=180)): return
    
    with sqlite3.connect(DB_PATH) as conn:
        if conn.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,)).fetchone():
            return

        full_text = f"{title} {description}"
        excerpt = description[:300] + "..."

        if pdf_leser and (link.lower().endswith(".pdf") or any(x in title.lower() for x in ["høring", "forskrift", "notat"])):
            tilleggs_tekst = pdf_leser.hent_pdf_tekst(link, maks_sider=10)
            if tilleggs_tekst and "FEIL" not in tilleggs_tekst:
                full_text += " " + tilleggs_tekst
                excerpt = f"[DOKUMENT-INFO]: {tilleggs_tekst[:600]}..."
                time.sleep(0.5)

        if matches_composite_logic(full_text):
            if "høring" in title.lower(): title = "📢 [HØRING] " + title
            if "proposisjon" in title.lower(): title = "📜 [PROP] " + title
            conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)", (item_id, source_name, title, datetime.utcnow().isoformat()))
            conn.execute("INSERT INTO weekly_hits (source, title, description, link, pub_date, excerpt, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (source_name, title, description, link, pub_date.isoformat(), excerpt, datetime.utcnow().isoformat()))
            conn.commit()
        else:
            conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)", (item_id, source_name, title, datetime.utcnow().isoformat()))
            conn.commit()

def check_stortinget():
    logger.info("🏛️ Poller Stortinget...")
    session = get_http_session()
    try:
        res = session.get("https://data.stortinget.no/eksport/sesjoner?format=json", timeout=15)
        sid = res.json()["innevaerende_sesjon"]["id"]
        page = 0
        while True:
            url = f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&pagesize=100&page={page}&format=json"
            data = session.get(url, timeout=15).json()
            saker = data.get("saker_liste", [])
            if not saker: break
            for sak in saker:
                dg = str(sak.get("dokumentgruppe") or "").lower()
                if any(x in dg for x in ["spørsmål", "interpellasjon", "referat", "skriftlig"]): continue
                analyze_item("🏛️ Stortinget", sak.get("tittel", ""), f"Type: {dg}. Tema: {sak.get('tema','')}", f"https://stortinget.no/sak/{sak['id']}", datetime.utcnow(), f"ST-{sak['id']}")
            page += 1
            time.sleep(0.5)
    except Exception as e:
        logger.error(f"Feil mot Stortinget: {e}")

def check_rss_feeds():
    for name, url in RSS_SOURCES.items():
        try:
            # Codex Pkt 1: Sjekk HTTP-status på feed
            feed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
            if getattr(feed, "status", 200) >= 400:
                logger.warning(f"⚠️ RSS-feil {getattr(feed, 'status', 'UKJENT')} for {name}")
                continue
                
            for entry in feed.entries:
                p_date = datetime(*entry.published_parsed[:6]) if hasattr(entry, "published_parsed") and entry.published_parsed else datetime.utcnow()
                analyze_item(name, clean_text(entry.get("title", "")), clean_text(entry.get("description", "")), entry.get("link", ""), p_date, entry.get("guid") or entry.get("link"))
        except Exception as e:
            logger.error(f"Feil ved RSS {name}: {e}")

# ===========================================
# 5. RAPPORTERING
# ===========================================

def send_weekly_report():
    email_user = os.environ.get("EMAIL_USER", "").strip()
    email_pass = os.environ.get("EMAIL_PASS", "").strip()
    email_to = os.environ.get("EMAIL_RECIPIENT", email_user).strip()
    
    # Codex Pkt 3: Sjekk mottaker før vi starter
    if not email_user or not email_pass or not email_to:
        logger.warning("E-post config mangler (USER/PASS/RECIPIENT). Hopper over sending.")
        return

    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT source, title, link, excerpt, pub_date FROM weekly_hits WHERE detected_at >= ? ORDER BY pub_date DESC", (cutoff,)).fetchall()

    if not rows: 
        logger.info("Ingen treff å rapportere denne uken.")
        return

    md_text = [f"# 🛡️ LovSonar: {len(rows)} Relevante treff", "Fokus: Bærekraft & Byggevarehandel\n"]
    for r in rows:
        md_text.append(f"## {r[1]}\n**Kilde:** {r[0]} | **Dato:** {r[4][:10]} | [Link]({r[2]})\n> {r[3]}\n---")
    md_text.append("\n### 🤖 AI KONTEKST\n" + os.environ.get("COMPANY_CONTEXT", "Obs BYGG."))
    
    msg = MIMEText("\n".join(md_text), "plain", "utf-8")
    msg["Subject"] = Header(f"LovSonar: {len(rows)} treff", "utf-8")
    msg["From"] = email_user
    msg["To"] = email_to

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
            server.login(email_user, email_pass)
            server.send_message(msg, from_addr=email_user, to_addrs=[email_to])
        logger.info("📧 Rapport sendt OK.")
    except Exception as e:
        logger.error(f"Feil ved sending: {e}")

if __name__ == "__main__":
    setup_database()
    purge_old_data() # Kjører vaktmester-rydding hver gang
    if os.environ.get("LOVSONAR_MODE", "daily").lower() == "weekly": 
        send_weekly_report()
    else:
        check_rss_feeds()
        check_stortinget()


