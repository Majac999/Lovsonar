+   1 """LovSonar v7.1 - Kompakt versjon med alle fiksene"""
+   2 import sqlite3, feedparser, logging, os, smtplib, re, hashlib, asyncio, aiohttp
+   3 from datetime import datetime, timedelta
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
+  22     Keyword("obs bygg", 2.0, False), Keyword("coop", 1.5, False), 
+  23     Keyword("detaljhandel", 1.0), Keyword("ombruk", 1.5), Keyword("byggforretning", 1.5)
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
+  46 USER_AGENT = "LovSonar/7.1 (Coop Obs BYGG)"
+  47 MAX_PDF_SIZE = 10_000_000
+  48 
+  49 logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
+  50 logger = logging.getLogger(__name__)
+  51 
+  52 # --- 2. ANALYSE-MOTOR ---
+  53 MONTHS_NO = {'januar':1,'februar':2,'mars':3,'april':4,'mai':5,'juni':6,
+  54              'juli':7,'august':8,'september':9,'oktober':10,'november':11,'desember':12}
+  55 
+  56 def extract_deadline(text: str) -> tuple[Optional[datetime], str]:
+  57     """Ekstraher høringsfrist fra tekst"""
+  58     patterns = [
+  59         r'(?:høringsfrist|frist)[:\s]+(\d{1,2})[.\s]+(\w+)\s+(\d{4})',
+  60         r'innen\s+(\d{1,2})[.\s]+(\w+)\s+(\d{4})',
+  61     ]
+  62     for pattern in patterns:
+  63         match = re.search(pattern, text, re.IGNORECASE)
+  64         if match:
+  65             try:
+  66                 day, month_name, year = int(match.group(1)), match.group(2).lower(), int(match.group(3))
+  67                 if month_name in MONTHS_NO:
+  68                     return datetime(year, MONTHS_NO[month_name], day), match.group(0)
+  69             except (ValueError, KeyError):
+  70                 continue
+  71     return None, ""
+  72 
+  73 def match_keyword(text: str, kw: Keyword) -> bool:
+  74     """Match nøkkelord med valgfri ordgrense"""
+  75     if kw.word_boundary:
+  76         return bool(re.search(r'\b' + re.escape(kw.term) + r'\b', text, re.IGNORECASE))
+  77     return kw.term.lower() in text.lower()
+  78 
+  79 def analyze_content(text: str, source_name: str) -> dict:
+  80     """Analyser innhold for relevans"""
+  81     t = text.lower()
+  82     segment_score = sum(kw.weight for kw in KEYWORDS_SEGMENT if match_keyword(t, kw))
+  83     topic_score = sum(kw.weight for kw in KEYWORDS_TOPIC if match_keyword(t, kw))
+  84     critical_score = sum(kw.weight for kw in KEYWORDS_CRITICAL if match_keyword(t, kw))
+  85     
+  86     matched = [kw.term for kw in KEYWORDS_SEGMENT + KEYWORDS_TOPIC + KEYWORDS_CRITICAL if match_keyword(t, kw)]
+  87     total_score = segment_score * 1.5 + topic_score + critical_score * 2.0
+  88     
+  89     deadline, deadline_text = extract_deadline(text)
+  90     is_hearing = "høring" in source_name.lower()
+  91     
+  92     # Relevans: segment + topic ELLER kritiske ord ELLER høring med tema
+  93     is_relevant = (
+  94         (segment_score >= 1.5 and topic_score >= 2.0) or
+  95         critical_score >= 2.0 or
+  96         (is_hearing and topic_score >= 3.0) or
+  97         total_score >= 8.0
+  98     )
+  99     
+ 100     # Prioritet basert på frist og score
+ 101     priority = Priority.LOW
+ 102     if is_relevant:
+ 103         if deadline:
+ 104             days_until = (deadline - datetime.now()).days
+ 105             if days_until <= 30: priority = Priority.CRITICAL
+ 106             elif days_until <= 60: priority = Priority.HIGH
+ 107             else: priority = Priority.MEDIUM
+ 108         elif critical_score >= 3.0: priority = Priority.HIGH
+ 109         elif total_score >= 10.0: priority = Priority.MEDIUM
+ 110     
+ 111     return {
+ 112         "is_relevant": is_relevant, "score": total_score, "priority": priority,
+ 113         "matched": matched, "deadline": deadline, "deadline_text": deadline_text
+ 114     }
+ 115 
+ 116 # --- 3. DATABASE ---
+ 117 def setup_db() -> sqlite3.Connection:
+ 118     conn = sqlite3.connect(DB_PATH)
+ 119     conn.execute("""CREATE TABLE IF NOT EXISTS seen_items (
+ 120         item_id TEXT PRIMARY KEY, source TEXT, title TEXT, date_seen TEXT)""")
+ 121     conn.execute("""CREATE TABLE IF NOT EXISTS weekly_hits (
+ 122         id INTEGER PRIMARY KEY AUTOINCREMENT,
+ 123         source TEXT, title TEXT, link TEXT, excerpt TEXT,
+ 124         priority INTEGER, deadline TEXT, deadline_text TEXT,
+ 125         relevance_score REAL, matched_keywords TEXT,
+ 126         detected_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
+ 127     conn.commit()
+ 128     return conn
+ 129 
+ 130 # --- 4. INNHENTING ---
+ 131 async def fetch_pdf_text(session: aiohttp.ClientSession, url: str) -> str:
+ 132     """Hent og les PDF asynkront"""
+ 133     try:
+ 134         async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
+ 135             if r.status != 200:
+ 136                 return ""
+ 137             content = await r.read()
+ 138             if len(content) > MAX_PDF_SIZE:
+ 139                 return ""
+ 140             reader = PdfReader(BytesIO(content))
+ 141             texts = []
+ 142             for page in reader.pages[:5]:
+ 143                 try:
+ 144                     texts.append(page.extract_text() or "")
+ 145                 except Exception:
+ 146                     continue
+ 147             return " ".join(texts)
+ 148     except Exception as e:
+ 149         logger.debug(f"PDF-feil {url}: {e}")
+ 150         return ""
+ 151 
+ 152 def unwrap_stortinget_list(data: dict, key: str) -> list:
+ 153     """Naviger i Stortingets nestet JSON"""
+ 154     obj = data.get(key, {})
+ 155     if isinstance(obj, list):
+ 156         return obj
+ 157     if isinstance(obj, dict):
+ 158         for v in obj.values():
+ 159             if isinstance(v, list):
+ 160                 return v
+ 161     return []
+ 162 
+ 163 async def check_stortinget(session: aiohttp.ClientSession, conn: sqlite3.Connection):
+ 164     """Sjekk Stortinget for relevante saker"""
+ 165     logger.info("🏛️ Sjekker Stortinget...")
+ 166     try:
+ 167         # Hent sesjon
+ 168         async with session.get("https://data.stortinget.no/eksport/sesjoner?format=json") as r:
+ 169             sessions = await r.json()
+ 170             sid = sessions.get("innevaerende_sesjon", {}).get("id", "2024-2025")
+ 171         
+ 172         # Hent saker
+ 173         url = f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&pagesize=100&format=json"
+ 174         async with session.get(url) as r:
+ 175             data = await r.json()
+ 176         
+ 177         saker = unwrap_stortinget_list(data, "saker_liste")
+ 178         hits = 0
+ 179         
+ 180         for sak in saker:
+ 181             sak_id = sak.get("id")
+ 182             if not sak_id:
+ 183                 continue
+ 184             
+ 185             # Filtrer ut spørsmål/interpellasjoner
+ 186             doc_type = str(sak.get("dokumentgruppe", "")).lower()
+ 187             if any(x in doc_type for x in ["spørsmål", "interpellasjon", "referat"]):
+ 188                 continue
+ 189             
+ 190             item_id = f"ST-{sak_id}"
+ 191             if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (item_id,)).fetchone():
+ 192                 continue
+ 193             
+ 194             title = sak.get("tittel", "")
+ 195             tema = sak.get("tema", "")
+ 196             link = f"https://stortinget.no/sak/{sak_id}"
+ 197             full_text = f"{title} {tema}"
+ 198             
+ 199             result = analyze_content(full_text, "Stortinget")
+ 200             
+ 201             if result["is_relevant"]:
+ 202                 conn.execute("""INSERT INTO weekly_hits 
+ 203                     (source, title, link, excerpt, priority, deadline, deadline_text, relevance_score, matched_keywords)
+ 204                     VALUES (?,?,?,?,?,?,?,?,?)""",
+ 205                     ("🏛️ Stortinget", title, link, f"Tema: {tema}", result["priority"].value,
+ 206                      result["deadline"].isoformat() if result["deadline"] else None,
+ 207                      result["deadline_text"], result["score"], ",".join(result["matched"][:10])))
+ 208                 hits += 1
+ 209                 logger.info(f"  ✅ {title[:50]}... (score={result['score']:.1f})")
+ 210             
+ 211             conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
+ 212                         (item_id, "Stortinget", title, datetime.now().isoformat()))
+ 213         
+ 214         conn.commit()
+ 215         logger.info(f"  ✓ {hits} relevante saker fra Stortinget")
+ 216         
+ 217     except Exception as e:
+ 218         logger.error(f"Stortinget-feil: {e}")
+ 219 
+ 220 async def process_rss(session: aiohttp.ClientSession, name: str, url: str, conn: sqlite3.Connection):
+ 221     """Prosesser én RSS-kilde"""
+ 222     logger.info(f"🔎 Sjekker: {name}")
+ 223     try:
+ 224         async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
+ 225             if r.status >= 400:
+ 226                 logger.warning(f"  ⚠️ HTTP {r.status} for {name}")
+ 227                 return
+ 228             content = await r.text()
+ 229         
+ 230         feed = feedparser.parse(content)
+ 231         hits = 0
+ 232         
+ 233         for entry in feed.entries:
+ 234             title = getattr(entry, 'title', '')
+ 235             link = getattr(entry, 'link', '')
+ 236             summary = getattr(entry, 'summary', getattr(entry, 'description', ''))
+ 237             
+ 238             item_id = hashlib.sha256(f"{name}|{link}|{title}".encode()).hexdigest()
+ 239             if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (item_id,)).fetchone():
+ 240                 continue
+ 241             
+ 242             full_text = f"{title} {summary}"
+ 243             
+ 244             # Hent PDF for høringer
+ 245             if link.lower().endswith(".pdf") or "høring" in title.lower():
+ 246                 pdf_text = await fetch_pdf_text(session, link)
+ 247                 if pdf_text:
+ 248                     full_text += " " + pdf_text
+ 249             
+ 250             result = analyze_content(full_text, name)
+ 251             
+ 252             if result["is_relevant"]:
+ 253                 conn.execute("""INSERT INTO weekly_hits 
+ 254                     (source, title, link, excerpt, priority, deadline, deadline_text, relevance_score, matched_keywords)
+ 255                     VALUES (?,?,?,?,?,?,?,?,?)""",
+ 256                     (name, title, link, summary[:500], result["priority"].value,
+ 257                      result["deadline"].isoformat() if result["deadline"] else None,
+ 258                      result["deadline_text"], result["score"], ",".join(result["matched"][:10])))
+ 259                 hits += 1
+ 260                 
+ 261                 pri_emoji = {Priority.CRITICAL: "🚨", Priority.HIGH: "⚠️", Priority.MEDIUM: "📋", Priority.LOW: "📌"}
+ 262                 logger.info(f"  {pri_emoji[result['priority']]} {title[:50]}... (score={result['score']:.1f})")
+ 263             
+ 264             conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
+ 265                         (item_id, name, title, datetime.now().isoformat()))
+ 266         
+ 267         conn.commit()
+ 268         logger.info(f"  ✓ {hits} relevante fra {name}")
+ 269         
+ 270     except Exception as e:
+ 271         logger.error(f"RSS-feil {name}: {e}")
+ 272 
+ 273 # --- 5. RAPPORT ---
+ 274 def generate_html_report(rows: list) -> str:
+ 275     """Generer HTML-rapport"""
+ 276     colors = {1: "#dc3545", 2: "#fd7e14", 3: "#ffc107", 4: "#28a745"}
+ 277     labels = {1: "🚨 KRITISK", 2: "⚠️ HØY", 3: "📋 MEDIUM", 4: "📌 LAV"}
+ 278     now = datetime.now().strftime('%Y-%m-%d')
+ 279     
+ 280     html = f"""<!DOCTYPE html>
+ 281 <html><head><meta charset="utf-8"><style>
+ 282 body {{ font-family: -apple-system, sans-serif; max-width: 700px; margin: 20px auto; background: #f5f5f5; }}
+ 283 .header {{ background: linear-gradient(135deg, #1a5f7a, #086972); color: white; padding: 25px; border-radius: 10px; }}
+ 284 .item {{ background: white; border-radius: 8px; margin: 15px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); overflow: hidden; }}
+ 285 .item-head {{ padding: 15px; border-left: 5px solid; }}
+ 286 .item-body {{ padding: 15px; border-top: 1px solid #eee; font-size: 14px; color: #444; }}
+ 287 .deadline {{ background: #dc3545; color: white; padding: 3px 8px; border-radius: 3px; font-size: 12px; }}
+ 288 .keywords {{ margin-top: 10px; }}
+ 289 .kw {{ display: inline-block; background: #e9ecef; padding: 2px 6px; border-radius: 3px; font-size: 11px; margin: 2px; }}
+ 290 a {{ color: #1a5f7a; }}
+ 291 </style></head><body>
+ 292 <div class="header">
+ 293 <h2 style="margin:0;">🛡️ LovSonar Ukesrapport</h2>
+ 294 <p style="margin:10px 0 0;">Regulatorisk radar for byggevarehandel</p>
+ 295 <p><strong>{len(rows)}</strong> treff | <strong>{sum(1 for r in rows if r[5]==1)}</strong> kritiske | {now}</p>
+ 296 </div>
+ 297 """
+ 298     
+ 299     for row in rows:
+ 300         # row: id, source, title, link, excerpt, priority, deadline, deadline_text, score, keywords, detected_at
+ 301         source, title, link, excerpt = row[1], row[2], row[3], row[4]
+ 302         priority, deadline_text, score = row[5], row[7] or "", row[8]
+ 303         keywords = (row[9] or "").split(",")[:6]
+ 304         
+ 305         color = colors.get(priority, "#28a745")
+ 306         label = labels.get(priority, "📌 LAV")
+ 307         
+ 308         deadline_html = f'<span class="deadline">⏰ {deadline_text}</span>' if deadline_text else ""
+ 309         kw_html = "".join(f'<span class="kw">{k}</span>' for k in keywords if k)
+ 310         
+ 311         html += f"""
+ 312 <div class="item">
+ 313 <div class="item-head" style="border-color: {color};">
+ 314 <strong>{source}</strong> | {label} | Score: {score:.1f} {deadline_html}<br>
+ 315 <a href="{link}" target="_blank"><strong>{title}</strong></a>
+ 316 </div>
+ 317 <div class="item-body">
+ 318 {excerpt[:400]}{'...' if len(excerpt)>400 else ''}
+ 319 <div class="keywords">{kw_html}</div>
+ 320 </div>
+ 321 </div>"""
+ 322     
+ 323     html += f"<p style='text-align:center; color:#666; font-size:12px;'>Generert av LovSonar v7.1 • {now}</p></body></html>"
+ 324     return html
+ 325 
+ 326 def send_weekly_report():
+ 327     """Send ukesrapport via e-post"""
+ 328     email_user = os.environ.get("EMAIL_USER", "").strip()
+ 329     email_pass = os.environ.get("EMAIL_PASS", "").strip()
+ 330     email_to = os.environ.get("EMAIL_RECIPIENT", email_user).strip()
+ 331     
+ 332     if not all([email_user, email_pass, email_to]):
+ 333         logger.warning("⚠️ E-postvariabler mangler (EMAIL_USER, EMAIL_PASS, EMAIL_RECIPIENT)")
+ 334         return
+ 335     
+ 336     conn = sqlite3.connect(DB_PATH)
+ 337     rows = conn.execute("""
+ 338         SELECT * FROM weekly_hits 
+ 339         WHERE detected_at > datetime('now', '-7 days') 
+ 340         ORDER BY priority ASC, relevance_score DESC
+ 341     """).fetchall()
+ 342     conn.close()
+ 343     
+ 344     if not rows:
+ 345         logger.info("ℹ️ Ingen treff å rapportere denne uken")
+ 346         return
+ 347     
+ 348     msg = MIMEMultipart("alternative")
+ 349     msg["Subject"] = f"🛡️ LovSonar: {len(rows)} treff (uke {datetime.now().isocalendar()[1]})"
+ 350     msg["From"] = email_user
+ 351     msg["To"] = email_to
+ 352     
+ 353     text_fallback = f"LovSonar Ukesrapport\n\n{len(rows)} relevante treff. Se HTML-versjonen for detaljer."
+ 354     msg.attach(MIMEText(text_fallback, "plain", "utf-8"))
+ 355     msg.attach(MIMEText(generate_html_report(rows), "html", "utf-8"))
+ 356     
+ 357     try:
+ 358         with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
+ 359             server.login(email_user, email_pass)
+ 360             server.send_message(msg)
+ 361         logger.info(f"📧 Rapport sendt til {email_to}")
+ 362     except Exception as e:
+ 363         logger.error(f"E-postfeil: {e}")
+ 364 
+ 365 def print_stats():
+ 366     """Vis statistikk"""
+ 367     conn = sqlite3.connect(DB_PATH)
+ 368     rows = conn.execute("""
+ 369         SELECT source, priority, COUNT(*) 
+ 370         FROM weekly_hits 
+ 371         WHERE detected_at > datetime('now', '-7 days')
+ 372         GROUP BY source, priority
+ 373     """).fetchall()
+ 374     conn.close()
+ 375     
+ 376     if rows:
+ 377         logger.info("📊 Ukens statistikk:")
+ 378         for source, priority, count in rows:
+ 379             pri_name = {1:"KRITISK", 2:"HØY", 3:"MEDIUM", 4:"LAV"}.get(priority, "?")
+ 380             logger.info(f"  {source}: {count} {pri_name}")
+ 381 
+ 382 # --- 6. MAIN ---
+ 383 async def run_radar():
+ 384     """Kjør daglig sjekk"""
+ 385     conn = setup_db()
+ 386     
+ 387     async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
+ 388         tasks = [process_rss(session, name, url, conn) for name, url in RSS_SOURCES.items()]
+ 389         tasks.append(check_stortinget(session, conn))
+ 390         await asyncio.gather(*tasks, return_exceptions=True)
+ 391     
+ 392     conn.close()
+ 393 
+ 394 def main():
+ 395     logger.info("🚀 LovSonar v7.1 starter...")
+ 396     mode = os.environ.get("LOVSONAR_MODE", "daily").lower()
+ 397     
+ 398     if mode == "weekly":
+ 399         logger.info("📅 Kjører i UKESRAPPORT-modus")
+ 400         print_stats()
+ 401         send_weekly_report()
+ 402     else:
+ 403         logger.info("📅 Kjører i DAGLIG-modus")
+ 404         asyncio.run(run_radar())
+ 405         print_stats()
+ 406     
+ 407     logger.info("✅ Ferdig!")
+ 408 
+ 409 if __name__ == "__main__":
+ 410     main()
