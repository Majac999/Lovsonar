"""LovSonar v7.3 - Spesialtilpasset Obs BYGG (Med bransjefilter)"""
import sqlite3, feedparser, logging, os, smtplib, re, hashlib, asyncio, aiohttp
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from io import BytesIO
from pypdf import PdfReader
from dataclasses import dataclass
from typing import Optional
from enum import Enum

# --- 1. KONFIGURASJON ---
class Priority(Enum):
    CRITICAL = 1; HIGH = 2; MEDIUM = 3; LOW = 4

@dataclass
class Keyword:
    term: str; weight: float = 1.0; word_boundary: bool = True

# Bransje-nøkkelord (Må matches for at frister skal gjelde)
KEYWORDS_SEGMENT = [
    Keyword("byggevare", 2.0, False), 
    Keyword("trelast", 1.5, False), 
    Keyword("jernvare", 1.5, False),
    Keyword("obs bygg", 2.0, False), 
    Keyword("detaljhandel", 1.0, False), 
    Keyword("ombruk", 1.5, False), 
    Keyword("byggforretning", 1.5, False)
]

KEYWORDS_TOPIC = [
    Keyword("byggevareforordning", 3.0, False), Keyword("espr", 2.5, False), 
    Keyword("ppwr", 2.5, False), Keyword("digitalt produktpass", 3.0, False), 
    Keyword("dpp", 2.5, False), Keyword("åpenhetsloven", 2.5, False), 
    Keyword("grønnvasking", 2.0, False), Keyword("pfas", 2.5, False), 
    Keyword("tek17", 2.0, False), Keyword("miljøkrav", 1.5, False), 
    Keyword("epd", 2.0, False), Keyword("emballasje", 1.5, False)
]

KEYWORDS_CRITICAL = [
    Keyword("høringsfrist", 3.0, False), Keyword("frist", 2.0, False), 
    Keyword("ikrafttredelse", 2.5, False), Keyword("vedtak", 1.5, False)
]

RSS_SOURCES = {
    "📢 Høringer": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?show=rss",
    "🇪🇺 Europapolitikk": "https://www.regjeringen.no/no/tema/europapolitikk/id1160/?show=rss",
    "📚 NOU": "https://www.regjeringen.no/no/dokument/nou-er/id1767/?show=rss",
}

# Ny database for å sikre en ren skanning med den nye logikken
DB_PATH = "lovsonar_v7_3.db"
USER_AGENT = "LovSonar/7.3 (Obs BYGG Strategic Intelligence)"
MAX_PDF_SIZE = 10_000_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- 2. ANALYSE-MOTOR ---
MONTHS_NO = {'januar':1,'februar':2,'mars':3,'april':4,'mai':5,'juni':6,
             'juli':7,'august':8,'september':9,'oktober':10,'november':11,'desember':12}

