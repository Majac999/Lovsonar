+   1 """LovSonar v7.1 - Regulatorisk radar for byggevarehandel"""
+   2 import sqlite3, feedparser, logging, os, smtplib, re, hashlib, asyncio, aiohttp
+   3 from datetime import datetime
+   4 from email.mime.text import MIMEText
+   5 from email.mime.multipart import MIMEMultipart
+   6 from io import BytesIO
+   7 from pypdf import PdfReader
+   8 from dataclasses import dataclass
+   9 from typing import Optional
+  10 from enum import Enum
+  11 
+  12 # --- 1. KONFIGURASJON ---
+  13 class Priority(Enum):
+  14     CRITICAL = 1; HIGH = 2; MEDIUM = 3; LOW = 4
+  15 
+  16 @dataclass
+  17 class Keyword:
+  18     term: str; weight: float = 1.0; word_boundary: bool = True
+  19 
+  20 KEYWORDS_SEGMENT = [
+  21     Keyword("byggevare", 2.0), Keyword("trelast", 1.5), Keyword("jernvare", 1.5),
+  22     Keyword("detaljhandel", 1.5), Keyword("varehandel", 1.5), 
+  23     Keyword("faghandel", 1.0), Keyword("ombruk", 1.5), Keyword("byggforretning", 1.5)
+  24 ]
+  25 
+  26 KEYWORDS_TOPIC = [
+  27     Keyword("byggevareforordning", 3.0), Keyword("espr", 2.5), Keyword("ppwr", 2.5),
+  28     Keyword("digitalt produktpass", 3.0), Keyword("dpp", 2.5), Keyword("åpenhetsloven", 2.5), 
+  29     Keyword("grønnvasking", 2.0), Keyword("pfas", 2.5), Keyword("reach", 2.0),
+  30     Keyword("tek17", 2.0), Keyword("miljøkrav", 1.5), Keyword("sirkulær", 2.0),
+  31     Keyword("epd", 2.0), Keyword("ce-merking", 2.0), Keyword("emballasje", 1.5)
+  32 ]
+  33 
+  34 KEYWORDS_CRITICAL = [
+  35     Keyword("høringsfrist", 3.0), Keyword("frist", 2.0), Keyword("ikrafttredelse", 2.5), 
+  36     Keyword("vedtak", 1.5), Keyword("trer i kraft", 2.5)
+  37 ]
+  38 
+  39 RSS_SOURCES = {
+  40     "📢 Høringer": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?show=rss",
+  41     "🇪🇺 Europapolitikk": "https://www.regjeringen.no/no/tema/europapolitikk/id1160/?show=rss",
+  42     "📚 NOU": "https://www.regjeringen.no/no/dokument/nou-er/id1767/?show=rss",
+  43 }
+  44 
+  45 DB_PATH = "lovsonar.db"
+  46 USER_AGENT = "Mozilla/5.0 (compatible; LovSonar/7.1)"
+  47 MAX_PDF_SIZE = 10_000_000
+  48 
+  49 logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
+  50 logger = logging.getLogger(__name__)
+  51 
+  52 # --- 2. ANALYSE ---
+  53 MONTHS_NO = {'januar':1,'februar':2,'mars':3,'april':4,'mai':5,'juni':6,
+  54              'juli':7,'august':8,'september':9,'oktober':10,'november':11,'desember':12}
+  55 
+  56 def extract_deadline(text: str) -> tuple[Optional[datetime], str]:
+  57     patterns = [
+  58         r'(?:høringsfrist|frist)[:\s]+(\d{1,2})[.\s]+(\w+)\s+(\d{4})',
+  59         r'innen\s+(\d{1,2})[.\s]+(\w+)\s+(\d{4})',
+  60     ]
+  61     for pattern in patterns:
+  62         match = re.search(pattern, text, re.IGNORECASE)
+  63         if match:
+  64             try:
+  65                 day, month_name, year = int(match.group(1)), match.group(2).lower(), int(match.group(3))
+  66                 if month_name in MONTHS_NO:
+  67                     return datetime(year, MONTHS_NO[month_name], day), match.group(0)
+  68             except (ValueError, KeyError):
+  69                 continue
+  70     return None, ""
+  71 
+  72 def match_keyword(text: str, kw: Keyword) -> bool:
+  73     if kw.word_boundary:
+  74         return bool(re.search(r'\b' + re.escape(kw.term) + r'\b', text, re.IGNORECASE))
+  75     return kw.term.lower() in text.lower()
+  76 
+  77 def analyze_content(text: str, source_name: str) -> dict:
+  78     t = text.lower()
+  79     segment_score = sum(kw.weight for kw in KEYWORDS_SEGMENT if match_keyword(t, kw))
+  80     topic_score = sum(kw.weight for kw in KEYWORDS_TOPIC if match_keyword(t, kw))
+  81     critical_score = sum(kw.weight for kw in KEYWORDS_CRITICAL if match_keyword(t, kw))
+  82     
+  83     matched = [kw.term for kw in KEYWORDS_SEGMENT + KEYWORDS_TOPIC + KEYWORDS_CRITICAL if match_keyword(t, kw)]
+  84     total_score = segment_score * 1.5 + topic_score + critical_score * 2.0
+  85     
+  86     deadline, deadline_text = extract_deadline(text)
+  87     is_hearing = "høring" in source_name.lower()
+  88     
+  89     is_relevant = (
+  90         (segment_score >= 1.5 and topic_score >= 2.0) or
+  91         critical_score >= 2.0 or
+  92         (is_hearing and topic_score >= 3.0) or
+  93         total_score >= 8.0
+  94     )
+  95     
+  96     priority = Priority.LOW
+  97     if is_relevant:
+  98         if deadline:
+  99             days_until = (deadline - datetime.now()).days
+ 100             if days_until <= 30: priority = Priority.CRITICAL
+ 101             elif days_until <= 60: priority = Priority.HIGH
+ 102             else: priority = Priority.MEDIUM
+ 103         elif critical_score >= 3.0: priority = Priority.HIGH
+ 104         elif total_score >= 10.0: priority = Priority.MEDIUM
+ 105     
+ 106     return {
+ 107         "is_relevant": is_relevant, "score": total_score, "priority": priority,
+ 108         "matched": matched, "deadline": deadline, "deadline_text": deadline_text
+ 109     }
+ 110 
+ 111 # --- 3. DATABASE ---
+ 112 def setup_db() -> sqlite3.Connection:
+ 113     conn = sqlite3.connect(DB_PATH)
+ 114     conn.execute("""CREATE TABLE IF NOT EXISTS seen_items (
+ 115         item_id TEXT PRIMARY KEY, source TEXT, title TEXT, date_seen TEXT)""")
+ 116     conn.execute("""CREATE TABLE IF NOT EXISTS weekly_hits (
+ 117         id INTEGER PRIMARY KEY AUTOINCREMENT,
+ 118         source TEXT, title TEXT, link TEXT, excerpt TEXT,
+ 119         priority INTEGER, deadline TEXT, deadline_text TEXT,
+ 120         relevance_score REAL, matched_keywords TEXT,
+ 121         detected_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
+ 122     conn.commit()
+ 123     return conn
+ 124 
+ 125 # --- 4. INNHENTING ---
+ 126 async def fetch_pdf_text(session: aiohttp.ClientSession, url: str) -> str:
+ 127     try:
+ 128         async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
+ 129             if r.status != 200: return ""
+ 130             content = await r.read()
+ 131             if len(content) > MAX_PDF_SIZE: return ""
+ 132             reader = PdfReader(BytesIO(content))
+ 133             texts = [page.extract_text() or "" for page in reader.pages[:5]]
+ 134             return " ".join(texts)
+ 135     except Exception as e:
+ 136         logger.debug(f"PDF-feil {url}: {e}")
+ 137         return ""
+ 138 
+ 139 def unwrap_stortinget_list(data: dict, key: str) -> list:
+ 140     obj = data.get(key, {})
+ 141     if isinstance(obj, list): return obj
+ 142     if isinstance(obj, dict):
+ 143         for v in obj.values():
+ 144             if isinstance(v, list): return v
+ 145     return []
+ 146 
+ 147 async def check_stortinget(session: aiohttp.ClientSession, conn: sqlite3.Connection):
+ 148     logger.info("🏛️ Sjekker Stortinget...")
+ 149     try:
+ 150         async with session.get("https://data.stortinget.no/eksport/sesjoner?format=json") as r:
+ 151             sessions = await r.json()
+ 152             sid = sessions.get("innevaerende_sesjon", {}).get("id", "2025-2026")
+ 153         
+ 154         url = f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&pagesize=100&format=json"
+ 155         async with session.get(url) as r:
+ 156             data = await r.json()
+ 157         
+ 158         saker = unwrap_stortinget_list(data, "saker_liste")
+ 159         hits = 0
+ 160         for sak in saker:
+ 161             sak_id = sak.get("id")
+ 162             if not sak_id: continue
+ 163             doc_type = str(sak.get("dokumentgruppe", "")).lower()
+ 164             if any(x in doc_type for x in ["spørsmål", "interpellasjon", "referat"]): continue
+ 165             
+ 166             item_id = f"ST-{sak_id}"
+ 167             if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (item_id,)).fetchone(): continue
+ 168             
+ 169             title = sak.get("tittel", ""); tema = sak.get("tema", ""); link = f"https://stortinget.no/sak/{sak_id}"
+ 170             result = analyze_content(f"{title} {tema}", "Stortinget")
+ 171             
+ 172             if result["is_relevant"]:
+ 173                 conn.execute("""INSERT INTO weekly_hits 
+ 174                     (source, title, link, excerpt, priority, deadline, deadline_text, relevance_score, matched_keywords)
+ 175                     VALUES (?,?,?,?,?,?,?,?,?)""",
+ 176                     ("🏛️ Stortinget", title, link, f"Tema: {tema}", result["priority"].value,
+ 177                      result["deadline"].isoformat() if result["deadline"] else None,
+ 178                      result["deadline_text"], result["score"], ",".join(result["matched"][:10])))
+ 179                 hits += 1
+ 180             
+ 181             conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
+ 182                         (item_id, "Stortinget", title, datetime.now().isoformat()))
+ 183         conn.commit()
+ 184         logger.info(f"  ✓ {hits} relevante saker")
+ 185     except Exception as e: logger.error(f"Stortinget-feil: {e}")
+ 186 
+ 187 async def process_rss(session: aiohttp.ClientSession, name: str, url: str, conn: sqlite3.Connection):
+ 188     logger.info(f"🔎 Sjekker: {name}")
+ 189     try:
+ 190         async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
+ 191             if r.status >= 400: return
+ 192             content = await r.read()
+ 193         feed = feedparser.parse(content)
+ 194         hits = 0
+ 195         for entry in feed.entries:
+ 196             title = getattr(entry, 'title', ''); link = getattr(entry, 'link', '')
+ 197             summary = getattr(entry, 'summary', getattr(entry, 'description', ''))
+ 198             item_id = hashlib.sha256(f"{name}|{link}|{title}".encode()).hexdigest()
+ 199             if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (item_id,)).fetchone(): continue
+ 200             
+ 201             full_text = f"{title} {summary}"
+ 202             if link.lower().endswith(".pdf") or "høring" in title.lower():
+ 203                 pdf_text = await fetch_pdf_text(session, link)
+ 204                 if pdf_text: full_text += " " + pdf_text
+ 205             
+ 206             result = analyze_content(full_text, name)
+ 207             if result["is_relevant"]:
+ 208                 conn.execute("""INSERT INTO weekly_hits 
+ 209                     (source, title, link, excerpt, priority, deadline, deadline_text, relevance_score, matched_keywords)
+ 210                     VALUES (?,?,?,?,?,?,?,?,?)""",
+ 211                     (name, title, link, summary[:500], result["priority"].value,
+ 212                      result["deadline"].isoformat() if result["deadline"] else None,
+ 213                      result["deadline_text"], result["score"], ",".join(result["matched"][:10])))
+ 214                 hits += 1
+ 215             
+ 216             conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
+ 217                         (item_id, name, title, datetime.now().isoformat()))
+ 218         conn.commit()
+ 219         logger.info(f"  ✓ {hits} relevante treff")
+ 220     except Exception as e: logger.error(f"RSS-feil {name}: {e}")
+ 221 
+ 222 # --- 5. RAPPORT ---
+ 223 def generate_html_report(rows: list) -> str:
+ 224     colors = {1: "#dc3545", 2: "#fd7e14", 3: "#ffc107", 4: "#28a745"}
+ 225     labels = {1: "🚨 KRITISK", 2: "⚠️ HØY", 3: "📋 MEDIUM", 4: "📌 LAV"}
+ 226     now = datetime.now().strftime('%Y-%m-%d')
+ 227     html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
+ 228     body {{ font-family: sans-serif; max-width: 700px; margin: 20px auto; background: #f5f5f5; }}
+ 229     .header {{ background: linear-gradient(135deg, #1a5f7a, #086972); color: white; padding: 25px; border-radius: 10px; }}
+ 230     .item {{ background: white; border-radius: 8px; margin: 15px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); overflow: hidden; }}
+ 231     .item-head {{ padding: 15px; border-left: 5px solid; }}
+ 232     .item-body {{ padding: 15px; border-top: 1px solid #eee; font-size: 14px; color: #444; }}
+ 233     .deadline {{ background: #dc3545; color: white; padding: 3px 8px; border-radius: 3px; font-size: 12px; }}
+ 234     .kw {{ display: inline-block; background: #e9ecef; padding: 2px 6px; border-radius: 3px; font-size: 11px; margin: 2px; }}
+ 235     a {{ color: #1a5f7a; text-decoration: none; font-weight: bold; }}
+ 236     </style></head><body><div class="header"><h2>🛡️ LovSonar: Regulatorisk Radar</h2>
+ 237     <p>{len(rows)} treff identifisert | {now}</p></div>"""
+ 238     for row in rows:
+ 239         source, title, link, excerpt, priority, d_text, score, kw = row[1], row[2], row[3], row[4], row[5], row[7], row[8], row[9]
+ 240         dl_html = f'<span class="deadline">⏰ {d_text}</span>' if d_text else ""
+ 241         kw_html = "".join(f'<span class="kw">{k}</span>' for k in (kw or "").split(",")[:6] if k)
+ 242         html += f"""<div class="item"><div class="item-head" style="border-color: {colors.get(priority, '#ddd')};">
+ 243         <strong>{source}</strong> | {labels.get(priority, 'INFO')} | Score: {score:.1f} {dl_html}<br>
+ 244         <a href="{link}" target="_blank">{title}</a></div><div class="item-body">{excerpt[:400]}...<br>{kw_html}</div></div>"""
+ 245     return html + f"<p style='text-align:center; font-size:12px;'>LovSonar v7.1</p></body></html>"
+ 246 
+ 247 def send_weekly_report():
+ 248     user, pw, to = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASS"), os.environ.get("EMAIL_RECIPIENT")
+ 249     if not all([user, pw, to]):
+ 250         logger.warning("⚠️ Mangler EMAIL_USER, EMAIL_PASS eller EMAIL_RECIPIENT")
+ 251         return
+ 252     conn = sqlite3.connect(DB_PATH)
+ 253     rows = conn.execute("SELECT * FROM weekly_hits WHERE detected_at > datetime('now', '-7 days') ORDER BY priority ASC, relevance_score DESC").fetchall()
+ 254     conn.close()
+ 255     if not rows:
+ 256         logger.info("ℹ️ Ingen treff å rapportere")
+ 257         return
+ 258     msg = MIMEMultipart("alternative")
+ 259     msg["Subject"] = f"🛡️ LovSonar: {len(rows)} treff (uke {datetime.now().isocalendar()[1]})"
+ 260     msg["From"], msg["To"] = user, to
+ 261     msg.attach(MIMEText(generate_html_report(rows), "html", "utf-8"))
+ 262     try:
+ 263         with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
+ 264             s.login(user, pw); s.send_message(msg)
+ 265         logger.info(f"📧 Rapport sendt til {to}")
+ 266     except Exception as e: logger.error(f"E-postfeil: {e}")
+ 267 
+ 268 # --- 6. MAIN ---
+ 269 async def run_radar():
+ 270     conn = setup_db()
+ 271     async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
+ 272         tasks = [process_rss(session, n, u, conn) for n, u in RSS_SOURCES.items()]
+ 273         tasks.append(check_stortinget(session, conn))
+ 274         await asyncio.gather(*tasks)
+ 275     conn.close()
+ 276 
+ 277 if __name__ == "__main__":
+ 278     logger.info("🚀 LovSonar v7.1 starter...")
+ 279     mode = os.environ.get("LOVSONAR_MODE", "daily").lower()
+ 280     if mode == "weekly": send_weekly_report()
+ 281     else: asyncio.run(run_radar())
+ 282     logger.info("✅ Ferdig!")
