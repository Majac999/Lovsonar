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

# Sjekker pdf_leser.py
try:
    import pdf_leser
except ImportError:
    pdf_leser = None

# ===========================================
# 1. KONFIGURASJON & NØKKELORD
# ===========================================

KW_SEGMENT = ["byggevare", "byggevarehus", "trelast", "jernvare", "lavpris", "discount", "billigkjede", "gds", "diy", "ombruk", "materialbank", "produktdatabase", "byggtjeneste", "varehandel", "samvirkelag", "coop", "obs bygg"]
KW_TOPIC = ["bærekraft", "sirkulær", "gjenvinning", "miljøkrav", "taksonomi", "esg", "espr", "ecodesign", "ppwr", "cbam", "csrd", "csddd", "aktsomhet", "green claims", "grønnvasking", "reach", "clp", "pfas", "eudr", "epbd", "byggevareforordning", "emballasje", "plastløftet", "merking", "digitalt produktpass", "dpp", "sporbarhet", "epd", "farlige stoffer", "biocid", "voc", "torv", "høringsnotat", "høringsfrist"]
KW_NOISE = ["skriv ut", "verktøylinje", "del paragraf", "meny", "til toppen", "personvern"]

RSS_SOURCES = {
    "📢 Høring": "https://www.regjeringen.no/no/dokument/horingar/id2000001/?show=rss",
    "📜 Lovforslag": "https://www.regjeringen.no/no/dokument/proposisjonar-og-meldingar/id2000161/?show=rss",
    "🇪🇺 EØS-notat": "https://www.regjeringen.no/no/dokument/eos-notat/id2000002/?show=rss",
    "📚 NOU": "https://www.regjeringen.no/no/dokument/nou-er/id2000003/?show=rss"
}
DB_PATH = "lovsonar_seen.db"
USER_AGENT = "LovSonar/2.6 (Strategic Compliance Tool)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# ===========================================
# 2. ROBUSTE HJELPEFUNKSJONER (Pkt 1, 4, 5)
# ===========================================

def get_http_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": USER_AGENT})
    return session

def unwrap_stortinget_list(obj, key_path):
    """Pkt 1: Graver ut listen fra Stortingets komplekse JSON-struktur."""
    cur = obj
    for k in key_path.split('.'):
        if isinstance(cur, dict):
            cur = cur.get(k, {})
        else:
            return []
    
    if isinstance(cur, list): return cur
    if isinstance(cur, dict):
        # Sjekker om det er en dypere 'sak' eller lignende boks
        for v in cur.values():
            if isinstance(v, list): return v
    return []

def make_stable_id(source, link, title):
    """Pkt 4: Lager en unik ID selv om RSS-feeden mangler GUID."""
    s = f"{source}|{link}|{title}".encode("utf-8")
    return hashlib.sha256(s).hexdigest()

def clean_text(text):
    if not text: return ""
    from html import unescape
    return " ".join(re.sub(r"<[^>]+>", " ", unescape(text)).split()).strip()

# ===========================================
# 3. ANALYSE-LOGIKK
# ===========================================

def analyze_item(source_name, title, description, link, pub_date, item_id):
    if pub_date < (datetime.utcnow() - timedelta(days=180)): return
    
    with sqlite3.connect(DB_PATH) as conn:
        if conn.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,)).fetchone():
            return

        full_text = f"{title} {description}"
        # Sjekk PDF hvis aktuelt
        if pdf_leser and (link.lower().endswith(".pdf") or "høring" in title.lower()):
            tillegg = pdf_leser.hent_pdf_tekst(link, maks_sider=10)
            if tillegg and "FEIL" not in tillegg:
                full_text += " " + tillegg

        # AND-logikk
        t = full_text.lower()
        has_segment = any(k in t for k in KW_SEGMENT)
        has_topic = any(k in t for k in KW_TOPIC)
        is_critical = any(k in t for k in ["høringsfrist", "forslag til endring"])

        if (has_segment and has_topic) or is_critical:
            logger.info(f"✅ TREFF: {title}")
            conn.execute("INSERT INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)", 
                         (item_id, source_name, title, datetime.utcnow().isoformat()))
            conn.execute("INSERT INTO weekly_hits (source, title, description, link, pub_date, excerpt, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                         (source_name, title, description, link, pub_date.isoformat(), description[:500], datetime.utcnow().isoformat()))
            conn.commit()
        else:
            # Marker som sett (støy)
            conn.execute("INSERT INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)", 
                         (item_id, source_name, title, datetime.utcnow().isoformat()))
            conn.commit()

# ===========================================
# 4. INNSAMLING (Pkt 3: RSS via Requests)
# ===========================================

def check_rss():
    session = get_http_session()
    for name, url in RSS_SOURCES.items():
        try:
            # Pkt 3: Bruker requests istedenfor feedparser direkte for timeout/retry
            r = session.get(url, timeout=15)
            r.raise_for_status()
            feed = feedparser.parse(r.text)
            
            for entry in feed.entries:
                title = clean_text(entry.get("title", ""))
                link = entry.get("link", "")
                guid = entry.get("guid") or make_stable_id(name, link, title)
                p_date = datetime(*entry.published_parsed[:6]) if hasattr(entry, "published_parsed") else datetime.utcnow()
                
                analyze_item(name, title, clean_text(entry.get("description", "")), link, p_date, guid)
        except Exception as e:
            logger.error(f"Feil ved RSS {name}: {e}")

def check_stortinget():
    logger.info("🏛️ Poller Stortinget...")
    session = get_http_session()
    try:
        res = session.get("https://data.stortinget.no/eksport/sesjoner?format=json", timeout=15).json()
        sid = res.get("innevaerende_sesjon", {}).get("id", "2025-2026")
        
        # Pkt 1: Håndterer paginering og wrapper-problemet
        page = 1 # Stortinget er ofte 1-basert
        while True:
            url = f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&pagesize=50&page={page}&format=json"
            data = session.get(url, timeout=15).json()
            
            # Bruker den nye unwrap-funksjonen for å finne lista
            saker = unwrap_stortinget_list(data, "saker_liste")
            if not saker: break
            
            for sak in saker:
                dg = str(sak.get("dokumentgruppe", "")).lower()
                if any(x in dg for x in ["spørsmål", "interpellasjon", "referat"]): continue
                
                # Pkt 2: Prøver å finne en reell dato
                p_date_str = sak.get("sist_oppdatert") or sak.get("registrert_dato")
                p_date = datetime.fromisoformat(p_date_str.replace("Z", "")) if p_date_str else datetime.utcnow()
                
                analyze_item("🏛️ Stortinget", sak.get("tittel", ""), f"Tema: {sak.get('tema','')}", f"https://stortinget.no/sak/{sak['id']}", p_date, f"ST-{sak['id']}")
            
            page += 1
            if page > 5: break # Sikkerhetsventil for å ikke hente hele historien
            time.sleep(1)
            
    except Exception as e:
        logger.error(f"Feil mot Stortinget: {e}")

# ===========================================
# 5. RAPPORTERING & DB SETUP
# ===========================================

def setup_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS seen_items (item_id TEXT PRIMARY KEY, source TEXT, title TEXT, date_seen TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS weekly_hits (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, title TEXT, description TEXT, link TEXT, pub_date TEXT, excerpt TEXT, detected_at TEXT)")
        conn.commit()

if __name__ == "__main__":
    setup_db()
    if os.environ.get("LOVSONAR_MODE") == "weekly":
        # (Her kommer din eksisterende e-post funksjon)
        pass
    else:
        check_rss()
        check_stortinget()