def extract_deadline(text: str) -> tuple[Optional[datetime], str]:
    patterns = [
        r'(?:høringsfrist|frist)[:\s]+(\d{1,2})[.\s]+(\w+)\s+(\d{4})',
        r'innen\s+(\d{1,2})[.\s]+(\w+)\s+(\d{4})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                day, month_name, year = int(match.group(1)), match.group(2).lower(), int(match.group(3))
                if month_name in MONTHS_NO:
                    return datetime(year, MONTHS_NO[month_name], day), match.group(0)
            except: continue
    return None, ""

def match_keyword(text: str, kw: Keyword) -> bool:
    if kw.word_boundary:
        return bool(re.search(r'\b' + re.escape(kw.term) + r'\b', text, re.IGNORECASE))
    return kw.term.lower() in text.lower()

def analyze_content(text: str, source_name: str) -> dict:
    t = text.lower()
    segment_score = sum(kw.weight for kw in KEYWORDS_SEGMENT if match_keyword(t, kw))
    topic_score = sum(kw.weight for kw in KEYWORDS_TOPIC if match_keyword(t, kw))
    critical_score = sum(kw.weight for kw in KEYWORDS_CRITICAL if match_keyword(t, kw))
    
    matched = [kw.term for kw in KEYWORDS_SEGMENT + KEYWORDS_TOPIC + KEYWORDS_CRITICAL if match_keyword(t, kw)]
    total_score = segment_score * 1.5 + topic_score + critical_score * 2.0
    
    deadline, deadline_text = extract_deadline(text)
    is_hearing = "høring" in source_name.lower()
    
    # OPPGRADERT LOGIKK: Krever bransje-treff (segment_score > 0) for å godta kritiske ord (som frist)
    is_relevant = (
        (segment_score >= 1.5 and topic_score >= 2.0) or  # Bransje + Tema
        (segment_score > 0 and critical_score >= 2.0) or  # Bransje + Kritisk (Frist/Vedtak)
        (is_hearing and topic_score >= 3.0) or           # Ren høring om tunge temaer
        total_score >= 10.0                               # Svært høy totalscore
    )
    
    priority = Priority.LOW
    if is_relevant:
        if deadline:
            days_until = (deadline - datetime.now()).days
            if days_until <= 30: priority = Priority.CRITICAL
            elif days_until <= 60: priority = Priority.HIGH
            else: priority = Priority.MEDIUM
        elif critical_score >= 3.0: priority = Priority.HIGH
        elif total_score >= 10.0: priority = Priority.MEDIUM
    
    return {
        "is_relevant": is_relevant, "score": total_score, "priority": priority,
        "matched": matched, "deadline": deadline, "deadline_text": deadline_text
    }

# --- 3. DATABASE ---
def setup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS seen_items (
        item_id TEXT PRIMARY KEY, source TEXT, title TEXT, date_seen TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS weekly_hits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT, title TEXT, link TEXT, excerpt TEXT,
        priority INTEGER, deadline TEXT, deadline_text TEXT,
        relevance_score REAL, matched_keywords TEXT,
        detected_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    return conn

# --- 4. INNHENTING ---
async def fetch_pdf_text(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200: return ""
            content = await r.read()
            if len(content) > MAX_PDF_SIZE: return ""
            reader = PdfReader(BytesIO(content))
            texts = [page.extract_text() or "" for page in reader.pages[:5]]
            return " ".join(texts)
    except: return ""

def unwrap_stortinget_list(data: dict, key: str) -> list:
    obj = data.get(key, {})
    if isinstance(obj, list): return obj
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list): return v
    return []

async def check_stortinget(session: aiohttp.ClientSession, conn: sqlite3.Connection):
    logger.info("🏛️ Sjekker Stortinget...")
    try:
        async with session.get("https://data.stortinget.no/eksport/sesjoner?format=json") as r:
            sessions = await r.json()
            sid = sessions.get("innevaerende_sesjon", {}).get("id", "2024-2025")
        
        url = f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&pagesize=500&format=json"
        async with session.get(url) as r:
            data = await r.json()
        
        saker = unwrap_stortinget_list(data, "saker_liste")
        hits = 0
        for sak in saker:
            sak_id = sak.get("id")
            if not sak_id: continue
            doc_type = str(sak.get("dokumentgruppe", "")).lower()
            if any(x in doc_type for x in ["spørsmål", "interpellasjon", "referat"]): continue
            
            item_id = f"ST-{sak_id}"
            if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (item_id,)).fetchone(): continue
            
            title, tema = sak.get("tittel", ""), sak.get("tema", "")
            result = analyze_content(f"{title} {tema}", "Stortinget")
            
            if result["is_relevant"]:
                conn.execute("""INSERT INTO weekly_hits 
                    (source, title, link, excerpt, priority, deadline, deadline_text, relevance_score, matched_keywords)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    ("🏛️ Stortinget", title, f"https://stortinget.no/sak/{sak_id}", f"Tema: {tema}", result["priority"].value,
                     result["deadline"].isoformat() if result["deadline"] else None,
                     result["deadline_text"], result["score"], ",".join(result["matched"][:10])))
                hits += 1
            
            conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
                        (item_id, "Stortinget", title, datetime.now().isoformat()))
        conn.commit()
        logger.info(f"  ✓ {hits} relevante saker fra Stortinget")
    except Exception as e: logger.error(f"Stortinget-feil: {e}")

async def process_rss(session: aiohttp.ClientSession, name: str, url: str, conn: sqlite3.Connection):
    logger.info(f"🔎 Sjekker: {name}")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status >= 400: return
            content = await r.read()
        feed = feedparser.parse(content)
        hits = 0
        for entry in feed.entries:
            title, link = getattr(entry, 'title', ''), getattr(entry, 'link', '')
            summary = getattr(entry, 'summary', getattr(entry, 'description', ''))
            item_id = hashlib.sha256(f"{name}|{link}|{title}".encode()).hexdigest()
            if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (item_id,)).fetchone(): continue
            
            full_text = f"{title} {summary}"
            if link.lower().endswith(".pdf") or "høring" in title.lower():
                pdf_text = await fetch_pdf_text(session, link)
                if pdf_text: full_text += " " + pdf_text
            
            result = analyze_content(full_text, name)
            if result["is_relevant"]:
                conn.execute("""INSERT INTO weekly_hits 
                    (source, title, link, excerpt, priority, deadline, deadline_text, relevance_score, matched_keywords)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (name, title, link, summary[:500], result["priority"].value,
                     result["deadline"].isoformat() if result["deadline"] else None,
                     result["deadline_text"], result["score"], ",".join(result["matched"][:10])))
                hits += 1
            
            conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
                        (item_id, name, title, datetime.now().isoformat()))
        conn.commit()
        logger.info(f"  ✓ {hits} relevante fra {name}")
    except Exception as e: logger.error(f"RSS-feil {name}: {e}")

