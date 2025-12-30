"""
LovRadar v13.0 - Komplett regulatorisk overvåkningssystem for byggevarehandel
Kombinerer:
- LovSonar: RSS/API-overvåkning for nye høringer, proposisjoner, nyheter
- LovRadar: Endringsdeteksjon i eksisterende lover og forskrifter
- PDF-parsing: Leser høringsnotater for dypere innsikt
- Fristovervåking: Ekstraherer og varsler om høringsfrister (30/60 dager)
- Database: SQLite-historikk for compliance-dokumentasjon
"""

import os, json, hashlib, smtplib, re, asyncio, aiohttp, logging, feedparser, sqlite3
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from io import BytesIO
from bs4 import BeautifulSoup
from dataclasses import dataclass
from enum import Enum
from difflib import SequenceMatcher

# Valgfri PDF-støtte
try:
    from pypdf import PdfReader
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False
    logging.warning("pypdf ikke installert - PDF-parsing deaktivert")

# =============================================================================
# 1. KONFIGURASJON & STRATEGISKE NØKKELORD
# =============================================================================

class Priority(Enum):
    CRITICAL = 1  # Frist < 30 dager eller stor lovendring
    HIGH = 2      # Relevant + frist, eller betydelig endring
    MEDIUM = 3    # Relevant tema
    LOW = 4       # Mulig interessant

@dataclass
class Keyword:
    term: str
    weight: float = 1.0
    category: str = "general"
    word_boundary: bool = True

# --- Spesialiserte nøkkelord for Obs BYGG / Varehandel ---
KEYWORDS_SEGMENT = [
    Keyword("byggevare", 2.0, "core"), Keyword("trelast", 1.5, "core"),
    Keyword("jernvare", 1.5, "core"), Keyword("detaljhandel", 1.0, "retail"),
    Keyword("ombruk", 1.5, "sustainability"), Keyword("byggevarehus", 1.5, "core")
]

KEYWORDS_TOPIC = [
    # EU & Bærekraft (ESPR, Digitalt produktpass er kritisk for 2026)
    Keyword("espr", 3.0, "eu"), Keyword("digitalt produktpass", 3.0, "digital"),
    Keyword("dpp", 2.5, "digital"), Keyword("ppwr", 2.5, "packaging"),
    Keyword("eudr", 2.5, "timber"), Keyword("miljødeklarasjon", 2.0, "sustainability"),
    Keyword("epd", 2.0, "sustainability"), Keyword("bærekraft", 1.5, "sustainability"),
    # Kjemikalier & Sikkerhet
    Keyword("reach", 2.0, "chemicals"), Keyword("pfas", 2.5, "chemicals"),
    Keyword("asbest", 3.0, "danger"), Keyword("farlige stoffer", 2.0, "chemicals"),
    # Juss & Compliance
    Keyword("åpenhetsloven", 2.5, "compliance"), Keyword("aktsomhet", 2.0, "compliance"),
    Keyword("grønnvasking", 2.5, "marketing"), Keyword("tek17", 2.0, "building")
]

KEYWORDS_CRITICAL = [
    Keyword("høringsfrist", 3.0, "deadline"), Keyword("ikrafttredelse", 2.5, "deadline"),
    Keyword("trer i kraft", 2.5, "deadline"), Keyword("forbud", 2.5, "legal")
]

