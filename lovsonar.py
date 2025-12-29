import sqlite3, feedparser, logging, os, smtplib, re, hashlib, asyncio, aiohttp, json, time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from io import BytesIO
from pypdf import PdfReader
from collections import defaultdict
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

# --- 1. KONFIGURASJON & PRIORITET ---
class Priority(Enum):
    CRITICAL = 1; HIGH = 2; MEDIUM = 3; LOW = 4

@dataclass
class Keyword:
    term: str; weight: float = 1.0; category: str = "general"; require_word_boundary: bool = True

# Dine spesialtilpassede nøkkelord for Obs BYGG
KEYWORDS_SEGMENT = [
    Keyword("byggevare", 2.0), Keyword("trelast", 1.5), Keyword("obs bygg", 2.0, require_word_boundary=False),
    Keyword("coop", 2.0, require_word_boundary=False), Keyword("detaljhandel", 1.0), Keyword("ombruk", 1.5)
]

KEYWORDS_TOPIC = [
    Keyword("byggevareforordning", 3.0), Keyword("espr", 2.5), Keyword("ppwr", 2.5),
    Keyword("dpp", 2.5), Keyword("åpenhetsloven", 2.5), Keyword("grønnvasking", 2.0),
    Keyword("pfas", 2.5), Keyword("tek17", 2.0), Keyword("miljøkrav", 1.5)
]

KEYWORDS_CRITICAL = [
    Keyword("høringsfrist", 3.0), Keyword("frist", 2.0), Keyword("ikrafttredelse", 2.5), Keyword("vedtak", 1.5)
]

RSS_SOURCES = {
    "📢 Høringer": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?show=rss",
    "🇪🇺 Europapolitikk": "https://www.regjeringen.no/no/tema/europapolitikk/id1160/?show=rss",
    "🏗️ DiBK Nyheter": "https://dibk.no/rss",
    "🌿 Miljødirektoratet": "https://www.miljodirektoratet.no/rss/aktuelt/"
}

DB_PATH = "lovsonar_v7.db"
USER_AGENT = "LovSonar/7.0 (Coop Obs BYGG Strategic Intelligence)"
MAX_PDF_SIZE = 10_000_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- 2. JURIDISK ANALYSE-MOTOR ---
def extract_deadline(text: str) -> tuple[Optional[datetime], str]:
    patterns = [r'(?:høringsfrist|frist)[:\s]+(\d{1,2})[.\s]+(\w+)\s+(\d{4})']
    months_no = {'januar':1,'februar':2,'mars':3,'april':4,'mai':5,'juni':6,'juli':7,'august':8,'september':9,'oktober':10,'november':11,'desember':12}
    match = re.search(patterns[0], text, re.IGNORECASE)
    if match:
        try:
            day, m_name, year = int(match.group(1)), match.group(2).lower(), int(match.group(3))
            if m_name in months_no: return datetime(year, months_no[m_name], day), match.group(0)
        except: pass
    return None, ""

def analyze_content(text: str, source_name: str):
    t = text.lower(); score = 0.0; matched = []
    for kw in KEYWORDS_SEGMENT + KEYWORDS_TOPIC + KEYWORDS_CRITICAL:
        if kw.require_word_boundary:
            if re.search(r'\b' + re.escape(kw.term.lower()) + r'\b', t):
                score += kw.weight; matched.append(kw.term)
        elif kw.term.lower() in t:
            score += kw.weight; matched.append(kw.term)
    
    deadline, d_text = extract_deadline(text)
    is_relevant = score >= 4.0 or "høring" in t
    
    priority = Priority.LOW
    if is_relevant:
        if deadline and (deadline - datetime.now()).days <= 30: priority = Priority.CRITICAL
        elif score >= 10.0: priority = Priority.HIGH
        elif score >= 5.0: priority = Priority.MEDIUM
    
    return is_relevant, score, priority, matched, deadline, d_text

# --- 3. INNHENTING (RSS + PDF + STORTINGET) ---
async def fetch_pdf_text(session, url):
    try:
        async with session.get(url, timeout=20) as r:
            if r.status == 200:
                content = await r.read()
                if len(content) < MAX_PDF_SIZE:
                    reader = PdfReader(BytesIO(content))
                    return " ".join([p.extract_text() for p in reader.pages[:5]])
    except: return ""
    return ""

