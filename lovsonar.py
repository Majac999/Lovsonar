import sqlite3
import feedparser
import logging
import os
import smtplib
import time
import requests
import hashlib
import re
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.header import Header
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from io import BytesIO
from pypdf import PdfReader
from collections import defaultdict
from bs4 import BeautifulSoup

# ===========================================
# 1. KONFIGURASJON & NØKKELORD
# ===========================================

KW_SEGMENT = [
    "byggevare", "byggevarehus", "trelast", "jernvare", "lavpris", "discount",
    "billigkjede", "gds", "diy", "ombruk", "materialbank", "produktdatabase",
    "byggtjeneste", "varehandel", "samvirkelag", "coop", "obs bygg",
    "byggforretning", "bygg og anlegg", "detaljhandel", "faghandel", 
    "nettbutikk", "e-handel", "innkjøpskjede"
]

KW_TOPIC = [
    "bærekraft", "sirkulær", "gjenvinning", "miljøkrav", "taksonomi", "esg", "espr",
    "ecodesign", "ppwr", "cbam", "csrd", "csddd", "aktsomhet", "green claims",
    "grønnvasking", "reach", "clp", "pfas", "eudr", "epbd", "byggevareforordning",
    "emballasje", "plastløftet", "merking", "digitalt produktpass", "dpp",
    "sporbarhet", "epd", "farlige stoffer", "biocid", "voc", "torv", "høringsnotat",
    "høringsfrist", "universell utforming", "tilgjengelighet", "crpd",
    "menneskerettigheter", "funksjonsnedsettelse", "diskriminering",
    "livsløpsanalyse", "karbonfotavtrykk", "co2", "klimagassutslipp",
    "avfallshåndtering", "renovasjon", "gjenbruk", "reparasjon",
    "produktansvar", "produsentansvar", "materialpass",
    "renovering", "rehab", "energimerking", "tek17", "tek10",
    "brannsikkerhet", "ce-merking", "byggevareforskriften",
    "byggeregler", "energieffektivisering", "passivhus",
    "energikrav", "klima", "miljødeklarasjon"
]

KW_NOISE = [
    "skriv ut", "verktøylinje", "del paragraf", "meny", "til toppen",
    "personvern", "til hovedinnhald", "hopp til innhold"
]

KW_CRITICAL = [
    "høringsfrist", "frist", "påminnelse", "forslag til endring", 
    "crpd", "vedtak", "ikrafttredelse", "overgangsordning",
    "implementering", "gjennomføring"
]

# ✅ AKTIVE KILDER (Uten # foran)
RSS_SOURCES = {
    "📢 Høringer": "https://www.regjeringen.no/no/dokument/horinger/id1763/?show=rss",
    "📘 Meldinger": "https://www.regjeringen.no/no/dokument/proposisjoner-og-meldinger/id1754/?show=rss",
    "📜 Proposisjoner": "https://www.regjeringen.no/no/dokument/proposisjoner-og-meldinger/id1754/?show=rss",
    "🇪🇺 Europapolitikk": "https://www.regjeringen.no/no/tema/europapolitikk/id1160/?show=rss",
    "⚖️ Menneskerettigheter": "https://www.regjeringen.no/no/tema/utenrikssaker/menneskerettigheter/id1160/?show=rss",
    "📚 NOU": "https://www.regjeringen.no/no/dokument/nou-er/id1767/?show=rss"
}

MAX_AGE_DAYS = {
    "📢 Høringer": 90,
    "🏛️ Stortinget": 60,
    "default": 180
}

DB_PATH = "lovsonar_seen.db"
USER_AGENT = "LovSonar/6.1 (Coop Obs BYGG Compliance)"
MAX_PDF_SIZE = 10_000_000  # 10MB

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# ===========================================
# 2. HJELPEFUNKSJONER
# ===========================================

