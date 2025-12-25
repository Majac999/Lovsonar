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
from html import unescape
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
    "plastløftet", "emballasje", "klimaavgift", "digitale produktpass", "dpp"
]

# RSS-LENKER (Ferdig oppsatt med Høringer, NOU, Prop og Meldinger)
RSS_SOURCES = {
    "📢 Høringer": "https://www.regjeringen.no/no/aktuelt/horinger/id1763/rss",
    "📚 NOU (Utredninger)": "https://www.regjeringen.no/no/dokument/nou-er/id1767/rss",
    "📜 Lovforslag & Meldinger": "https://www.regjeringen.no/no/dokument/proposisjoner-og-meldinger/id1754/rss",
    "🇪🇺 EØS-notater": "https://www.regjeringen.no/no/tema/europapolitikk/eos-notater/id669358/rss"
}

DB_PATH = "lovsonar_seen.db"
USER_AGENT = "LovSonar/2.1 (Internal Compliance Tool)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# ===========================================
# 2. NETTVERK & HJELPEFUNKSJONER
# ===========================================

def get_http_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
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

def get_publish_date(entry):
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6])
    return datetime.utcnow()

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
# 4. ANALYSE (Med PDF-filter)
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
                time.sleep(1) # Høflig pause mot server
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
# 5. INNSAMLING
# ===========================================

def check_rss_feeds():
    for name, url in RSS_SOURCES.items():
        logger.info(f"📡 Sjekker {name}...")
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": USER_AGENT})
            
            if hasattr(feed, "status") and feed.status >= 400:
                logger.error(f"❌ HTTP feil mot {name}: {feed.status}")
                continue

            for entry in feed.entries:
                item_id = entry.get("guid") or entry.get("link")
                pub_date = get_publish_date(entry)
                
                analyze_item(
                    source_name=name,
                    title=clean_text(entry.get("title", "")),
                    description=clean_text(entry.get("description", "")),
                    link=entry.get("link", ""),
                    pub_date=pub_date,
                    item_id=item_id
                )
        except Exception as e:
            logger.error(f"❌ Feil ved lesing av RSS {name}: {e}")

def check_stortinget():
    logger.info("🏛️ Sjekker Stortinget...")
    session = get_http_session()
    
    try:
        res = session.get("https://data.stortinget.no/eksport/sesjoner?format=json", timeout=10)
        res.raise_for_status()
        sid = res.json()["innevaerende_sesjon"]["id"]
        
        res_saker = session.get(f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&format=json", timeout=10)
        res_saker.raise_for_status()
        data = res_saker.json()
        
        for sak in data.get("saker_liste", []):
            dg = str(sak.get("dokumentgruppe") or "").lower()
            if any(x in dg for x in ["spørsmål", "interpellasjon", "referat", "skriftlig"]): 
                continue
                
            item_id = f"STORTINGET-{sak['id']}"
            tittel = sak.get("tittel", "")
            tema = sak.get("tema", "") or ""
            
            analyze_item(
                source_name="🏛️ Stortingssak",
                title=tittel,
                description=f"Type: {dg}. Tema: {tema}.",
                link=f"https://stortinget.no/sak/{sak['id']}",
                pub_date=datetime.utcnow(),
                item_id=item_id
            )
            
    except Exception as e:
        logger.error(f"❌ Feil mot Stortinget API: {e}")

# ===========================================
# 6. RAPPORTERING
# ===========================================

def send_weekly_report():
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT source, title, link, excerpt, pub_date 
            FROM weekly_hits 
            WHERE detected_at >= ? 
            ORDER BY pub_date DESC
        """, (cutoff,)).fetchall()

    if not rows:
        logger.info("Ingen treff denne uken.")
        return

    md_text = [f"# 🛡️ LovSonar: {len(rows)} treff"]
    md_text.append(f"Rapportdato: {datetime.now().strftime('%d.%m.%Y')}\n")

    for r in rows:
        source, title, link, excerpt, pdate = r
        try: d_str = datetime.fromisoformat(pdate).strftime('%d.%m')
        except: d_str = "N/A"
        
        md_text.append(f"## {title}")
        md_text.append(f"**Kilde:** {source} | **Dato:** {d_str}")
        md_text.append(f"[Les saken]({link})\n")
        md_text.append(f"> {excerpt[:800]}...\n")
        md_text.append("---")

    company_context = os.environ.get("COMPANY_CONTEXT", "Ingen profil funnet.")
    md_text.append("\n### 🤖 ANALYSE-KONTEKST (Kopier til AI)")
    md_text.append(company_context)
    md_text.append("\n**OPPGAVE:** Analyser sakene over. Påvirker dette Obs BYGG/Coop?")

    full_msg = "\n".join(md_text)
    
    email_user = os.environ.get("EMAIL_USER")
    email_pass = os.environ.get("EMAIL_PASS")
    email_to = os.environ.get("EMAIL_RECIPIENT", email_user)

    if email_user and email_pass:
        msg = MIMEText(full_msg, "plain", "utf-8")
        msg["Subject"] = Header(f"LovSonar: {len(rows)} treff", "utf-8")
        msg["From"] = email_user
        msg["To"] = email_to

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
                server.login(email_user, email_pass)
                server.send_message(msg)
            logger.info("📧 Rapport sendt!")
        except Exception as e:
            logger.error(f"Feil ved sending: {e}")
    else:
        logger.warning("Mangler e-post oppsett. Printer rapport til logg.")
        print(full_msg)

# ===========================================
# MAIN
# ===========================================

if __name__ == "__main__":
    setup_database()
    purge_old_data()
    
    mode = os.environ.get("LOVSONAR_MODE", "daily").lower()
    
    if mode == "weekly":
        logger.info("Kjører ukesrapport...")
        send_weekly_report()
    else:
        logger.info("Kjører daglig innsamling...")
        check_rss_feeds()
        check_stortinget()