# --- 5. RAPPORT ---
def generate_html_report(rows: list) -> str:
    colors = {1: "#dc3545", 2: "#fd7e14", 3: "#ffc107", 4: "#28a745"}
    labels = {1: "🚨 KRITISK", 2: "⚠️ HØY", 3: "📋 MEDIUM", 4: "📌 LAV"}
    now = datetime.now().strftime('%Y-%m-%d')
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    body {{ font-family: sans-serif; max-width: 700px; margin: 20px auto; background: #f5f5f5; }}
    .header {{ background: linear-gradient(135deg, #1a5f7a, #086972); color: white; padding: 25px; border-radius: 10px; }}
    .item {{ background: white; border-radius: 8px; margin: 15px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); overflow: hidden; }}
    .item-head {{ padding: 15px; border-left: 5px solid; }}
    .item-body {{ padding: 15px; border-top: 1px solid #eee; font-size: 14px; color: #444; }}
    .deadline {{ background: #dc3545; color: white; padding: 3px 8px; border-radius: 3px; font-size: 12px; }}
    .kw {{ display: inline-block; background: #e9ecef; padding: 2px 6px; border-radius: 3px; font-size: 11px; margin: 2px; }}
    a {{ color: #1a5f7a; text-decoration: none; font-weight: bold; }}
    </style></head><body><div class="header"><h2>🛡️ LovSonar Ukesrapport</h2>
    <p>{len(rows)} relevante treff funnet for Obs BYGG | {now}</p></div>"""
    for row in rows:
        source, title, link, excerpt, priority, d_text, score, kw = row[1], row[2], row[3], row[4], row[5], row[7], row[8], row[9]
        dl_html = f'<span class="deadline">⏰ {d_text}</span>' if d_text else ""
        kw_html = "".join(f'<span class="kw">{k}</span>' for k in (kw or "").split(",")[:6] if k)
        html += f"""<div class="item"><div class="item-head" style="border-color: {colors.get(priority, '#ddd')};">
        <strong>{source}</strong> | {labels.get(priority, 'INFO')} | Score: {score:.1f} {dl_html}<br>
        <a href="{link}" target="_blank">{title}</a></div><div class="item-body">{excerpt[:400]}...<br>{kw_html}</div></div>"""
    return html + "</body></html>"

def send_report():
    user, pw, to = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASS"), os.environ.get("EMAIL_RECIPIENT")
    if not all([user, pw, to]): return
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT * FROM weekly_hits WHERE detected_at > datetime('now', '-7 days') ORDER BY priority ASC, relevance_score DESC").fetchall()
    conn.close()
    if not rows: return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🛡️ LovSonar: {len(rows)} relevante treff"
    msg["From"], msg["To"] = user, to
    msg.attach(MIMEText(generate_html_report(rows), "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw); s.send_message(msg)
        logger.info("📧 E-post sendt.")
    except Exception as e: logger.error(f"E-postfeil: {e}")

async def run_radar():
    conn = setup_db()
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        tasks = [process_rss(session, n, u, conn) for n, u in RSS_SOURCES.items()]
        tasks.append(check_stortinget(session, conn))
        await asyncio.gather(*tasks, return_exceptions=True)
    send_report()
    conn.close()

if __name__ == "__main__":
    logger.info("🚀 LovSonar v7.3 starter...")
    asyncio.run(run_radar())
    logger.info("✅ Ferdig!")