def verify_config():
    required = ["EMAIL_USER", "EMAIL_PASS", "EMAIL_RECIPIENT"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.warning(f"⚠️ Mangler miljøvariabler for e-post: {', '.join(missing)}")
        return False
    logger.info("✅ E-postkonfigurasjon OK")
    return True

def get_http_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": USER_AGENT})
    return session

def unwrap_stortinget_list(obj, key_path):
    cur = obj
    for k in key_path.split('.'):
        if isinstance(cur, dict):
            cur = cur.get(k, {})
        else:
            return []
    if isinstance(cur, list):
        return cur
    if isinstance(cur, dict):
        for v in cur.values():
            if isinstance(v, list):
                return v
    return []

def parse_stortinget_date(value):
    if not value:
        return datetime.utcnow()
    if isinstance(value, str) and value.startswith("/Date("):
        try:
            ms = int(re.search(r"/Date\((\d+)", value).group(1))
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            return datetime.utcnow()
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return datetime.utcnow()

def make_stable_id(source, link, title):
    s = f"{source}|{link}|{title}".encode("utf-8")
    return hashlib.sha256(s).hexdigest()

def clean_text(text):
    if not text:
        return ""
    from html import unescape
    return " ".join(re.sub(r"<[^>]+>", " ", unescape(text)).split()).strip()

def hent_pdf_tekst_intern(session, url, maks_sider=10):
    try:
        try:
            head = session.head(url, timeout=10)
            content_length = int(head.headers.get("Content-Length", 0))
            if content_length > MAX_PDF_SIZE:
                logger.warning(f"📄 PDF for stor ({content_length / 1_000_000:.1f}MB), hopper over")
                return ""
        except Exception:
            pass
        
        r = session.get(url, timeout=30)
        r.raise_for_status()
        
        content_type = r.headers.get("Content-Type", "").lower()
        if "application/pdf" not in content_type and not url.lower().endswith(".pdf"):
            return ""
        
        if len(r.content) > MAX_PDF_SIZE:
            logger.warning(f"📄 PDF for stor ({len(r.content) / 1_000_000:.1f}MB), hopper over")
            return ""
        
        reader = PdfReader(BytesIO(r.content))
        tekst = []
        total_pages = len(reader.pages)
        
        for i in range(min(total_pages, maks_sider)):
            try:
                page_text = reader.pages[i].extract_text()
                if page_text:
                    tekst.append(page_text)
            except Exception as e:
                logger.warning(f"Kunne ikke lese side {i+1}: {e}")
                continue
        
        full_text = " ".join(tekst)
        if full_text:
            logger.info(f"📄 PDF lest OK ({total_pages} sider, {len(full_text)} tegn ekstrahert)")
        return full_text
        
    except Exception as e:
        logger.warning(f"Kunne ikke lese PDF ({url}): {e}")
        return ""

# ===========================================
# 3. ANALYSE LOGIKK
# ===========================================

def analyze_item(conn, session, source_name, title, description, link, pub_date, item_id):
    max_days = MAX_AGE_DAYS.get(source_name, MAX_AGE_DAYS["default"])
    if pub_date < (datetime.utcnow() - timedelta(days=max_days)):
        return
    
    if conn.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (str(item_id),)).fetchone():
        return

    full_text = f"{title} {description}"

    if link.lower().endswith(".pdf") or "høring" in title.lower():
        tillegg = hent_pdf_tekst_intern(session, link)
        if tillegg:
            full_text += " " + tillegg

    t = full_text.lower()
    
    if sum(1 for k in KW_NOISE if k in t) > 5:
        conn.execute(
            "INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)",
            (str(item_id), source_name, title, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return

    segment_score = sum(1 for k in KW_SEGMENT if k in t)
    topic_score = sum(1 for k in KW_TOPIC if k in t)
    critical_score = sum(1 for k in KW_CRITICAL if k in t)
    
    is_hearing = "høring" in source_name.lower() or "høring" in title.lower()
    
    is_relevant = (
        (segment_score >= 1 and topic_score >= 2) or
        critical_score >= 1 or
        (is_hearing and topic_score >= 1)
    )
    
    if is_relevant:
        logger.info(
            f"✅ TREFF ({source_name}): {title} "
            f"[segment={segment_score}, topic={topic_score}, critical={critical_score}]"
        )
        
        conn.execute(
            "INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)",
            (str(item_id), source_name, title, datetime.utcnow().isoformat()),
        )
        
        conn.execute(
            "INSERT INTO weekly_hits (source, title, description, link, pub_date, excerpt, "
            "detected_at, relevance_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_name, 
                title, 
                description, 
                link, 
                pub_date.isoformat(), 
                description[:500], 
                datetime.utcnow().isoformat(),
                segment_score + topic_score + critical_score
            ),
        )
        conn.commit()
    else:
        conn.execute(
            "INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)",
            (str(item_id), source_name, title, datetime.utcnow().isoformat()),
        )
        conn.commit()

# ===========================================
# 4. INNSAMLING MED FALLBACK (v6.1)
# ===========================================

def parse_rss_fallback(content):
    """
    Plan B: Bruk BeautifulSoup til å lese både RSS (<item>) og Atom (<entry>).
    """
    try:
        soup = BeautifulSoup(content, 'xml')
        items = soup.find_all(['item', 'entry']) # Hent både RSS og Atom
        
        if not items:
            soup = BeautifulSoup(content, 'html.parser')
            items = soup.find_all(['item', 'entry'])
            
        result = []
        for item in items:
            # Tittel
            title_tag = item.find('title')
            title = title_tag.get_text(strip=True) if title_tag else "Uten tittel"
            
            # Link
            link = ""
            link_tag = item.find('link')
            if link_tag:
                link = link_tag.get_text(strip=True) or link_tag.get('href', '')
            
            # Beskrivelse
            desc_tag = item.find(['description', 'summary', 'content'])
            desc = desc_tag.get_text(strip=True) if desc_tag else ""
            
            # Dato (Forenklet fallback: bruk 'nå')
            pub_date = datetime.utcnow()
            
            result.append({
                'title': title,
                'link': link,
                'description': desc,
                'pub_date': pub_date
            })
        return result
    except Exception as e:
        logger.warning(f"Fallback parsing feilet også: {e}")
        return []

def check_rss():
    session = get_http_session()
    with sqlite3.connect(DB_PATH) as conn:
        for name, url in RSS_SOURCES.items():
            logger.info(f"🔎 Leser RSS: {name}")
            try:
                r = session.get(url, timeout=20)
                if r.status_code >= 400:
                    continue
                
                # Prøv feedparser først
                feed = feedparser.parse(r.content)
                entries = []
                
                if getattr(feed, "bozo", 0) or not feed.entries:
                    # Fallback til BeautifulSoup (Plan B)
                    raw_items = parse_rss_fallback(r.content)
                    for item in raw_items:
                        class MockEntry: pass
                        e = MockEntry()
                        e.title = item['title']
                        e.link = item['link']
                        e.description = item['description']
                        e.published_parsed = item['pub_date'].timetuple()
                        entries.append(e)
                else:
                    entries = feed.entries

                items_processed = 0
                for entry in entries:
                    title = clean_text(getattr(entry, 'title', ''))
                    link = getattr(entry, 'link', '')
                    guid = make_stable_id(name, link, title)
                    
                    if hasattr(entry, 'published_parsed') and entry.published_parsed:
                        p_date = datetime(*entry.published_parsed[:6])
                    else:
                        p_date = datetime.utcnow()
                    
                    desc = clean_text(getattr(entry, 'description', ''))
                    
                    analyze_item(conn, session, name, title, desc, link, p_date, guid)
                    items_processed += 1
                
                logger.info(f"  ✓ Prosesserte {items_processed} items fra {name}")
                
            except Exception as e:
                logger.error(f"❌ Feil ved RSS {name}: {e}")

def check_stortinget():
    logger.info("🏛️ Poller Stortinget ...")
    session = get_http_session()
    
    with sqlite3.connect(DB_PATH) as conn:
        try:
            res = session.get("https://data.stortinget.no/eksport/sesjoner?format=json", timeout=20).json()
            sid = res.get("innevaerende_sesjon", {}).get("id", "2025-2026")
            logger.info(f"  Aktuell sesjon: {sid}")
            
            page = 1
            total_processed = 0
            
            while True:
                url = f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&pagesize=50&page={page}&format=json"
                data = session.get(url, timeout=20).json()
                saker = unwrap_stortinget_list(data, "saker_liste")
                
                if not saker:
                    break
                
                for sak in saker:
                    dg = str(sak.get("dokumentgruppe", "")).lower
        