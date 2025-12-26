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
    print("⚠️ ADVARSEL: Fant ikke pdf_leser.py. PDF-analyse vil ikke fungere.")

# ===========================================
# 1. KONFIGURASJON & NØKKELORD
# ===========================================

KW_SEGMENT = ["byggevare", "byggevarehus", "trelast", "jernvare", "lavpris", "discount", "billigkjede", "gds", "diy", "ombruk", "materialbank", "produktdatabase", "byggtjeneste", "varehandel", "samvirkelag", "coop", "obs bygg"]
KW_TOPIC = ["bærekraft", "sirkulær", "gjenvinning", "miljøkrav", "taksonomi", "esg", "espr", "ecodesign", "ppwr", "cbam", "csrd", "csddd", "aktsomhet", "green claims", "grønnvasking", "reach", "clp", "pfas", "eudr", "epbd", "byggevareforordning", "emballasje", "plastløftet", "merking", "digitalt produktpass", "dpp", "sporbarhet", "epd", "farlige stoffer", "biocid", "voc", "torv", "høringsnotat", "høringsfrist", "universell utforming", "tilgjengelighet", "crpd"]
KW_NOISE = ["skriv ut", "verktøylinje", "del paragraf", "meny", "til toppen", "personvern"]

# OPPDATERTE URL-ER (Fikser 404-feil)
RSS_SOURCES = {
    "📢 Høring": "https://www.regjeringen.no/no/dokument/horingar/id2000001/?show=rss",
    "📜 Lovforslag": "https://www.regjeringen.no/no/dokument/proposisjonar-og-meldingar/id2000161/?show=rss",
    "🇪🇺 EØS-notat": "https://www.regjeringen.no/no/dokument/eos-notat/id2000002/?show=rss",
    "📚 NOU": "https://www.regjeringen.no/no/dokument/nou-er/id2000003/?show=rss"
}

DB_PATH = "lovsonar_seen.db"
USER_AGENT = "LovSonar/2.7 (Strategic Compliance Tool)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# ===========================================
# 2. ROBUSTE HJELPEFUNKSJONER
# ===========================================

def get_http_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry)) 
    session.headers.update({"User-Agent": USER_AGENT})
    return session

def unwrap_stortinget_list(obj, key_path):
    """Graver ut listen fra Stortingets komplekse JSON-struktur."""
    cur = obj
    for k in key_path.split('.'):
        if isinstance(cur, dict):
            cur = cur.get(k, {})
        else:
            return []
    
    if isinstance(cur, list): return cur
    if isinstance(cur, dict):
        for v in cur.values():
            if isinstance(v, list): return v
    return []

def make_stable_id(source, link, title):
    """Lager en unik ID selv om RSS-feeden mangler GUID."""
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
        if conn.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (str(item_id),)).fetchone():
            return

        full_text = f"{title} {description}"
        
        # Sjekk PDF hvis aktuelt
        if pdf_leser and (link.lower().endswith(".pdf") or "høring" in title.lower()):
            try:
                tillegg = pdf_leser.hent_pdf_tekst(link, maks_sider=10)
                if tillegg and "FEIL" not in tillegg:
                    full_text += " " + tillegg
            except Exception as e:
                logger.warning(f"Kunne ikke lese PDF for {title}: {e}")

        # AND-logikk
        t = full_text.lower()
        if sum(1 for k in KW_NOISE if k in t) > 3: return # Støyfilter

        has_segment = any(k in t for k in KW_SEGMENT)
        has_topic = any(k in t for k in KW_TOPIC)
        is_critical = any(k in t for k in ["høringsfrist", "forslag til endring"])

        if (has_segment and has_topic) or is_critical:
            logger.info(f"✅ TREFF: {title}")
            conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)", 
                         (str(item_id), source_name, title, datetime.utcnow().isoformat()))
            conn.execute("INSERT INTO weekly_hits (source, title, description, link, pub_date, excerpt, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                         (source_name, title, description, link, pub_date.isoformat(), description[:500], datetime.utcnow().isoformat()))
            conn.commit()
        else:
            # Marker som sett (støy)
            conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)", 
                         (str(item_id), source_name, title, datetime.utcnow().isoformat()))
            conn.commit()

# ===========================================
# 4. INNSAMLING (RSS & Stortinget)
# ===========================================

def check_rss():
    session = get_http_session()
    for name, url in RSS_SOURCES.items():
        try:
            r = session.get(url, timeout=15)
            r.raise_for_status()
            feed = feedparser.parse(r.text)
            
            for entry in feed.entries:
                title = clean_text(entry.get("title", ""))
                link = entry.get("link", "")
                guid = entry.get("guid") or make_stable_id(name, link, title)
                
                # Robust datoparsing
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    p_date = datetime(*entry.published_parsed[:6])
                else:
                    p_date = datetime.utcnow()
                
                analyze_item(name, title, clean_text(entry.get("description", "")), link, p_date, guid)
        except Exception as e:
            logger.error(f"Feil ved RSS {name}: {e}")

def check_stortinget():
    logger.info("🏛️ Poller Stortinget...")
    session = get_http_session()
    try:
        res = session.get("https://data.stortinget.no/eksport/sesjoner?format=json", timeout=15).json()
        sid = res.get("innevaerende_sesjon", {}).get("id", "2025-2026")
        
        page = 1
        while True:
            url = f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&pagesize=50&page={page}&format=json"
            data = session.get(url, timeout=15).json()
            
            saker = unwrap_stortinget_list(data, "saker_liste")
            if not saker: break
            
            for sak in saker:
                dg = str(sak.get("dokumentgruppe", "")).lower()
                if any(x in dg for x in ["spørsmål", "interpellasjon", "referat"]): continue
                
                p_date_str = sak.get("sist_oppdatert") or sak.get("registrert_dato")
                p_date = datetime.fromisoformat(p_date_str.replace("Z", "")) if p_date_str else datetime.utcnow()
                
                analyze_item("🏛️ Stortinget", sak.get("tittel", ""), f"Tema: {sak.get('tema','')}", f"https://stortinget.no/sak/{sak['id']}", p_date, f"ST-{sak['id']}")
            
            page += 1
            if page > 5: break 
            time.sleep(1)
            
    except Exception as e:
        logger.error(f"Feil mot Stortinget: {e}")

# ===========================================
# 5. DATABASE & RAPPORTERING
# ===========================================

def setup_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS seen_items (item_id TEXT PRIMARY KEY, source TEXT, title TEXT, date_seen TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS weekly_hits (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, title TEXT, description TEXT, link TEXT, pub_date TEXT, excerpt TEXT, detected_at TEXT)")
        
        # Indekser for ytelse
        conn.execute("CREATE INDEX IF NOT EXISTS idx_weekly_detected ON weekly_hits(detected_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_source ON seen_items(source)")
        
        conn.commit()

def send_weekly_report():
    email_user = os.environ.get("EMAIL_USER", "").strip()
    email_pass = os.environ.get("EMAIL_PASS", "").strip()
    email_to = os.environ.get("EMAIL_RECIPIENT", email_user).strip()
    
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
        source, title, link, excerpt, p_date = r
        d_str = p_date[:10]
        md_text.append(f"## {title}")
        md_text.append(f"**Kilde:** {source} | **Dato:** {d_str} | [Åpne sak]({link})")
        md_text.append(f"> {excerpt}\n")
        md_text.append("---")
    
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
    setup_db()
    if os.environ.get("LOVSONAR_MODE", "daily").lower() == "weekly":
        send_weekly_report()
    else:
        check_rss()
        check_stortinget()
