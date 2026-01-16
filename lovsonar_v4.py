+   1 #!/usr/bin/env python3
+   2 # -*- coding: utf-8 -*-
+   3 """
+   4 LovSonar v4.0 - Strategisk Fremtidsovervaking for Varehandel
+   5 =============================================================
+   6 Optimalisert for AI-analyse (GPT/ChatGPT).
+   7 
+   8 Endringer fra v3.1:
+   9 - Optimalisert rapportformat for LLM-analyse
+  10 - Flere datakilder (Regjeringen.no horinger)
+  11 - Utvidet nokkelordliste (CSRD, Green Claims, CSDDD)
+  12 - Fjernet feedparser-avhengighet
+  13 - Forbedret prioritering og kategorisering
+  14 """
+  15 
+  16 import os
+  17 import json
+  18 import hashlib
+  19 import smtplib
+  20 import re
+  21 import logging
+  22 import sqlite3
+  23 import requests
+  24 from datetime import datetime, timedelta
+  25 from pathlib import Path
+  26 from typing import Dict, List, Any, Optional
+  27 from email.mime.text import MIMEText
+  28 from email.mime.multipart import MIMEMultipart
+  29 from xml.etree import ElementTree as ET
+  30 from bs4 import BeautifulSoup
+  31 from difflib import SequenceMatcher
+  32 from dataclasses import dataclass
+  33 from enum import Enum
+  34 
+  35 # =============================================================================
+  36 # KONFIGURASJON
+  37 # =============================================================================
+  38 
+  39 USER_AGENT = "LovSonar/4.0 (Strategisk Pilot for Varehandel)"
+  40 DB_PATH = os.getenv("LOVSONAR_DB", "lovsonar_v4.db")
+  41 CACHE_FILE = os.getenv("LOVSONAR_CACHE", "lovsonar_cache.json")
+  42 MAX_AGE_DAYS = int(os.getenv("LOVSONAR_MAX_AGE_DAYS", "180"))
+  43 CHANGE_THRESHOLD = 0.5  # Minimum % endring for varsling
+  44 
+  45 logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
+  46 logger = logging.getLogger("LovSonar")
+  47 
+  48 # =============================================================================
+  49 # PRIORITET & NOKKELORD
+  50 # =============================================================================
+  51 
+  52 class Priority(Enum):
+  53     CRITICAL = 1  # Umiddelbar handling kreves
+  54     HIGH = 2      # Planlegg respons
+  55     MEDIUM = 3    # Folg med
+  56     LOW = 4       # Informasjon
+  57 
+  58 @dataclass
+  59 class Keyword:
+  60     term: str
+  61     weight: float
+  62     category: str
+  63     description: str = ""
+  64 
+  65 # Segmentnokkelord - identifiserer om saken gjelder varehandel/bygg
+  66 KEYWORDS_SEGMENT = [
+  67     Keyword("byggevare", 2.0, "core", "Kjernevirksomhet"),
+  68     Keyword("byggevarehus", 2.0, "core", "Kjernevirksomhet"),
+  69     Keyword("trelast", 1.5, "core", "Hovedsortiment"),
+  70     Keyword("jernvare", 1.5, "core", "Hovedsortiment"),
+  71     Keyword("detaljhandel", 1.0, "retail", "Bransje"),
+  72     Keyword("varehandel", 1.0, "retail", "Bransje"),
+  73     Keyword("dagligvare", 0.8, "retail", "Relatert"),
+  74     Keyword("forbruker", 0.8, "retail", "Malgruppe"),
+  75 ]
+  76 
+  77 # EU Green Deal og barekraft - UTVIDET
+  78 KEYWORDS_EU_SUSTAINABILITY = [
+  79     # EU Green Deal - Kjernereguleringer
+  80     Keyword("espr", 3.0, "eu_core", "Ecodesign for Sustainable Products Regulation"),
+  81     Keyword("ecodesign", 2.5, "eu_core", "Okodesign-krav"),
+  82     Keyword("digitalt produktpass", 3.0, "digital", "Digital Product Passport (DPP)"),
+  83     Keyword("dpp", 2.5, "digital", "Digital Product Passport"),
+  84     Keyword("produktpass", 2.5, "digital", "Produktdokumentasjon"),
+  85 
+  86     # NYE: Corporate Sustainability
+  87     Keyword("csrd", 3.0, "reporting", "Corporate Sustainability Reporting Directive"),
+  88     Keyword("csddd", 3.0, "due_diligence", "Corporate Sustainability Due Diligence Directive"),
+  89     Keyword("barekraftsrapportering", 2.5, "reporting", "Sustainability reporting"),
+  90     Keyword("esrs", 2.5, "reporting", "European Sustainability Reporting Standards"),
+  91 
+  92     # NYE: Green Claims
+  93     Keyword("green claims", 3.0, "marketing", "Green Claims Directive"),
+  94     Keyword("miljopastander", 2.5, "marketing", "Green claims regulering"),
+  95     Keyword("substansiering", 2.0, "marketing", "Dokumentasjonskrav miljopastand"),
+  96 
+  97     # Emballasje og avfall
+  98     Keyword("ppwr", 2.5, "packaging", "Packaging and Packaging Waste Regulation"),
+  99     Keyword("emballasje", 2.0, "packaging", "Emballasjekrav"),
+ 100     Keyword("engangsplast", 2.0, "packaging", "SUP-direktiv"),
+ 101     Keyword("produsentansvar", 2.0, "packaging", "EPR - Extended Producer Responsibility"),
+ 102     Keyword("pantesystem", 2.0, "packaging", "Deposit return system"),
+ 103 
+ 104     # Skog og avskoging
+ 105     Keyword("eudr", 2.5, "deforestation", "EU Deforestation Regulation"),
+ 106     Keyword("avskoging", 2.0, "deforestation", "Avskogingsfri"),
+ 107     Keyword("sporbarhet", 2.0, "deforestation", "Verdikjede-sporbarhet"),
+ 108     Keyword("geolokalisering", 2.0, "deforestation", "EUDR-krav"),
+ 109 
+ 110     # Miljodeklarasjoner
+ 111     Keyword("miljodeklarasjon", 2.0, "sustainability", "EPD"),
+ 112     Keyword("epd", 2.0, "sustainability", "Environmental Product Declaration"),
+ 113     Keyword("klimaavtrykk", 1.5, "sustainability", "Carbon footprint"),
+ 114     Keyword("livslopanalyse", 1.5, "sustainability", "LCA"),
+ 115     Keyword("barekraft", 1.5, "sustainability", "Generell barekraft"),
+ 116     Keyword("sirkular", 1.5, "sustainability", "Sirkularokonomi"),
+ 117     Keyword("ombruk", 1.5, "sustainability", "Gjenbruk"),
+ 118 
+ 119     # Kjemikalier
+ 120     Keyword("reach", 2.0, "chemicals", "REACH-forordningen"),
+ 121     Keyword("pfas", 2.5, "chemicals", "PFAS-forbud"),
+ 122     Keyword("farlige stoffer", 2.0, "chemicals", "Kjemikalieregulering"),
+ 123     Keyword("biocid", 1.5, "chemicals", "Biocidforordningen"),
+ 124     Keyword("mikroplast", 2.0, "chemicals", "Mikroplast-forbud"),
+ 125 ]
+ 126 
+ 127 # Norske reguleringer og compliance
+ 128 KEYWORDS_NORWEGIAN = [
+ 129     Keyword("apenhetsloven", 2.5, "compliance", "Aktsomhetsvurderinger"),
+ 130     Keyword("aktsomhet", 2.0, "compliance", "Due diligence"),
+ 131     Keyword("menneskerettigheter", 1.5, "compliance", "Menneskerettigheter i verdikjeden"),
+ 132     Keyword("gronnvasking", 2.5, "marketing", "Villedende miljopaastander"),
+ 133     Keyword("miljopastand", 2.0, "marketing", "Green claims"),
+ 134     Keyword("markedsforingsloven", 1.5, "marketing", "Markedsforingsregler"),
+ 135     Keyword("forbrukertilsynet", 1.5, "marketing", "Tilsyn"),
+ 136     Keyword("tek17", 2.0, "building", "Byggeforskrift"),
+ 137     Keyword("byggteknisk", 1.5, "building", "Byggeregelverk"),
+ 138     Keyword("dok-forskriften", 2.0, "building", "Dokumentasjon av byggevarer"),
+ 139     Keyword("eos-avtalen", 2.0, "eu", "EOS-relevans"),
+ 140     Keyword("eos-relevans", 2.0, "eu", "EU-direktiv i norsk rett"),
+ 141 ]
+ 142 
+ 143 # Kritiske hendelser (frister, ikrafttredelse)
+ 144 KEYWORDS_CRITICAL = [
+ 145     Keyword("horingsfrist", 3.0, "deadline", "Frist for innspill"),
+ 146     Keyword("horingsnotat", 2.0, "deadline", "Horingsdokument"),
+ 147     Keyword("ikrafttredelse", 2.5, "deadline", "Lov trer i kraft"),
+ 148     Keyword("trer i kraft", 2.5, "deadline", "Tidspunkt for virkning"),
+ 149     Keyword("forbud", 2.5, "legal", "Forbud mot stoff/praksis"),
+ 150     Keyword("pabud", 2.0, "legal", "Nytt krav"),
+ 151     Keyword("overtredelsesgebyr", 2.0, "legal", "Sanksjoner"),
+ 152     Keyword("implementering", 2.0, "deadline", "Implementeringsfrist"),
+ 153 ]
+ 154 
+ 155 ALL_KEYWORDS = KEYWORDS_SEGMENT + KEYWORDS_EU_SUSTAINABILITY + KEYWORDS_NORWEGIAN + KEYWORDS_CRITICAL
+ 156 
+ 157 # =============================================================================
+ 158 # DATAKILDER
+ 159 # =============================================================================
+ 160 
+ 161 # RSS-feeds som fungerer (verifisert)
+ 162 RSS_SOURCES = {
+ 163     "Forbrukertilsynet": {
+ 164         "url": "https://www.forbrukertilsynet.no/feed",
+ 165         "type": "consumer",
+ 166         "emoji": "scales",
+ 167         "description": "Gronnvasking, markedsforing, forbrukerrettigheter"
+ 168     },
+ 169 }
+ 170 
+ 171 # Stortinget API - strukturerte data
+ 172 STORTINGET_API = {
+ 173     "saker": "https://data.stortinget.no/eksport/saker?sesjonid={sesjon}",
+ 174     "horinger": "https://data.stortinget.no/eksport/horinger?sesjonid={sesjon}",
+ 175 }
+ 176 
+ 177 # Regjeringen.no horingssider (HTML-scraping)
+ 178 REGJERINGEN_HORINGER = {
+ 179     "Klima og miljo": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?ownerid=668",
+ 180     "Naering og handel": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?ownerid=709",
+ 181     "Forbrukersaker": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?ownerid=298",
+ 182 }
+ 183 
+ 184 # Lover og forskrifter a overvake (HTML-scraping med endringssporing)
+ 185 LAWS_TO_MONITOR = {
+ 186     "Apenhetsloven": "https://lovdata.no/dokument/NL/lov/2021-06-18-99",
+ 187     "Produktkontrolloven": "https://lovdata.no/dokument/NL/lov/1976-06-11-79",
+ 188     "Markedsforingsloven": "https://lovdata.no/dokument/NL/lov/2009-01-09-2",
+ 189     "Forbrukerkjopsloven": "https://lovdata.no/dokument/NL/lov/2002-06-21-34",
+ 190 }
+ 191 
+ 192 REGULATIONS_TO_MONITOR = {
+ 193     "Byggevareforskriften (DOK)": "https://lovdata.no/dokument/SF/forskrift/2013-12-17-1579",
+ 194     "TEK17": "https://lovdata.no/dokument/SF/forskrift/2017-06-19-840",
+ 195     "Avfallsforskriften": "https://lovdata.no/dokument/SF/forskrift/2004-06-01-930",
+ 196     "Produktforskriften": "https://lovdata.no/dokument/SF/forskrift/2004-06-01-922",
+ 197 }
+ 198 
+ 199 # Relevante stortingskomiteer
+ 200 RELEVANT_COMMITTEES = [
+ 201     "naringskomiteen",
+ 202     "energi- og miljokomiteen",
+ 203     "finanskomiteen",
+ 204     "familie- og kulturkomiteen",
+ 205 ]
+ 206 
+ 207 # =============================================================================
+ 208 # DATABASE
+ 209 # =============================================================================
+ 210 
+ 211 def setup_db() -> sqlite3.Connection:
+ 212     """Opprett database for historikk og compliance-dokumentasjon."""
+ 213     conn = sqlite3.connect(DB_PATH)
+ 214 
+ 215     conn.execute("""
+ 216         CREATE TABLE IF NOT EXISTS seen_items (
+ 217             item_id TEXT PRIMARY KEY,
+ 218             source TEXT,
+ 219             title TEXT,
+ 220             date_seen TEXT
+ 221         )
+ 222     """)
+ 223 
+ 224     conn.execute("""
+ 225         CREATE TABLE IF NOT EXISTS sonar_hits (
+ 226             id INTEGER PRIMARY KEY AUTOINCREMENT,
+ 227             source TEXT,
+ 228             title TEXT,
+ 229             link TEXT,
+ 230             priority INTEGER,
+ 231             score REAL,
+ 232             deadline TEXT,
+ 233             matched_keywords TEXT,
+ 234             category TEXT,
+ 235             detected_at TEXT DEFAULT CURRENT_TIMESTAMP
+ 236         )
+ 237     """)
+ 238 
+ 239     conn.execute("""
+ 240         CREATE TABLE IF NOT EXISTS radar_hits (
+ 241             id INTEGER PRIMARY KEY AUTOINCREMENT,
+ 242             law_name TEXT,
+ 243             url TEXT,
+ 244             change_percent REAL,
+ 245             detected_at TEXT DEFAULT CURRENT_TIMESTAMP
+ 246         )
+ 247     """)
+ 248 
+ 249     conn.commit()
+ 250     return conn
+ 251 
+ 252 # =============================================================================
+ 253 # ANALYSE
+ 254 # =============================================================================
+ 255 
+ 256 def extract_deadline(text: str) -> Optional[str]:
+ 257     """Ekstraher horingsfrist fra tekst."""
+ 258     patterns = [
+ 259         r'(?:horingsfrist|frist)[:\s]+(\d{1,2})[.\s]+([a-zA-ZeaoAO]+)\s+(\d{4})',
+ 260         r'(?:frist|deadline)[:\s]+(\d{1,2})\.(\d{1,2})\.(\d{4})',
+ 261         r'innen\s+(\d{1,2})[.\s]+([a-zA-ZeaoAO]+)\s+(\d{4})',
+ 262     ]
+ 263 
+ 264     for pattern in patterns:
+ 265         match = re.search(pattern, text, re.IGNORECASE)
+ 266         if match:
+ 267             return match.group(0)
+ 268     return None
+ 269 
+ 270 def analyze_relevance(text: str, source_type: str = "") -> Dict[str, Any]:
+ 271     """Analyser tekst for relevans til varehandel/barekraft."""
+ 272     t = text.lower()
+ 273 
+ 274     # Finn matchende nokkelord
+ 275     segment_matches = [(kw.term, kw.weight, kw.category) for kw in KEYWORDS_SEGMENT if kw.term.lower() in t]
+ 276     topic_matches = [(kw.term, kw.weight, kw.category) for kw in KEYWORDS_EU_SUSTAINABILITY if kw.term.lower() in t]
+ 277     norwegian_matches = [(kw.term, kw.weight, kw.category) for kw in KEYWORDS_NORWEGIAN if kw.term.lower() in t]
+ 278     critical_matches = [(kw.term, kw.weight, kw.category) for kw in KEYWORDS_CRITICAL if kw.term.lower() in t]
+ 279 
+ 280     all_matches = segment_matches + topic_matches + norwegian_matches + critical_matches
+ 281 
+ 282     # Beregn score
+ 283     segment_score = sum(w for _, w, _ in segment_matches) * 1.5
+ 284     topic_score = sum(w for _, w, _ in topic_matches)
+ 285     norwegian_score = sum(w for _, w, _ in norwegian_matches)
+ 286     critical_score = sum(w for _, w, _ in critical_matches)
+ 287 
+ 288     total_score = segment_score + topic_score + norwegian_score + critical_score
+ 289 
+ 290     # Bestem relevans
+ 291     has_high_priority_topic = any(w >= 2.5 for _, w, _ in topic_matches + norwegian_matches)
+ 292     is_relevant = total_score >= 3.0 or has_high_priority_topic
+ 293 
+ 294     # Bestem prioritet
+ 295     priority = Priority.LOW
+ 296     if is_relevant:
+ 297         deadline = extract_deadline(text)
+ 298         has_deadline = deadline is not None or critical_score > 0
+ 299 
+ 300         if has_deadline and total_score > 8:
+ 301             priority = Priority.CRITICAL
+ 302         elif total_score > 6 or has_deadline:
+ 303             priority = Priority.HIGH
+ 304         elif total_score > 4:
+ 305             priority = Priority.MEDIUM
+ 306         else:
+ 307             priority = Priority.LOW
+ 308 
+ 309     # Kategoriser hovedtema
+ 310     categories = set(cat for _, _, cat in all_matches)
+ 311     main_category = "general"
+ 312     if "eu_core" in categories or "digital" in categories:
+ 313         main_category = "EU Green Deal"
+ 314     elif "reporting" in categories or "due_diligence" in categories:
+ 315         main_category = "Corporate Sustainability"
+ 316     elif "packaging" in categories or "deforestation" in categories:
+ 317         main_category = "EU Verdikjede"
+ 318     elif "chemicals" in categories or "sustainability" in categories:
+ 319         main_category = "Miljo/Kjemikalier"
+ 320     elif "compliance" in categories or "marketing" in categories:
+ 321         main_category = "Compliance/Markedsforing"
+ 322     elif "building" in categories:
+ 323         main_category = "Byggeregelverk"
+ 324 
+ 325     return {
+ 326         "is_relevant": is_relevant,
+ 327         "score": round(total_score, 1),
+ 328         "priority": priority,
+ 329         "matched_keywords": [term for term, _, _ in all_matches],
+ 330         "categories": list(categories),
+ 331         "main_category": main_category,
+ 332         "deadline": extract_deadline(text),
+ 333     }
+ 334 
+ 335 # =============================================================================
+ 336 # RSS PARSING (uten feedparser)
+ 337 # =============================================================================
+ 338 
+ 339 def parse_rss(xml_content: bytes) -> List[Dict[str, str]]:
+ 340     """Parse RSS feed manuelt uten feedparser."""
+ 341     entries = []
+ 342     try:
+ 343         root = ET.fromstring(xml_content)
+ 344         # Standard RSS 2.0
+ 345         for item in root.findall(".//item"):
+ 346             entry = {
+ 347                 "title": item.findtext("title", ""),
+ 348                 "link": item.findtext("link", ""),
+ 349                 "summary": item.findtext("description", ""),
+ 350             }
+ 351             entries.append(entry)
+ 352         # Atom format
+ 353         if not entries:
+ 354             ns = {"atom": "http://www.w3.org/2005/Atom"}
+ 355             for item in root.findall(".//atom:entry", ns):
+ 356                 link_elem = item.find("atom:link", ns)
+ 357                 entry = {
+ 358                     "title": item.findtext("atom:title", "", ns),
+ 359                     "link": link_elem.get("href", "") if link_elem is not None else "",
+ 360                     "summary": item.findtext("atom:summary", "", ns),
+ 361                 }
+ 362                 entries.append(entry)
+ 363     except Exception as e:
+ 364         logger.error(f"RSS parse error: {e}")
+ 365     return entries
+ 366 
+ 367 # =============================================================================
+ 368 # STORTINGET API
+ 369 # =============================================================================
+ 370 
+ 371 def get_current_session() -> str:
+ 372     """Hent gjeldende stortingssesjon."""
+ 373     now = datetime.now()
+ 374     year = now.year
+ 375     if now.month >= 10:
+ 376         return f"{year}-{year + 1}"
+ 377     return f"{year - 1}-{year}"
+ 378 
+ 379 def fetch_stortinget_data(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
+ 380     """Hent saker fra Stortingets API."""
+ 381     logger.info("Henter data fra Stortinget...")
+ 382     hits = []
+ 383     session = get_current_session()
+ 384 
+ 385     for endpoint_name, url_template in STORTINGET_API.items():
+ 386         url = url_template.format(sesjon=session)
+ 387 
+ 388         try:
+ 389             response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
+ 390             response.raise_for_status()
+ 391 
+ 392             root = ET.fromstring(response.content)
+ 393             ns = {"s": "http://data.stortinget.no"}
+ 394 
+ 395             items = root.findall(".//s:sak", ns) or root.findall(".//s:horing", ns)
+ 396 
+ 397             for item in items:
+ 398                 title = item.findtext("s:tittel", "", ns) or item.findtext("s:kort_tittel", "", ns)
+ 399                 item_id = item.findtext("s:id", "", ns)
+ 400                 komite = item.findtext("s:komite/s:navn", "", ns) or ""
+ 401 
+ 402                 hash_id = hashlib.sha256(f"{item_id}{title}".encode()).hexdigest()[:16]
+ 403 
+ 404                 if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (hash_id,)).fetchone():
+ 405                     continue
+ 406 
+ 407                 full_text = f"{title} {komite}"
+ 408                 analysis = analyze_relevance(full_text, "stortinget")
+ 409 
+ 410                 conn.execute(
+ 411                     "INSERT INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
+ 412                     (hash_id, "Stortinget", title, datetime.now().isoformat())
+ 413                 )
+ 414 
+ 415                 if analysis["is_relevant"]:
+ 416                     link = f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={item_id}"
+ 417 
+ 418                     hit = {
+ 419                         "source": "Stortinget",
+ 420                         "title": title,
+ 421                         "link": link,
+ 422                         "priority": analysis["priority"],
+ 423                         "score": analysis["score"],
+ 424                         "matched_keywords": analysis["matched_keywords"],
+ 425                         "category": analysis["main_category"],
+ 426                         "deadline": analysis["deadline"],
+ 427                         "committee": komite,
+ 428                     }
+ 429                     hits.append(hit)
+ 430 
+ 431                     conn.execute(
+ 432                         """INSERT INTO sonar_hits
+ 433                            (source, title, link, priority, score, deadline, matched_keywords, category)
+ 434                            VALUES (?,?,?,?,?,?,?,?)""",
+ 435                         ("Stortinget", title, link, analysis["priority"].value,
+ 436                          analysis["score"], analysis["deadline"],
+ 437                          ",".join(analysis["matched_keywords"]), analysis["main_category"])
+ 438                     )
+ 439 
+ 440                     logger.info(f"  [{analysis['priority'].name}] {title[:60]}...")
+ 441 
+ 442             logger.info(f"  Prosessert {len(items)} elementer fra {endpoint_name}")
+ 443 
+ 444         except Exception as e:
+ 445             logger.error(f"Feil ved henting fra Stortinget ({endpoint_name}): {e}")
+ 446 
+ 447     return hits
+ 448 
+ 449 # =============================================================================
+ 450 # RSS-FEEDS
+ 451 # =============================================================================
+ 452 
+ 453 def fetch_rss_feeds(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
+ 454     """Hent og analyser RSS-feeds."""
+ 455     logger.info("Sjekker RSS-feeds...")
+ 456     hits = []
+ 457 
+ 458     for name, config in RSS_SOURCES.items():
+ 459         try:
+ 460             response = requests.get(
+ 461                 config["url"],
+ 462                 headers={"User-Agent": USER_AGENT},
+ 463                 timeout=30
+ 464             )
+ 465             response.raise_for_status()
+ 466 
+ 467             entries = parse_rss(response.content)
+ 468 
+ 469             for entry in entries[:15]:
+ 470                 link = entry.get('link', '')
+ 471                 title = entry.get('title', '')
+ 472                 summary = entry.get('summary', '')
+ 473 
+ 474                 hash_id = hashlib.sha256(link.encode()).hexdigest()[:16]
+ 475 
+ 476                 if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (hash_id,)).fetchone():
+ 477                     continue
+ 478 
+ 479                 full_text = f"{title} {summary}"
+ 480                 analysis = analyze_relevance(full_text, config["type"])
+ 481 
+ 482                 conn.execute(
+ 483                     "INSERT INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
+ 484                     (hash_id, name, title, datetime.now().isoformat())
+ 485                 )
+ 486 
+ 487                 if analysis["is_relevant"]:
+ 488                     hit = {
+ 489                         "source": name,
+ 490                         "title": title,
+ 491                         "link": link,
+ 492                         "priority": analysis["priority"],
+ 493                         "score": analysis["score"],
+ 494                         "matched_keywords": analysis["matched_keywords"],
+ 495                         "category": analysis["main_category"],
+ 496                         "deadline": analysis["deadline"],
+ 497                     }
+ 498                     hits.append(hit)
+ 499 
+ 500                     conn.execute(
+ 501                         """INSERT INTO sonar_hits
+ 502                            (source, title, link, priority, score, deadline, matched_keywords, category)
+ 503                            VALUES (?,?,?,?,?,?,?,?)""",
+ 504                         (name, title, link, analysis["priority"].value,
+ 505                          analysis["score"], analysis["deadline"],
+ 506                          ",".join(analysis["matched_keywords"]), analysis["main_category"])
+ 507                     )
+ 508 
+ 509                     logger.info(f"  [{analysis['priority'].name}] {title[:60]}...")
+ 510 
+ 511             logger.info(f"  {name}: {len(entries)} innlegg sjekket")
+ 512 
+ 513         except Exception as e:
+ 514             logger.error(f"  {name}: {e}")
+ 515 
+ 516     return hits
+ 517 
+ 518 # =============================================================================
+ 519 # REGJERINGEN.NO HORINGER
+ 520 # =============================================================================
+ 521 
+ 522 def fetch_regjeringen_horinger(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
+ 523     """Hent horinger fra regjeringen.no (HTML-scraping)."""
+ 524     logger.info("Sjekker Regjeringen.no horinger...")
+ 525     hits = []
+ 526 
+ 527     for name, url in REGJERINGEN_HORINGER.items():
+ 528         try:
+ 529             response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
+ 530             response.raise_for_status()
+ 531 
+ 532             soup = BeautifulSoup(response.content, "html.parser")
+ 533 
+ 534             # Finn horingslenker
+ 535             for link_elem in soup.select("a[href*='/hoeringer/']")[:10]:
+ 536                 title = link_elem.get_text(strip=True)
+ 537                 link = link_elem.get("href", "")
+ 538 
+ 539                 if not link.startswith("http"):
+ 540                     link = "https://www.regjeringen.no" + link
+ 541 
+ 542                 hash_id = hashlib.sha256(link.encode()).hexdigest()[:16]
+ 543 
+ 544                 if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (hash_id,)).fetchone():
+ 545                     continue
+ 546 
+ 547                 analysis = analyze_relevance(title, "regjeringen")
+ 548 
+ 549                 conn.execute(
+ 550                     "INSERT INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
+ 551                     (hash_id, f"Regjeringen ({name})", title, datetime.now().isoformat())
+ 552                 )
+ 553 
+ 554                 if analysis["is_relevant"]:
+ 555                     hit = {
+ 556                         "source": f"Regjeringen ({name})",
+ 557                         "title": title,
+ 558                         "link": link,
+ 559                         "priority": analysis["priority"],
+ 560                         "score": analysis["score"],
+ 561                         "matched_keywords": analysis["matched_keywords"],
+ 562                         "category": analysis["main_category"],
+ 563                         "deadline": analysis["deadline"],
+ 564                     }
+ 565                     hits.append(hit)
+ 566 
+ 567                     logger.info(f"  [{analysis['priority'].name}] {title[:60]}...")
+ 568 
+ 569             logger.info(f"  Regjeringen ({name}): sjekket")
+ 570 
+ 571         except Exception as e:
+ 572             logger.error(f"  Regjeringen ({name}): {e}")
+ 573 
+ 574     return hits
+ 575 
+ 576 # =============================================================================
+ 577 # LOVENDRING-OVERVAKING (RADAR)
+ 578 # =============================================================================
+ 579 
+ 580 def check_law_changes(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
+ 581     """Sjekk lover og forskrifter for endringer."""
+ 582     logger.info("Sjekker lover og forskrifter for endringer...")
+ 583     hits = []
+ 584 
+ 585     cache = {}
+ 586     if Path(CACHE_FILE).exists():
+ 587         with open(CACHE_FILE, 'r', encoding='utf-8') as f:
+ 588             cache = json.load(f)
+ 589 
+ 590     all_documents = {**LAWS_TO_MONITOR, **REGULATIONS_TO_MONITOR}
+ 591 
+ 592     for name, url in all_documents.items():
+ 593         try:
+ 594             response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
+ 595             response.raise_for_status()
+ 596 
+ 597             soup = BeautifulSoup(response.content, "html.parser")
+ 598 
+ 599             for element in soup(["script", "style", "nav", "footer"]):
+ 600                 element.decompose()
+ 601 
+ 602             text = re.sub(r'\s+', ' ', soup.get_text()).strip()
+ 603             new_hash = hashlib.sha256(text.encode()).hexdigest()
+ 604 
+ 605             prev = cache.get(name, {})
+ 606 
+ 607             if prev and new_hash != prev.get("hash"):
+ 608                 prev_text = prev.get("text", "")[:5000]
+ 609                 curr_text = text[:5000]
+ 610                 similarity = SequenceMatcher(None, prev_text, curr_text).ratio()
+ 611                 change_percent = round((1 - similarity) * 100, 2)
+ 612 
+ 613                 if change_percent >= CHANGE_THRESHOLD:
+ 614                     hit = {
+ 615                         "name": name,
+ 616                         "url": url,
+ 617                         "change_percent": change_percent,
+ 618                     }
+ 619                     hits.append(hit)
+ 620 
+ 621                     conn.execute(
+ 622                         "INSERT INTO radar_hits (law_name, url, change_percent) VALUES (?,?,?)",
+ 623                         (name, url, change_percent)
+ 624                     )
+ 625 
+ 626                     logger.info(f"  {name}: {change_percent}% endring detektert")
+ 627 
+ 628             cache[name] = {
+ 629                 "hash": new_hash,
+ 630                 "text": text[:5000],
+ 631                 "checked": datetime.now().isoformat()
+ 632             }
+ 633 
+ 634         except Exception as e:
+ 635             logger.error(f"  {name}: {e}")
+ 636 
+ 637     with open(CACHE_FILE, 'w', encoding='utf-8') as f:
+ 638         json.dump(cache, f, ensure_ascii=False, indent=2)
+ 639 
+ 640     logger.info(f"  Sjekket {len(all_documents)} dokumenter")
+ 641     return hits
+ 642 
+ 643 # =============================================================================
+ 644 # RAPPORTERING - OPTIMALISERT FOR GPT/AI-ANALYSE
+ 645 # =============================================================================
+ 646 
+ 647 def generate_report(sonar_hits: List[Dict], radar_hits: List[Dict]) -> str:
+ 648     """Generer strukturert rapport for AI-analyse."""
+ 649     lines = []
+ 650 
+ 651     # Header med kontekst for AI
+ 652     lines.append("=" * 70)
+ 653     lines.append("LOVSONAR v4.0 - STRATEGISK RAPPORT FOR VAREHANDEL")
+ 654     lines.append("=" * 70)
+ 655     lines.append("")
+ 656     lines.append(f"Rapport generert: {datetime.now().strftime('%d.%m.%Y kl. %H:%M')}")
+ 657     lines.append(f"Stortingssesjon: {get_current_session()}")
+ 658     lines.append("")
+ 659     lines.append("INSTRUKS TIL AI-ANALYSE:")
+ 660     lines.append("- Prioriter signaler med hoy score og nare frister")
+ 661     lines.append("- Vurder konsekvenser for varehandel og byggevare")
+ 662     lines.append("- Identifiser muligheter for proaktiv tilpasning")
+ 663     lines.append("- Flagg eventuelle compliance-risikoer")
+ 664     lines.append("")
+ 665     lines.append("-" * 70)
+ 666 
+ 667     # Statistikk
+ 668     lines.append("")
+ 669     lines.append("OPPSUMMERING:")
+ 670     lines.append(f"- Nye signaler funnet: {len(sonar_hits)}")
+ 671     lines.append(f"- Lovendringer detektert: {len(radar_hits)}")
+ 672 
+ 673     critical_count = len([h for h in sonar_hits if h["priority"] == Priority.CRITICAL])
+ 674     high_count = len([h for h in sonar_hits if h["priority"] == Priority.HIGH])
+ 675     if critical_count > 0:
+ 676         lines.append(f"- KRITISKE signaler: {critical_count}")
+ 677     if high_count > 0:
+ 678         lines.append(f"- HOY PRIORITET signaler: {high_count}")
+ 679 
+ 680     lines.append("")
+ 681     lines.append("=" * 70)
+ 682 
+ 683     # Lovendringer forst (mest kritisk)
+ 684     if radar_hits:
+ 685         lines.append("")
+ 686         lines.append("LOVENDRINGER DETEKTERT")
+ 687         lines.append("-" * 70)
+ 688         lines.append("")
+ 689         for hit in radar_hits:
+ 690             lines.append(f"LOV/FORSKRIFT: {hit['name']}")
+ 691             lines.append(f"  Endring: {hit['change_percent']}%")
+ 692             lines.append(f"  Lenke: {hit['url']}")
+ 693             lines.append(f"  Handling: Sjekk hva som er endret")
+ 694             lines.append("")
+ 695 
+ 696     # Kritiske signaler
+ 697     critical = [h for h in sonar_hits if h["priority"] == Priority.CRITICAL]
+ 698     if critical:
+ 699         lines.append("")
+ 700         lines.append("KRITISKE SIGNALER - KREVER HANDLING")
+ 701         lines.append("-" * 70)
+ 702         lines.append("")
+ 703         for hit in sorted(critical, key=lambda x: x["score"], reverse=True):
+ 704             lines.append(f"TITTEL: {hit['title']}")
+ 705             lines.append(f"  Kilde: {hit['source']}")
+ 706             lines.append(f"  Kategori: {hit['category']}")
+ 707             lines.append(f"  Score: {hit['score']}")
+ 708             lines.append(f"  Nokkelord: {', '.join(hit['matched_keywords'])}")
+ 709             if hit.get("deadline"):
+ 710                 lines.append(f"  FRIST: {hit['deadline']}")
+ 711             if hit.get("committee"):
+ 712                 lines.append(f"  Komite: {hit['committee']}")
+ 713             lines.append(f"  Lenke: {hit['link']}")
+ 714             lines.append("")
+ 715 
+ 716     # Hoy prioritet
+ 717     high = [h for h in sonar_hits if h["priority"] == Priority.HIGH]
+ 718     if high:
+ 719         lines.append("")
+ 720         lines.append("HOY PRIORITET - PLANLEGG RESPONS")
+ 721         lines.append("-" * 70)
+ 722         lines.append("")
+ 723         for hit in sorted(high, key=lambda x: x["score"], reverse=True):
+ 724             lines.append(f"TITTEL: {hit['title']}")
+ 725             lines.append(f"  Kilde: {hit['source']} | Score: {hit['score']}")
+ 726             lines.append(f"  Kategori: {hit['category']}")
+ 727             lines.append(f"  Nokkelord: {', '.join(hit['matched_keywords'])}")
+ 728             lines.append(f"  Lenke: {hit['link']}")
+ 729             lines.append("")
+ 730 
+ 731     # Medium prioritet (kompakt)
+ 732     medium = [h for h in sonar_hits if h["priority"] == Priority.MEDIUM]
+ 733     if medium:
+ 734         lines.append("")
+ 735         lines.append("MEDIUM PRIORITET - FOLG MED")
+ 736         lines.append("-" * 70)
+ 737         lines.append("")
+ 738         for hit in sorted(medium, key=lambda x: x["score"], reverse=True)[:10]:
+ 739             lines.append(f"- {hit['title'][:65]}...")
+ 740             lines.append(f"  ({hit['source']}, Score: {hit['score']}, {hit['category']})")
+ 741         lines.append("")
+ 742 
+ 743     # Ingen funn
+ 744     if not sonar_hits and not radar_hits:
+ 745         lines.append("")
+ 746         lines.append("Ingen nye relevante signaler i denne skanningen.")
+ 747         lines.append("Neste skanning: neste mandag kl. 07:00 UTC.")
+ 748         lines.append("")
+ 749 
+ 750     # Footer
+ 751     lines.append("=" * 70)
+ 752     lines.append("SLUTT PA RAPPORT")
+ 753     lines.append("=" * 70)
+ 754 
+ 755     return "\n".join(lines)
+ 756 
+ 757 def send_email(report: str, sonar_count: int, radar_count: int) -> bool:
+ 758     """Send rapport via e-post."""
+ 759     user = os.environ.get("EMAIL_USER", "").strip()
+ 760     pw = os.environ.get("EMAIL_PASS", "").strip()
+ 761     recipient = os.environ.get("EMAIL_RECIPIENT", "").strip()
+ 762 
+ 763     if not all([user, pw, recipient]):
+ 764         logger.warning("E-post-konfigurasjon mangler. Hopper over sending.")
+ 765         return False
+ 766 
+ 767     if sonar_count == 0 and radar_count == 0:
+ 768         logger.info("Ingen funn - sender ikke e-post.")
+ 769         return False
+ 770 
+ 771     msg = MIMEMultipart("alternative")
+ 772     msg["Subject"] = f"LovSonar: {sonar_count} signaler, {radar_count} lovendringer"
+ 773     msg["From"] = user
+ 774     msg["To"] = recipient
+ 775 
+ 776     # Ren tekst for AI-analyse
+ 777     msg.attach(MIMEText(report, "plain", "utf-8"))
+ 778 
+ 779     try:
+ 780         with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
+ 781             server.login(user, pw)
+ 782             server.send_message(msg)
+ 783         logger.info(f"Rapport sendt til {recipient}")
+ 784         return True
+ 785     except Exception as e:
+ 786         logger.error(f"Feil ved sending av e-post: {e}")
+ 787         return False
+ 788 
+ 789 # =============================================================================
+ 790 # HOVEDPROGRAM
+ 791 # =============================================================================
+ 792 
+ 793 def main():
+ 794     """Hovedfunksjon."""
+ 795     logger.info("=" * 60)
+ 796     logger.info("LovSonar v4.0 - Strategisk Fremtidsovervaking")
+ 797     logger.info("=" * 60)
+ 798 
+ 799     conn = setup_db()
+ 800 
+ 801     try:
+ 802         # 1. Hent fra Stortinget API
+ 803         stortinget_hits = fetch_stortinget_data(conn)
+ 804 
+ 805         # 2. Hent fra RSS-feeds
+ 806         rss_hits = fetch_rss_feeds(conn)
+ 807 
+ 808         # 3. Hent fra Regjeringen.no horinger
+ 809         regjeringen_hits = fetch_regjeringen_horinger(conn)
+ 810 
+ 811         # 4. Sjekk lovendringer
+ 812         radar_hits = check_law_changes(conn)
+ 813 
+ 814         # Kombiner alle sonar-treff
+ 815         all_sonar_hits = stortinget_hits + rss_hits + regjeringen_hits
+ 816 
+ 817         # Generer og vis rapport
+ 818         report = generate_report(all_sonar_hits, radar_hits)
+ 819         print("\n" + report)
+ 820 
+ 821         # Send e-post
+ 822         send_email(report, len(all_sonar_hits), len(radar_hits))
+ 823 
+ 824         # Commit database
+ 825         conn.commit()
+ 826 
+ 827         # Rydd opp gamle entries
+ 828         cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)
+ 829         conn.execute("DELETE FROM seen_items WHERE date_seen < ?", (cutoff.isoformat(),))
+ 830         conn.commit()
+ 831 
+ 832         logger.info("=" * 60)
+ 833         logger.info(f"Ferdig. {len(all_sonar_hits)} nye signaler, {len(radar_hits)} lovendringer.")
+ 834         logger.info("=" * 60)
+ 835 
+ 836     finally:
+ 837         conn.close()
+ 838 
+ 839 if __name__ == "__main__":
+ 840     main()