async def check_stortinget(session, db_conn):
    logger.info("🏛️ Poller Stortinget JSON-API...")
    try:
        async with session.get("https://data.stortinget.no/eksport/saker?sesjonid=2025-2026&pagesize=100&format=json") as r:
            data = await r.json()
            for sak in data.get("saker_liste", []):
                item_id = f"ST-{sak['id']}"
                if db_conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (item_id,)).fetchone(): continue
                
                title = sak.get("tittel", ""); link = f"https://stortinget.no/sak/{sak['id']}"
                rel, score, pri, match, dl, dl_t = analyze_content(f"{title} {sak.get('tema','')}", "Stortinget")
                
                if rel:
                    db_conn.execute("INSERT INTO weekly_hits (source, title, link, excerpt, priority, relevance_score) VALUES (?,?,?,?,?,?)",
                                   ("🏛️ Stortinget", title, link, f"Tema: {sak.get('tema','')}", pri.value, score))
                db_conn.execute("INSERT INTO seen_items (item_id, date_seen) VALUES (?,?)", (item_id, datetime.now().isoformat()))
            db_conn.commit()
    except Exception as e: logger.error(f"Stortinget-feil: {e}")

async def process_rss(session, name, url, db_conn):
    logger.info(f"🔎 Sjekker RSS: {name}...")
    try:
        async with session.get(url) as r:
            feed = feedparser.parse(await r.text())
            for entry in feed.entries:
                item_id = hashlib.sha256(f"{entry.link}{entry.title}".encode()).hexdigest()
                if db_conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (item_id,)).fetchone(): continue
                
                full_text = f"{entry.title} {getattr(entry, 'summary', '')}"
                if "høring" in entry.title.lower() or entry.link.endswith(".pdf"):
                    full_text += await fetch_pdf_text(session, entry.link)
                
                rel, score, pri, match, dl, dl_t = analyze_content(full_text, name)
                if rel:
                    db_conn.execute("INSERT INTO weekly_hits (source, title, link, excerpt, priority, deadline_text, relevance_score) VALUES (?,?,?,?,?,?,?)",
                                   (name, entry.title, entry.link, full_text[:500], pri.value, dl_t, score))
                db_conn.execute("INSERT INTO seen_items (item_id, date_seen) VALUES (?,?)", (item_id, datetime.now().isoformat()))
            db_conn.commit()
    except Exception as e: logger.error(f"RSS-feil {name}: {e}")

# --- 4. RAPPORT & MAIN ---
def generate_html_report(rows):
    colors = {1: "#dc3545", 2: "#fd7e14", 3: "#ffc107", 4: "#28a745"}
    html = "<html><body style='font-family: sans-serif;'><h2>🛡️ LovSonar Ukesrapport</h2>"
    for r in rows:
        html += f"<div style='border-left: 5px solid {colors.get(r[4], '#eee')}; padding: 10px; margin: 10px; background: #f9f9f9;'>"
        html += f"<b>{r[0]}</b>: <a href='{r[2]}'>{r[1]}</a> (Score: {r[7]})<br>"
        if r[6]: html += f"<b style='color: red;'>⏰ Frist: {r[6]}</b><br>"
        html += f"<p style='font-size: 13px;'>{r[3]}...</p></div>"
    return html + "</body></html>"

async def run_radar():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen_items (item_id TEXT PRIMARY KEY, date_seen TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS weekly_hits (source TEXT, title TEXT, link TEXT, excerpt TEXT, priority INTEGER, deadline_text TEXT, relevance_score REAL, detected_at DEFAULT CURRENT_TIMESTAMP)")
    
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        tasks = [process_rss(session, n, u, conn) for n, u in RSS_SOURCES.items()]
        tasks.append(check_stortinget(session, conn))
        await asyncio.gather(*tasks)
    conn.close()

def send_mail():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT * FROM weekly_hits WHERE detected_at > datetime('now', '-7 days') ORDER BY priority ASC").fetchall()
    if not rows: return
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🛡️ LovSonar: {len(rows)} relevante treff denne uken"
    msg.attach(MIMEText(generate_html_report(rows), "html", "utf-8"))
    
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["EMAIL_USER"], os.environ["EMAIL_PASS"])
        s.send_message(msg, from_addr=os.environ["EMAIL_USER"], to_addrs=os.environ["EMAIL_RECIPIENT"])
    conn.close()

if __name__ == "__main__":
    mode = os.environ.get("LOVSONAR_MODE", "daily")
    if mode == "weekly": send_mail()
    else: asyncio.run(run_radar())
