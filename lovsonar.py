import sqlite3
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
from html import unescape
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import re
from bs4 import BeautifulSoup # Nødvendig for å lese nettsiden direkte

# Sjekker om pdf_leser.py ligger i mappen
try:
    import pdf_leser
except ImportError:
    pdf_leser = None
    print("⚠️ ADVARSEL: Fant ikke pdf_leser.py. PDF-analyse vil ikke fungere.")

# ===========================================
# 1. KONFIGURASJON
# ===========================================

KW_SEGMENT = [
    "byggevare", "byggevarehus", "trelast", "jernvare", "lavpris", 
    "discount", "billigkjede", "gjør-det-selv", "gds", "diy", 
    "ombruk", "materialbank", "produktdatabase", "byggtjeneste",
    "varehandel", "konkurransetilsynet", "samvirkelag", "coop"
]

KW_TOPIC = [
    "bærekraft", "sirkulær", "gjenvinning", "miljøkrav", "taksonomi", 
    "esg", "espr", "ecodesign", "ppwr", "cbam", "csrd", "csddd", 
    "aktsomhet", "green claims", "grønnvasking", "reach", "clp", 
    "pfas", "eudr", "epbd", "byggevareforordning", "cpr", 
    "plastløftet", "emballasje", "klimaavgift", "digitale produktpass", "dpp",
    "arbeidsmiljøloven", "avhendingslova", "plan- og bygningsloven"
]

DB_PATH = "lovsonar_seen.db"

# Headers som får boten til å se ut som en ekte nettleser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "no-NO,no;q=0.9,en-US;q=0.8,en;q=0.7"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# ===========================================
# 2. HJELPEFUNKSJONER
# ===========================================

def get_http_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session

def clean_text(text):
    if not text: return ""
    text = unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split()).strip()

def matches_composite_logic(text):
    if not text: return False
    text_lower = text.lower()
    has_segment = any(k in text_lower for k in KW_SEGMENT)
    has_topic = any(k in text_lower for k in KW_TOPIC)
    return has_segment and has_topic

def is_old(date_obj, days=90):
    limit = datetime.utcnow() - timedelta(days=days)
    return date_obj < limit

# ===========================================
# 3. DATABASE
# ===========================================

def setup_database():
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
            CREATE TABLE IF NOT EXISTS weekly_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                source TEXT, 
                title TEXT, 
                description TEXT, 
                link TEXT, 
                pub_date TEXT,
                excerpt TEXT,
                detected_at TEXT
            )
        """)
        conn.commit()

def purge_old_data(days_to_keep=180):
    cutoff = (datetime.utcnow() - timedelta(days=days_to_keep)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM seen_items WHERE date_seen < ?", (cutoff,))
        conn.execute("DELETE FROM weekly_hits WHERE detected_at < ?", (cutoff,))
        conn.commit()

def is_seen(item_id):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (str(item_id),)).fetchone() is not None

def register_hit(item_id, source, title, desc, link, pub_date, excerpt):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)", 
                     (str(item_id), source, title, datetime.utcnow().isoformat()))
        conn.execute("""
            INSERT INTO weekly_hits (source, title, description, link, pub_date, excerpt, detected_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (source, title, desc, link, pub_date.isoformat(), excerpt, datetime.utcnow().isoformat()))
        conn.commit()

# ===========================================
# 4. ANALYSE
# ===========================================

def analyze_item(source_name, title, description, link, pub_date, item_id):
    if not item_id: return 
    if is_old(pub_date, days=180): return
    if is_seen(item_id): return

    full_text = f"{title} {description}"
    excerpt = description[:300] + "..."

    # PDF-Sjekk
    should_check_pdf = pdf_leser and (
        link.lower().endswith(".pdf") or 
        "høring" in title.lower() or 
        "forskrift" in title.lower()
    )

    if should_check_pdf:
        logger.info(f"   🔎 Sjekker PDF innhold: {title[:30]}...")
        try:
            tilleggs_tekst = pdf_leser.hent_pdf_tekst(link, maks_sider=10)
            if tilleggs_tekst and "FEIL" not in tilleggs_tekst:
                full_text += " " + tilleggs_tekst
                excerpt = f"[PDF]: {tilleggs_tekst[:600]}..."
                time.sleep(1) # Pause for å være høflig
        except Exception as e:
            logger.warning(f"Kunne ikke lese PDF: {e}")

    if matches_composite_logic(full_text):
        logger.info(f"✅ TREFF! {title}")
        if "høring" in title.lower(): title = "📢 [HØRING] " + title
        if "proposisjon" in title.lower(): title = "📜 [PROP] " + title
        register_hit(item_id, source_name, title, description, link, pub_date, excerpt)
    else:
        # Marker som sett (støy) så vi ikke sjekker den igjen
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)", 
                         (str(item_id), source_name, title, datetime.utcnow().isoformat()))
            conn.commit()

# ===========================================
# 5. INNSAMLING (Stortinget API + Regjeringen Web)
# ===========================================

def check_regjeringen_nettside():
    """
    Henter høringer direkte fra nettsiden (HTML) siden RSS er blokkert.
    """
    url = "https://www.regjeringen.no/no/aktuelt/horinger/id1763/"
    name = "📢 Regjeringen (Høringer)"
    logger.info(f"🌐 Sjekker {name} via nettsiden...")
    
    session = get_http_session()
    try:
        res = session.get(url, timeout=15)
        res.raise_for_status()
        
        soup = BeautifulSoup(res.content, "html.parser")
        
        # Finn listen med dokumenter. Regjeringen bruker ofte <h3> med class "a-text-title"
        # eller lenker inne i en liste. Vi søker bredt for å være sikre.
        links = soup.find_all("h3", class_="a-text-title")
        
        if not links:
            # Fallback hvis de endrer design: finn alle lenker i hovedinnholdet
            main_content = soup.find(id="mainContent") or soup
            links = main_content.find_all("a", href=True)

        count = 0
        for element in links:
            # Hvis elementet er en h3, må vi finne lenken inni
            link_tag = element.find("a") if element.name != "a" else element
            
            if not link_tag or not link_tag.has_attr('href'):
                continue

            tittel = link_tag.get_text().strip()
            href = link_tag['href']
            
            # Filtrer bort meny-lenker og støy
            if len(tittel) < 10 or "javascript" in href:
                continue
                
            # Lag full URL
            if href.startswith("/"):
                full_url = "https://www.regjeringen.no" + href
            else:
                full_url = href
            
            # Sjekk at lenken ser ut som en sak
            if "/id" not in full_url and "/dokumenter/" not in full_url:
                continue

            item_id = full_url # Bruker URL som unik ID
            
            analyze_item(
                source_name=name,
                title=tittel,
                description="Hentet fra Regjeringen.no",
                link=full_url,
                pub_date=datetime.utcnow(),
                item_id=item_id
            )
            count += 1
            
        logger.info(f"   Fant {count} lenker på høringssiden.")

    except Exception as e:
        logger.error(f"❌ Feil ved lesing av nettside: {e}")

def check_stortinget():
    logger.info("🏛️ Sjekker Stortinget (API)...")
    session = get_http_session()
    
    try:
        res = session.get("