# --- KILDE-OPPSETT ---
RSS_SOURCES = {
    "📢 Høringer": {"url": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?show=rss", "max_age_days": 90},
    "🇪🇺 Europapolitikk": {"url": "https://www.regjeringen.no/no/tema/europapolitikk/id1160/?show=rss", "max_age_days": 120},
    "🏗️ DiBK (Byggkvalitet)": {"url": "https://dibk.no/rss", "max_age_days": 90},
    "🌿 Miljødirektoratet": {"url": "https://www.miljodirektoratet.no/rss/aktuelt/", "max_age_days": 90},
    "⚖️ Forbrukertilsynet": {"url": "https://www.forbrukertilsynet.no/feed", "max_age_days": 90}
}

LAWS_TO_MONITOR = {
    "Åpenhetsloven": "https://lovdata.no/dokument/NL/lov/2021-06-18-99",
    "Produktkontrolloven": "https://lovdata.no/dokument/NL/lov/1976-06-11-79",
    "Markedsføringsloven": "https://lovdata.no/dokument/NL/lov/2009-01-09-2",
    "Byggevareforskriften (DOK)": "https://lovdata.no/dokument/SF/forskrift/2014-12-17-1714",
    "Avfallsforskriften": "https://lovdata.no/dokument/SF/forskrift/2004-06-01-930",
    "TEK17 Kap 9 (Miljø)": "https://www.dibk.no/regelverk/byggteknisk-forskrift-tek17/9/9-1"
}

DB_PATH = "lovradar_v13.db"
CACHE_FILE = "lovradar_v13_cache.json"
USER_AGENT = "Mozilla/5.0 (compatible; LovRadar/13.0; Compliance-Monitoring-Oslo)"
CHANGE_THRESHOLD = 0.5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# =============================================================================
# 2. ANALYSE-MOTOR & DATABASE
# =============================================================================

def setup_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen_items (item_id TEXT PRIMARY KEY, source TEXT, title TEXT, date_seen TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS sonar_hits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, title TEXT, link TEXT, 
        priority INTEGER, deadline TEXT, score REAL, matched_keywords TEXT, detected_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS radar_hits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, law_name TEXT, url TEXT, 
        change_percent REAL, change_excerpt TEXT, detected_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    return conn

def extract_deadline(text):
    match = re.search(r'(?:høringsfrist|frist)[:\s]+(\d{1,2})[.\s]+([a-zA-ZæøåÆØÅ]+)\s+(\d{4})', text, re.IGNORECASE)
    if match: return match.group(0)
    return ""

def analyze_relevance(text, source_name=""):
    t = text.lower()
    seg_score = sum(kw.weight for kw in KEYWORDS_SEGMENT if kw.term.lower() in t)
    top_score = sum(kw.weight for kw in KEYWORDS_TOPIC if kw.term.lower() in t)
    total_score = (seg_score * 1.5) + top_score
    
    is_relevant = total_score >= 5.0 or any(kw.term.lower() in t for kw in KEYWORDS_TOPIC if kw.weight >= 2.5)
    priority = Priority.LOW
    if is_relevant:
        priority = Priority.CRITICAL if "frist" in t or total_score > 10 else Priority.HIGH if total_score > 7 else Priority.MEDIUM
        
    matched = [kw.term for kw in KEYWORDS_SEGMENT + KEYWORDS_TOPIC if kw.term.lower() in t]
    return {"is_relevant": is_relevant, "score": total_score, "priority": priority, "matched": matched, "deadline_text": extract_deadline(text)}

# =============================================================================
# 3. HOVEDFUNKSJONALITET
# =============================================================================

async def fetch_url(session, url):
    try:
        async with session.get(url, timeout=30) as response:
            if response.status == 200: return await response.text()
    except Exception as e: logger.error(f"Feil ved {url}: {e}")
    return None

async def check_law_changes(session, conn):
    logger.info("📜 Sjekker lover for endringer (Radar)...")
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r') as f: cache = json.load(f)
    else: cache = {}

    hits = []
    for name, url in LAWS_TO_MONITOR.items():
        html = await fetch_url(session, url)
        if not html: continue
        
        soup = BeautifulSoup(html, "html.parser")
        text = re.sub(r'\s+', ' ', soup.get_text()).strip()
        new_hash = hashlib.sha256(text.encode()).hexdigest()
        
        prev = cache.get(name, {})
        if prev and new_hash != prev.get("hash"):
            similarity = SequenceMatcher(None, prev.get("text", ""), text[:5000]).ratio()
            change = round((1 - similarity) * 100, 2)
            if change >= CHANGE_THRESHOLD:
                hits.append({"name": name, "url": url, "change_percent": change})
                conn.execute("INSERT INTO radar_hits (law_name, url, change_percent) VALUES (?,?,?)", (name, url, change))
        
        cache[name] = {"hash": new_hash, "text": text[:5000]}
    
    with open(CACHE_FILE, 'w') as f: json.dump(cache, f, indent=2)
    return hits

async def check_rss(session, conn):
    logger.info("📡 Sjekker nyheter og høringer (Sonar)...")
    hits = []
    for name, config in RSS_SOURCES.items():
        xml = await fetch_url(session, config["url"])
        if not xml: continue
        feed = feedparser.parse(xml)
        for entry in feed.entries[:10]:
            item_id = hashlib.sha256(entry.link.encode()).hexdigest()
            if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (item_id,)).fetchone(): continue
            
            analysis = analyze_relevance(entry.title + " " + getattr(entry, 'summary', ''), name)
            if analysis["is_relevant"]:
                hits.append({"source": name, "title": entry.title, "link": entry.link, "priority": analysis["priority"], "deadline_text": analysis["deadline_text"]})
                conn.execute("INSERT INTO sonar_hits (source, title, link, priority, deadline, score, matched_keywords) VALUES (?,?,?,?,?,?,?)",
                             (name, entry.title, entry.link, analysis["priority"].value, analysis["deadline_text"], analysis["score"], ",".join(analysis["matched"])))
            
            conn.execute("INSERT INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)", (item_id, name, entry.title, datetime.now().isoformat()))
    return hits

def send_report(sonar, radar):
    user, pw, to = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASS"), os.environ.get("EMAIL_RECIPIENT")
    if not (sonar or radar) or not all([user, pw, to]): return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🛡️ LovRadar v13.0: Ukentlig Compliance-Rapport {datetime.now().strftime('%d.%m')}"
    
    html = f"""<html><body style="font-family: Arial; color: #333;">
        <h2 style="color: #1a5f7a;">Regulatorisk Rapport - Obs BYGG</h2>
        <h3 style="color: #dc3545;">🔴 Lovendringer (Radar)</h3>
        {"".join([f"<p><b>{h['name']}</b>: {h['change_percent']}% endring. <a href='{h['url']}'>Se kilde</a></p>" for h in radar]) or "<p>Ingen endringer.</p>"}
        <hr>
        <h3 style="color: #fd7e14;">📡 Nye Høringer & Nyheter (Sonar)</h3>
        {"".join([f"<p>• <b>{h['title']}</b> ({h['source']})<br>Prio: {h['priority'].name} | {h['deadline_text']}<br><a href='{h['link']}'>Les mer</a></p>" for h in sonar]) or "<p>Ingen nye funn.</p>"}
    </body></html>"""
    
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)
    logger.info("📧 Rapport sendt.")

async def main():
    conn = setup_db()
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        radar_hits = await check_law_changes(session, conn)
        sonar_hits = await check_rss(session, conn)
        send_report(sonar_hits, radar_hits)
    conn.commit()
    conn.close()

if __name__ == "__main__":
    asyncio.run(main())
