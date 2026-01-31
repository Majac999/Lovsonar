+    1 #!/usr/bin/env python3
+    2 # -*- coding: utf-8 -*-
+    3 """
+    4 LovSonar v6.1 - Komplett Regulatorisk Overvaking for Byggevarehandel
+    5 =====================================================================
+    6 Kombinerer v6.0-arkitektur med v5.0-funksjonalitet.
+    7 
+    8 Optimalisert for: Obs BYGG / Coop Norge
+    9 Kjorefrekvens: Ukentlig (mandag morgen)
+   10 
+   11 Funksjoner:
+   12 - Stortinget API (saker og horinger)
+   13 - Regjeringen.no horinger (4 departementer)
+   14 - Forbrukertilsynet RSS
+   15 - Lovdata endringssporing (Radar)
+   16 - 100+ byggevare-spesifikke nokkelord
+   17 - Radar/Sonar korrelasjon
+   18 - Brukervennlig rapport for varehussjefer
+   19 """
+   20 
+   21 import os
+   22 import sys
+   23 import json
+   24 import hashlib
+   25 import logging
+   26 import sqlite3
+   27 import smtplib
+   28 import re
+   29 from dataclasses import dataclass, field
+   30 from datetime import datetime, timedelta
+   31 from difflib import SequenceMatcher
+   32 from pathlib import Path
+   33 from typing import Dict, List, Any, Optional, Set
+   34 from email.mime.text import MIMEText
+   35 from email.mime.multipart import MIMEMultipart
+   36 from xml.etree import ElementTree as ET
+   37 
+   38 import requests
+   39 from requests.adapters import HTTPAdapter
+   40 from urllib3.util.retry import Retry
+   41 from bs4 import BeautifulSoup
+   42 
+   43 # =============================================================================
+   44 # KONFIGURASJON
+   45 # =============================================================================
+   46 
+   47 VERSION = "6.1"
+   48 APP_NAME = "LovSonar"
+   49 USER_AGENT = f"LovSonar/{VERSION} (Obs BYGG Compliance Monitor; github.com/Majac999/Lovsonar)"
+   50 
+   51 # Database og cache
+   52 DB_PATH = os.getenv("LOVSONAR_DB", "lovsonar_v6.db")
+   53 CACHE_FILE = os.getenv("LOVSONAR_CACHE", "lovsonar_cache_v6.json")
+   54 MAX_AGE_DAYS = 180
+   55 REQUEST_TIMEOUT = 30
+   56 CHANGE_THRESHOLD = 0.5
+   57 
+   58 # Logging
+   59 LOG_LEVEL = os.getenv("LOVSONAR_LOG_LEVEL", "INFO")
+   60 logging.basicConfig(
+   61     level=getattr(logging, LOG_LEVEL.upper()),
+   62     format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
+   63     datefmt="%Y-%m-%d %H:%M:%S",
+   64     handlers=[logging.StreamHandler(sys.stdout)]
+   65 )
+   66 logger = logging.getLogger(APP_NAME)
+   67 
+   68 # =============================================================================
+   69 # DATAMODELLER
+   70 # =============================================================================
+   71 
+   72 @dataclass(frozen=True)
+   73 class Keyword:
+   74     """Immutable nokkelord med vekt og kategori."""
+   75     term: str
+   76     weight: float
+   77     category: str
+   78     description: str = ""
+   79 
+   80 
+   81 @dataclass
+   82 class Signal:
+   83     """Regulatorisk signal fra en kilde."""
+   84     source: str
+   85     signal_id: str
+   86     title: str
+   87     url: str
+   88     signal_type: str = "sonar"  # "sonar" eller "radar"
+   89     
+   90     # Analysefelt
+   91     score: float = 0.0
+   92     priority: int = 3  # 1=kritisk, 2=viktig, 3=info
+   93     matched_keywords: List[str] = field(default_factory=list)
+   94     categories: List[str] = field(default_factory=list)
+   95     deadline: Optional[str] = None
+   96     change_percent: Optional[float] = None  # For radar
+   97     
+   98     def to_dict(self) -> Dict:
+   99         return {
+  100             "source": self.source,
+  101             "signal_id": self.signal_id,
+  102             "title": self.title,
+  103             "url": self.url,
+  104             "signal_type": self.signal_type,
+  105             "score": self.score,
+  106             "priority": self.priority,
+  107             "matched_keywords": self.matched_keywords,
+  108             "categories": self.categories,
+  109             "deadline": self.deadline,
+  110             "change_percent": self.change_percent,
+  111         }
+  112 
+  113 
+  114 @dataclass
+  115 class Correlation:
+  116     """Kobling mellom radar og sonar."""
+  117     radar_law: str
+  118     radar_change: float
+  119     sonar_title: str
+  120     sonar_source: str
+  121     connection_keywords: List[str]
+  122     action: str
+  123 
+  124 
+  125 # =============================================================================
+  126 # NOKKELORD-DATABASE - KOMPLETT FOR BYGGEVARE 2026
+  127 # =============================================================================
+  128 
+  129 KEYWORDS = [
+  130     # --- KJERNEVIRKSOMHET ---
+  131     Keyword("byggevare", 2.5, "core", "Byggevarehandel"),
+  132     Keyword("byggevarehus", 2.5, "core", "Byggevarehus"),
+  133     Keyword("trelast", 2.0, "core", "Trelast"),
+  134     Keyword("jernvare", 2.0, "core", "Jernvare"),
+  135     Keyword("byggemarked", 2.0, "core", "Byggemarked"),
+  136     Keyword("byggebransjen", 2.0, "core", "Byggebransjen"),
+  137     Keyword("detaljhandel", 1.5, "retail", "Detaljhandel"),
+  138     Keyword("varehandel", 1.5, "retail", "Varehandel"),
+  139     Keyword("forbruker", 1.0, "retail", "Forbruker"),
+  140     
+  141     # --- EU GREEN DEAL ---
+  142     Keyword("espr", 3.5, "eu_core", "Ecodesign for Sustainable Products"),
+  143     Keyword("ecodesign", 3.0, "eu_core", "Okodesign"),
+  144     Keyword("okodesign", 3.0, "eu_core", "Okodesign"),
+  145     Keyword("digitalt produktpass", 3.5, "digital", "Digital Product Passport"),
+  146     Keyword("dpp", 3.0, "digital", "DPP"),
+  147     Keyword("produktpass", 3.0, "digital", "Produktpass"),
+  148     Keyword("materialpass", 3.0, "digital", "Materialpass"),
+  149     
+  150     # --- CORPORATE SUSTAINABILITY ---
+  151     Keyword("csrd", 3.5, "reporting", "Corporate Sustainability Reporting"),
+  152     Keyword("csddd", 3.5, "due_diligence", "Corporate Due Diligence"),
+  153     Keyword("barekraftsrapportering", 3.0, "reporting", "Barekraftsrapportering"),
+  154     Keyword("esrs", 3.0, "reporting", "European Sustainability Reporting Standards"),
+  155     Keyword("taksonomi", 2.5, "reporting", "EU Taxonomy"),
+  156     
+  157     # --- GREEN CLAIMS (KRITISK) ---
+  158     Keyword("green claims", 3.5, "marketing", "Green Claims Directive"),
+  159     Keyword("gronnvasking", 3.5, "marketing", "Gronnvasking"),
+  160     Keyword("miljopastand", 3.0, "marketing", "Miljopastand"),
+  161     Keyword("miljopastander", 3.0, "marketing", "Miljopastander"),
+  162     Keyword("klimanoytral", 3.5, "marketing", "Forbudt fra 2026"),
+  163     Keyword("karbonnoytral", 3.5, "marketing", "Forbudt fra 2026"),
+  164     Keyword("co2-noytral", 3.5, "marketing", "Forbudt fra 2026"),
+  165     Keyword("klimakompensasjon", 3.0, "marketing", "Klimakompensasjon"),
+  166     Keyword("pef", 2.5, "marketing", "Product Environmental Footprint"),
+  167     Keyword("villedende", 2.5, "marketing", "Villedende markedsforing"),
+  168     
+  169     # --- EMBALLASJE ---
+  170     Keyword("ppwr", 3.0, "packaging", "Packaging Waste Regulation"),
+  171     Keyword("emballasje", 2.5, "packaging", "Emballasje"),
+  172     Keyword("engangsplast", 2.5, "packaging", "Engangsplast"),
+  173     Keyword("produsentansvar", 2.5, "packaging", "Produsentansvar"),
+  174     Keyword("pantesystem", 2.0, "packaging", "Pantesystem"),
+  175     Keyword("resirkulert", 2.0, "packaging", "Resirkulert innhold"),
+  176     
+  177     # --- EUDR AVSKOGING (KRITISK FOR TRELAST) ---
+  178     Keyword("eudr", 3.5, "deforestation", "EU Deforestation Regulation"),
+  179     Keyword("avskoging", 3.0, "deforestation", "Avskoging"),
+  180     Keyword("avskogingsfri", 3.5, "deforestation", "Avskogingsfri"),
+  181     Keyword("sporbarhet", 3.0, "deforestation", "Sporbarhet"),
+  182     Keyword("geolokalisering", 3.0, "deforestation", "Geolokalisering"),
+  183     Keyword("tommer", 2.5, "deforestation", "Tommer"),
+  184     Keyword("tommerforordning", 3.0, "deforestation", "Tommerforordning"),
+  185     Keyword("risikoland", 2.5, "deforestation", "Risikoland"),
+  186     Keyword("benchmarking", 2.5, "deforestation", "EUDR benchmarking"),
+  187     
+  188     # --- SIRKULAROKONOMI ---
+  189     Keyword("sirkular", 2.5, "circular", "Sirkularokonomi"),
+  190     Keyword("sirkularokonomi", 2.5, "circular", "Sirkularokonomi"),
+  191     Keyword("ombruk", 2.0, "circular", "Ombruk"),
+  192     Keyword("gjenvinning", 2.0, "circular", "Gjenvinning"),
+  193     Keyword("reparerbarhet", 3.0, "circular", "Right to repair"),
+  194     Keyword("reparasjonsindeks", 2.5, "circular", "Reparasjonsindeks"),
+  195     Keyword("avfallshierarki", 2.0, "circular", "Avfallshierarki"),
+  196     Keyword("livslopanalyse", 2.5, "circular", "LCA"),
+  197     Keyword("lca", 2.5, "circular", "Life Cycle Assessment"),
+  198     
+  199     # --- KJEMIKALIER (KRITISK) ---
+  200     Keyword("reach", 3.0, "chemicals", "REACH"),
+  201     Keyword("pfas", 3.5, "chemicals", "PFAS-forbud kritisk"),
+  202     Keyword("svhc", 3.0, "chemicals", "Substances of Very High Concern"),
+  203     Keyword("farlige stoffer", 2.5, "chemicals", "Farlige stoffer"),
+  204     Keyword("biocid", 2.5, "chemicals", "Biocid"),
+  205     Keyword("mikroplast", 3.0, "chemicals", "Mikroplast"),
+  206     Keyword("rohs", 2.5, "chemicals", "RoHS"),
+  207     Keyword("clp", 2.5, "chemicals", "CLP-forordningen"),
+  208     Keyword("formaldehyd", 3.0, "chemicals", "Formaldehyd i treprodukter"),
+  209     Keyword("voc", 2.5, "chemicals", "VOC"),
+  210     Keyword("kandidatlisten", 3.0, "chemicals", "REACH kandidatliste"),
+  211     
+  212     # --- MILJODEKLARASJONER ---
+  213     Keyword("miljodeklarasjon", 2.5, "sustainability", "EPD"),
+  214     Keyword("epd", 2.5, "sustainability", "Environmental Product Declaration"),
+  215     Keyword("klimaavtrykk", 2.5, "sustainability", "Klimaavtrykk"),
+  216     Keyword("karbonfotavtrykk", 2.5, "sustainability", "Karbonfotavtrykk"),
+  217     Keyword("klimagass", 2.5, "sustainability", "Klimagass"),
+  218     Keyword("scope 3", 2.5, "sustainability", "Scope 3 utslipp"),
+  219     
+  220     # --- NORSKE REGULERINGER ---
+  221     Keyword("apenhetsloven", 3.0, "compliance", "Apenhetsloven"),
+  222     Keyword("aktsomhet", 2.5, "compliance", "Aktsomhet"),
+  223     Keyword("aktsomhetsvurdering", 3.0, "compliance", "Aktsomhetsvurdering"),
+  224     Keyword("menneskerettigheter", 2.0, "compliance", "Menneskerettigheter"),
+  225     Keyword("markedsforingsloven", 2.5, "marketing", "Markedsforingsloven"),
+  226     Keyword("forbrukertilsynet", 2.5, "marketing", "Forbrukertilsynet"),
+  227     Keyword("eos-avtalen", 2.5, "eu", "EOS-avtalen"),
+  228     
+  229     # --- BYGGEREGELVERK ---
+  230     Keyword("tek17", 3.0, "building", "TEK17"),
+  231     Keyword("tek", 2.5, "building", "Byggteknisk forskrift"),
+  232     Keyword("byggteknisk", 2.5, "building", "Byggteknisk"),
+  233     Keyword("dok-forskriften", 3.0, "building", "DOK-forskriften"),
+  234     Keyword("ce-merking", 3.0, "building", "CE-merking"),
+  235     Keyword("ytelseserklering", 3.0, "building", "Ytelseserklering"),
+  236     Keyword("ns 3720", 2.5, "building", "NS 3720"),
+  237     Keyword("breeam", 2.0, "building", "BREEAM"),
+  238     Keyword("breeam-nor", 2.0, "building", "BREEAM-NOR"),
+  239     Keyword("energimerking", 2.5, "building", "Energimerking"),
+  240     Keyword("byggevareforordningen", 3.0, "building", "CPR"),
+  241     Keyword("cpr", 2.5, "building", "Construction Products Regulation"),
+  242     
+  243     # --- FRISTER OG PROSESS ---
+  244     Keyword("horingsfrist", 3.5, "deadline", "Horingsfrist"),
+  245     Keyword("horingsnotat", 3.0, "deadline", "Horingsnotat"),
+  246     Keyword("horing", 2.5, "deadline", "Horing"),
+  247     Keyword("ikrafttredelse", 3.5, "deadline", "Ikrafttredelse"),
+  248     Keyword("trer i kraft", 3.5, "deadline", "Trer i kraft"),
+  249     Keyword("forbud", 3.5, "legal", "Forbud"),
+  250     Keyword("pabud", 3.0, "legal", "Pabud"),
+  251     Keyword("overtredelsesgebyr", 3.0, "legal", "Overtredelsesgebyr"),
+  252     Keyword("implementering", 3.0, "deadline", "Implementering"),
+  253     Keyword("overgangsperiode", 2.5, "deadline", "Overgangsperiode"),
+  254 ]
+  255 
+  256 # Lag indeks for rask oppslag
+  257 KEYWORD_INDEX = {kw.term.lower(): kw for kw in KEYWORDS}
+  258 
+  259 
+  260 # =============================================================================
+  261 # HTTP SESSION MED RETRY
+  262 # =============================================================================
+  263 
+  264 def create_session() -> requests.Session:
+  265     """Oppretter robust HTTP-session."""
+  266     session = requests.Session()
+  267     
+  268     retry = Retry(
+  269         total=3,
+  270         backoff_factor=1.0,
+  271         status_forcelist=(429, 500, 502, 503, 504),
+  272         allowed_methods=["GET", "HEAD"],
+  273     )
+  274     
+  275     adapter = HTTPAdapter(max_retries=retry)
+  276     session.mount("https://", adapter)
+  277     session.mount("http://", adapter)
+  278     session.headers.update({"User-Agent": USER_AGENT})
+  279     
+  280     return session
+  281 
+  282 
+  283 # =============================================================================
+  284 # DATABASE
+  285 # =============================================================================
+  286 
+  287 class Database:
+  288     """SQLite database for deduplisering og historikk."""
+  289     
+  290     def __init__(self, path: str = DB_PATH):
+  291         self.path = path
+  292         self.conn = sqlite3.connect(path, check_same_thread=False)
+  293         self._init_schema()
+  294     
+  295     def _init_schema(self):
+  296         """Oppretter tabeller."""
+  297         self.conn.executescript("""
+  298             CREATE TABLE IF NOT EXISTS seen_items (
+  299                 item_id TEXT PRIMARY KEY,
+  300                 source TEXT NOT NULL,
+  301                 title TEXT,
+  302                 first_seen TEXT NOT NULL,
+  303                 last_checked TEXT NOT NULL
+  304             );
+  305             
+  306             CREATE TABLE IF NOT EXISTS signals (
+  307                 id INTEGER PRIMARY KEY AUTOINCREMENT,
+  308                 signal_id TEXT UNIQUE,
+  309                 source TEXT,
+  310                 title TEXT,
+  311                 url TEXT,
+  312                 signal_type TEXT,
+  313                 score REAL,
+  314                 priority INTEGER,
+  315                 keywords TEXT,
+  316                 categories TEXT,
+  317                 detected_at TEXT DEFAULT CURRENT_TIMESTAMP
+  318             );
+  319             
+  320             CREATE TABLE IF NOT EXISTS radar_hits (
+  321                 id INTEGER PRIMARY KEY AUTOINCREMENT,
+  322                 law_name TEXT,
+  323                 url TEXT,
+  324                 change_percent REAL,
+  325                 detected_at TEXT DEFAULT CURRENT_TIMESTAMP
+  326             );
+  327             
+  328             CREATE INDEX IF NOT EXISTS idx_seen_date ON seen_items(last_checked);
+  329             CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(detected_at);
+  330         """)
+  331         self.conn.commit()
+  332     
+  333     def is_seen(self, item_id: str) -> bool:
+  334         """Sjekk om element er sett."""
+  335         cursor = self.conn.execute(
+  336             "SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,)
+  337         )
+  338         return cursor.fetchone() is not None
+  339     
+  340     def mark_seen(self, signal: Signal):
+  341         """Marker element som sett."""
+  342         now = datetime.utcnow().isoformat()
+  343         self.conn.execute("""
+  344             INSERT OR REPLACE INTO seen_items 
+  345             (item_id, source, title, first_seen, last_checked)
+  346             VALUES (?, ?, ?, ?, ?)
+  347         """, (signal.signal_id, signal.source, signal.title[:500], now, now))
+  348         
+  349         self.conn.execute("""
+  350             INSERT OR IGNORE INTO signals
+  351             (signal_id, source, title, url, signal_type, score, priority, keywords, categories)
+  352             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
+  353         """, (
+  354             signal.signal_id, signal.source, signal.title, signal.url,
+  355             signal.signal_type, signal.score, signal.priority,
+  356             ",".join(signal.matched_keywords), ",".join(signal.categories)
+  357         ))
+  358         self.conn.commit()
+  359     
+  360     def save_radar_hit(self, law_name: str, url: str, change_percent: float):
+  361         """Lagrer radar-treff."""
+  362         self.conn.execute(
+  363             "INSERT INTO radar_hits (law_name, url, change_percent) VALUES (?, ?, ?)",
+  364             (law_name, url, change_percent)
+  365         )
+  366         self.conn.commit()
+  367     
+  368     def cleanup_old(self, days: int = MAX_AGE_DAYS):
+  369         """Fjern gamle poster."""
+  370         cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
+  371         cursor = self.conn.execute(
+  372             "DELETE FROM seen_items WHERE last_checked < ?", (cutoff,)
+  373         )
+  374         logger.info(f"Database cleanup: {cursor.rowcount} gamle poster fjernet")
+  375         self.conn.commit()
+  376     
+  377     def close(self):
+  378         """Lukk tilkobling."""
+  379         self.conn.close()
+  380 
+  381 
+  382 # =============================================================================
+  383 # RELEVANSANALYSE
+  384 # =============================================================================
+  385 
+  386 class Analyzer:
+  387     """Analyserer tekst mot nokkelord-database."""
+  388     
+  389     CRITICAL_THRESHOLD = 8.0
+  390     HIGH_THRESHOLD = 5.0
+  391     MINIMUM_THRESHOLD = 3.0
+  392     
+  393     def __init__(self):
+  394         self.index = KEYWORD_INDEX
+  395     
+  396     def analyze(self, text: str, context: str = "") -> Dict[str, Any]:
+  397         """Analyser tekst for relevans."""
+  398         if not text:
+  399             return self._empty_result()
+  400         
+  401         combined = f"{text} {context}".lower()
+  402         matches = []
+  403         
+  404         for term, keyword in self.index.items():
+  405             if term in combined:
+  406                 matches.append(keyword)
+  407         
+  408         # Scoring
+  409         base_score = sum(kw.weight for kw in matches)
+  410         categories = set(kw.category for kw in matches)
+  411         category_bonus = len(categories) * 0.3
+  412         
+  413         # Boost for kjernevirksomhet
+  414         core_matches = [kw for kw in matches if kw.category == "core"]
+  415         core_bonus = len(core_matches) * 0.5
+  416         
+  417         score = base_score + category_bonus + core_bonus
+  418         
+  419         # Prioritet
+  420         has_deadline = any(kw.category == "deadline" for kw in matches)
+  421         
+  422         if score >= self.CRITICAL_THRESHOLD or (has_deadline and score >= 6):
+  423             priority = 1
+  424         elif score >= self.HIGH_THRESHOLD or has_deadline:
+  425             priority = 2
+  426         elif score >= self.MINIMUM_THRESHOLD:
+  427             priority = 3
+  428         else:
+  429             priority = 4  # Ikke relevant
+  430         
+  431         # Deadline-ekstraksjon
+  432         deadline = self._extract_deadline(combined)
+  433         
+  434         return {
+  435             "is_relevant": score >= self.MINIMUM_THRESHOLD,
+  436             "score": round(score, 1),
+  437             "priority": priority,
+  438             "matched_keywords": [kw.term for kw in matches],
+  439             "categories": sorted(categories),
+  440             "deadline": deadline,
+  441         }
+  442     
+  443     def _extract_deadline(self, text: str) -> Optional[str]:
+  444         """Ekstraher frist fra tekst."""
+  445         patterns = [
+  446             r'(?:frist|horingsfrist)[:\s]+(\d{1,2})[.\s]+(\w+)\s+(\d{4})',
+  447             r'(?:trer i kraft|ikrafttredelse)[:\s]+(\d{1,2})[.\s]+(\w+)\s+(\d{4})',
+  448             r'innen\s+(\d{1,2})[.\s]+(\w+)\s+(\d{4})',
+  449         ]
+  450         for pattern in patterns:
+  451             match = re.search(pattern, text, re.IGNORECASE)
+  452             if match:
+  453                 return match.group(0)
+  454         return None
+  455     
+  456     def _empty_result(self) -> Dict:
+  457         return {
+  458             "is_relevant": False,
+  459             "score": 0.0,
+  460             "priority": 4,
+  461             "matched_keywords": [],
+  462             "categories": [],
+  463             "deadline": None,
+  464         }
+  465 
+  466 
+  467 # =============================================================================
+  468 # DATAKILDER
+  469 # =============================================================================
+  470 
+  471 class StortingetSource:
+  472     """Henter saker fra Stortingets API."""
+  473     
+  474     API_BASE = "https://data.stortinget.no/eksport"
+  475     WEB_BASE = "https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/"
+  476     
+  477     def __init__(self, session: requests.Session, analyzer: Analyzer, db: Database):
+  478         self.session = session
+  479         self.analyzer = analyzer
+  480         self.db = db
+  481     
+  482     def _get_sessions(self) -> List[str]:
+  483         """Beregn aktive sesjoner."""
+  484         now = datetime.now()
+  485         year = now.year
+  486         if now.month >= 10:
+  487             return [f"{year}-{year+1}"]
+  488         return [f"{year-1}-{year}"]
+  489     
+  490     def fetch(self) -> List[Signal]:
+  491         """Hent saker."""
+  492         signals = []
+  493         
+  494         for session_id in self._get_sessions():
+  495             for endpoint in ["saker", "horinger"]:
+  496                 url = f"{self.API_BASE}/{endpoint}?sesjonid={session_id}"
+  497                 
+  498                 try:
+  499                     resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
+  500                     resp.raise_for_status()
+  501                     
+  502                     signals.extend(self._parse_xml(resp.content, session_id))
+  503                     
+  504                 except Exception as e:
+  505                     logger.error(f"Stortinget {endpoint} feil: {e}")
+  506         
+  507         logger.info(f"Stortinget: {len(signals)} nye signaler")
+  508         return signals
+  509     
+  510     def _parse_xml(self, content: bytes, session_id: str) -> List[Signal]:
+  511         """Parser Stortinget XML."""
+  512         signals = []
+  513         
+  514         try:
+  515             root = ET.fromstring(content)
+  516         except ET.ParseError as e:
+  517             logger.error(f"Stortinget XML parse feil: {e}")
+  518             return []
+  519         
+  520         ns = {"s": "http://data.stortinget.no"}
+  521         
+  522         for sak in root.findall(".//s:sak", ns) + root.findall(".//s:horing", ns):
+  523             sak_id = self._get_text(sak, "s:id", ns)
+  524             tittel = self._get_text(sak, "s:tittel", ns) or self._get_text(sak, "s:kort_tittel", ns)
+  525             
+  526             if not sak_id or not tittel:
+  527                 continue
+  528             
+  529             signal_id = f"stortinget:{sak_id}"
+  530             
+  531             if self.db.is_seen(signal_id):
+  532                 continue
+  533             
+  534             komite = self._get_text(sak, ".//s:komite/s:navn", ns, "")
+  535             analysis = self.analyzer.analyze(tittel, komite)
+  536             
+  537             if not analysis["is_relevant"]:
+  538                 continue
+  539             
+  540             signal = Signal(
+  541                 source=f"Stortinget ({session_id})",
+  542                 signal_id=signal_id,
+  543                 title=tittel,
+  544                 url=f"{self.WEB_BASE}?p={sak_id}",
+  545                 signal_type="sonar",
+  546                 score=analysis["score"],
+  547                 priority=analysis["priority"],
+  548                 matched_keywords=analysis["matched_keywords"],
+  549                 categories=analysis["categories"],
+  550                 deadline=analysis["deadline"],
+  551             )
+  552             
+  553             self.db.mark_seen(signal)
+  554             signals.append(signal)
+  555         
+  556         return signals
+  557     
+  558     def _get_text(self, elem: ET.Element, path: str, ns: Dict, default: str = "") -> str:
+  559         """Hent tekst fra XML-element."""
+  560         found = elem.find(path, ns)
+  561         return found.text.strip() if found is not None and found.text else default
+  562 
+  563 
+  564 class RegjeringenSource:
+  565     """Henter horinger fra Regjeringen.no."""
+  566     
+  567     SOURCES = {
+  568         "Klima og miljo": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?ownerid=668",
+  569         "Naering": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?ownerid=709",
+  570         "Forbruker": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?ownerid=298",
+  571         "Kommunal": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?ownerid=543",
+  572     }
+  573     
+  574     SELECTORS = [
+  575         "a[href*='/hoeringer/']",
+  576         "a[href*='/horinger/']",
+  577         ".444-LI a",
+  578     ]
+  579     
+  580     def __init__(self, session: requests.Session, analyzer: Analyzer, db: Database):
+  581         self.session = session
+  582         self.analyzer = analyzer
+  583         self.db = db
+  584     
+  585     def fetch(self) -> List[Signal]:
+  586         """Hent horinger."""
+  587         signals = []
+  588         
+  589         for name, url in self.SOURCES.items():
+  590             try:
+  591                 resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
+  592                 resp.raise_for_status()
+  593                 
+  594                 soup = BeautifulSoup(resp.content, "html.parser")
+  595                 
+  596                 # Prov selektorer
+  597                 links = []
+  598                 for selector in self.SELECTORS:
+  599                     links = soup.select(selector)[:15]
+  600                     if links:
+  601                         break
+  602                 
+  603                 for link in links:
+  604                     title = link.get_text(strip=True)
+  605                     href = link.get("href", "")
+  606                     
+  607                     if not title or len(title) < 10:
+  608                         continue
+  609                     
+  610                     if not href.startswith("http"):
+  611                         href = "https://www.regjeringen.no" + href
+  612                     
+  613                     signal_id = f"regjeringen:{hashlib.sha256(href.encode()).hexdigest()[:12]}"
+  614                     
+  615                     if self.db.is_seen(signal_id):
+  616                         continue
+  617                     
+  618                     analysis = self.analyzer.analyze(title)
+  619                     
+  620                     if not analysis["is_relevant"]:
+  621                         continue
+  622                     
+  623                     signal = Signal(
+  624                         source=f"Regjeringen ({name})",
+  625                         signal_id=signal_id,
+  626                         title=title,
+  627                         url=href,
+  628                         signal_type="sonar",
+  629                         score=analysis["score"],
+  630                         priority=analysis["priority"],
+  631                         matched_keywords=analysis["matched_keywords"],
+  632                         categories=analysis["categories"],
+  633                         deadline=analysis["deadline"],
+  634                     )
+  635                     
+  636                     self.db.mark_seen(signal)
+  637                     signals.append(signal)
+  638                 
+  639             except Exception as e:
+  640                 logger.error(f"Regjeringen {name} feil: {e}")
+  641         
+  642         logger.info(f"Regjeringen: {len(signals)} nye signaler")
+  643         return signals
+  644 
+  645 
+  646 class ForbrukertilsynetSource:
+  647     """Henter nyheter fra Forbrukertilsynet RSS."""
+  648     
+  649     URL = "https://www.forbrukertilsynet.no/feed"
+  650     
+  651     def __init__(self, session: requests.Session, analyzer: Analyzer, db: Database):
+  652         self.session = session
+  653         self.analyzer = analyzer
+  654         self.db = db
+  655     
+  656     def fetch(self) -> List[Signal]:
+  657         """Hent RSS-feed."""
+  658         signals = []
+  659         
+  660         try:
+  661             resp = self.session.get(self.URL, timeout=REQUEST_TIMEOUT)
+  662             resp.raise_for_status()
+  663             
+  664             root = ET.fromstring(resp.content)
+  665             
+  666             for item in root.findall(".//item")[:20]:
+  667                 title = self._get_text(item, "title")
+  668                 link = self._get_text(item, "link")
+  669                 description = self._get_text(item, "description")
+  670                 
+  671                 if not title or not link:
+  672                     continue
+  673                 
+  674                 signal_id = f"forbrukertilsynet:{hashlib.sha256(link.encode()).hexdigest()[:12]}"
+  675                 
+  676                 if self.db.is_seen(signal_id):
+  677                     continue
+  678                 
+  679                 # Fjern HTML fra description
+  680                 if description:
+  681                     description = BeautifulSoup(description, "html.parser").get_text()
+  682                 
+  683                 analysis = self.analyzer.analyze(title, description or "")
+  684                 
+  685                 if not analysis["is_relevant"]:
+  686                     continue
+  687                 
+  688                 signal = Signal(
+  689                     source="Forbrukertilsynet",
+  690                     signal_id=signal_id,
+  691                     title=title,
+  692                     url=link,
+  693                     signal_type="sonar",
+  694                     score=analysis["score"],
+  695                     priority=analysis["priority"],
+  696                     matched_keywords=analysis["matched_keywords"],
+  697                     categories=analysis["categories"],
+  698                     deadline=analysis["deadline"],
+  699                 )
+  700                 
+  701                 self.db.mark_seen(signal)
+  702                 signals.append(signal)
+  703             
+  704         except Exception as e:
+  705             logger.error(f"Forbrukertilsynet feil: {e}")
+  706         
+  707         logger.info(f"Forbrukertilsynet: {len(signals)} nye signaler")
+  708         return signals
+  709     
+  710     def _get_text(self, elem: ET.Element, tag: str) -> str:
+  711         """Hent tekst fra element."""
+  712         found = elem.find(tag)
+  713         return found.text.strip() if found is not None and found.text else ""
+  714 
+  715 
+  716 class LovdataRadar:
+  717     """Overvaker endringer i lover og forskrifter."""
+  718     
+  719     LAWS = {
+  720         "Apenhetsloven": "https://lovdata.no/dokument/NL/lov/2021-06-18-99",
+  721         "Markedsforingsloven": "https://lovdata.no/dokument/NL/lov/2009-01-09-2",
+  722         "Produktkontrolloven": "https://lovdata.no/dokument/NL/lov/1976-06-11-79",
+  723         "Forbrukerkjopsloven": "https://lovdata.no/dokument/NL/lov/2002-06-21-34",
+  724     }
+  725     
+  726     REGULATIONS = {
+  727         "TEK17": "https://lovdata.no/dokument/SF/forskrift/2017-06-19-840",
+  728         "Byggevareforskriften": "https://lovdata.no/dokument/SF/forskrift/2013-12-17-1579",
+  729         "Avfallsforskriften": "https://lovdata.no/dokument/SF/forskrift/2004-06-01-930",
+  730         "REACH-forskriften": "https://lovdata.no/dokument/SF/forskrift/2008-05-30-516",
+  731         "Produktforskriften": "https://lovdata.no/dokument/SF/forskrift/2004-06-01-922",
+  732     }
+  733     
+  734     def __init__(self, session: requests.Session, db: Database):
+  735         self.session = session
+  736         self.db = db
+  737         self.cache = self._load_cache()
+  738     
+  739     def _load_cache(self) -> Dict:
+  740         """Last inn cache."""
+  741         if Path(CACHE_FILE).exists():
+  742             with open(CACHE_FILE, 'r', encoding='utf-8') as f:
+  743                 return json.load(f)
+  744         return {}
+  745     
+  746     def _save_cache(self):
+  747         """Lagre cache."""
+  748         with open(CACHE_FILE, 'w', encoding='utf-8') as f:
+  749             json.dump(self.cache, f, ensure_ascii=False, indent=2)
+  750     
+  751     def fetch(self) -> List[Signal]:
+  752         """Sjekk for endringer."""
+  753         signals = []
+  754         all_docs = {**self.LAWS, **self.REGULATIONS}
+  755         
+  756         for name, url in all_docs.items():
+  757             try:
+  758                 resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
+  759                 resp.raise_for_status()
+  760                 
+  761                 soup = BeautifulSoup(resp.content, "html.parser")
+  762                 
+  763                 # Fjern stoy
+  764                 for elem in soup(["script", "style", "nav", "footer", "header"]):
+  765                     elem.decompose()
+  766                 
+  767                 text = re.sub(r'\s+', ' ', soup.get_text()).strip()
+  768                 new_hash = hashlib.sha256(text.encode()).hexdigest()
+  769                 
+  770                 prev = self.cache.get(name, {})
+  771                 
+  772                 if prev and new_hash != prev.get("hash"):
+  773                     # Beregn endringsprosent
+  774                     prev_text = prev.get("text", "")[:5000]
+  775                     curr_text = text[:5000]
+  776                     similarity = SequenceMatcher(None, prev_text, curr_text).ratio()
+  777                     change_pct = round((1 - similarity) * 100, 2)
+  778                     
+  779                     if change_pct >= CHANGE_THRESHOLD:
+  780                         signal = Signal(
+  781                             source="Lovdata Radar",
+  782                             signal_id=f"radar:{hashlib.sha256(url.encode()).hexdigest()[:12]}:{datetime.now().strftime('%Y%m%d')}",
+  783                             title=f"Endring detektert: {name}",
+  784                             url=url,
+  785                             signal_type="radar",
+  786                             score=change_pct,
+  787                             priority=1 if change_pct > 5 else 2,
+  788                             change_percent=change_pct,
+  789                         )
+  790                         
+  791                         self.db.save_radar_hit(name, url, change_pct)
+  792                         signals.append(signal)
+  793                         
+  794                         logger.info(f"Radar: {name} endret {change_pct}%")
+  795                 
+  796                 # Oppdater cache
+  797                 self.cache[name] = {
+  798                     "hash": new_hash,
+  799                     "text": text[:5000],
+  800                     "checked": datetime.now().isoformat(),
+  801                 }
+  802                 
+  803             except Exception as e:
+  804                 logger.error(f"Radar {name} feil: {e}")
+  805         
+  806         self._save_cache()
+  807         logger.info(f"Radar: {len(signals)} endringer detektert")
+  808         return signals
+  809 
+  810 
+  811 # =============================================================================
+  812 # KORRELASJON
+  813 # =============================================================================
+  814 
+  815 def correlate_signals(radar: List[Signal], sonar: List[Signal]) -> List[Correlation]:
+  816     """Kobler radar og sonar signaler."""
+  817     correlations = []
+  818     
+  819     keyword_map = {
+  820         "TEK17": {"tek17", "tek", "byggteknisk", "byggevare"},
+  821         "Byggevareforskriften": {"dok", "ce-merking", "ytelseserklering", "byggevare"},
+  822         "REACH-forskriften": {"reach", "svhc", "kjemikalier", "farlige stoffer", "pfas"},
+  823         "Markedsforingsloven": {"markedsforing", "gronnvasking", "villedende", "miljopastand"},
+  824         "Apenhetsloven": {"apenhet", "aktsomhet", "menneskerettigheter"},
+  825     }
+  826     
+  827     for r in radar:
+  828         law_name = r.title.replace("Endring detektert: ", "")
+  829         law_keywords = keyword_map.get(law_name, set())
+  830         
+  831         for s in sonar:
+  832             sonar_keywords = set(kw.lower() for kw in s.matched_keywords)
+  833             overlap = law_keywords & sonar_keywords
+  834             
+  835             if overlap:
+  836                 correlations.append(Correlation(
+  837                     radar_law=law_name,
+  838                     radar_change=r.change_percent or 0,
+  839                     sonar_title=s.title,
+  840                     sonar_source=s.source,
+  841                     connection_keywords=list(overlap),
+  842                     action=f"Undersok om endring i {law_name} henger sammen med: {s.title[:50]}..."
+  843                 ))
+  844     
+  845     return correlations
+  846 
+  847 
+  848 # =============================================================================
+  849 # RAPPORT - BRUKERVENNLIG
+  850 # =============================================================================
+  851 
+  852 def generate_report(
+  853     sonar: List[Signal],
+  854     radar: List[Signal],
+  855     correlations: List[Correlation]
+  856 ) -> str:
+  857     """Genererer brukervennlig ukentlig rapport."""
+  858     
+  859     now = datetime.now()
+  860     
+  861     # Sorter etter prioritet
+  862     sonar.sort(key=lambda s: (s.priority, -s.score))
+  863     
+  864     lines = []
+  865     
+  866     # === HEADER ===
+  867     lines.append("")
+  868     lines.append("=" * 70)
+  869     lines.append("   LOVSONAR UKENTLIG RAPPORT - OBS BYGG")
+  870     lines.append("=" * 70)
+  871     lines.append(f"   Uke {now.isocalendar()[1]}, {now.year}")
+  872     lines.append(f"   Generert: {now.strftime('%A %d. %B %Y kl. %H:%M')}")
+  873     lines.append("=" * 70)
+  874     lines.append("")
+  875     
+  876     # === OPPSUMMERING ===
+  877     lines.append("OPPSUMMERING")
+  878     lines.append("-" * 40)
+  879     
+  880     critical = [s for s in sonar if s.priority == 1]
+  881     important = [s for s in sonar if s.priority == 2]
+  882     info = [s for s in sonar if s.priority == 3]
+  883     
+  884     lines.append(f"   Totalt signaler:     {len(sonar)}")
+  885     lines.append(f"   Lovendringer:        {len(radar)}")
+  886     lines.append(f"   Korrelasjoner:       {len(correlations)}")
+  887     lines.append("")
+  888     lines.append(f"   KRITISK (handling):  {len(critical)}")
+  889     lines.append(f"   VIKTIG (planlegg):   {len(important)}")
+  890     lines.append(f"   INFO (folg med):     {len(info)}")
+  891     lines.append("")
+  892     
+  893     # === LOVENDRINGER (RADAR) ===
+  894     if radar:
+  895         lines.append("")
+  896         lines.append("=" * 70)
+  897         lines.append("   LOVENDRINGER DETEKTERT")
+  898         lines.append("=" * 70)
+  899         lines.append("")
+  900         
+  901         for r in radar:
+  902             law_name = r.title.replace("Endring detektert: ", "")
+  903             lines.append(f"   {law_name}")
+  904             lines.append(f"   Endring: {r.change_percent}%")
+  905             lines.append(f"   {r.url}")
+  906             lines.append(f"   -> Sjekk hva som er endret")
+  907             lines.append("")
+  908     
+  909     # === KORRELASJONER ===
+  910     if correlations:
+  911         lines.append("")
+  912         lines.append("=" * 70)
+  913         lines.append("   KOBLINGER FUNNET (Radar + Sonar)")
+  914         lines.append("=" * 70)
+  915         lines.append("")
+  916         
+  917         for c in correlations:
+  918             lines.append(f"   Lov: {c.radar_law} ({c.radar_change}% endret)")
+  919             lines.append(f"   Signal: {c.sonar_title[:55]}...")
+  920             lines.append(f"   Kobling: {', '.join(c.connection_keywords)}")
+  921             lines.append(f"   -> {c.action}")
+  922             lines.append("")
+  923     
+  924     # === KRITISKE SIGNALER ===
+  925     if critical:
+  926         lines.append("")
+  927         lines.append("=" * 70)
+  928         lines.append("   KRITISK - KREVER HANDLING")
+  929         lines.append("=" * 70)
+  930         lines.append("")
+  931         
+  932         for s in critical:
+  933             lines.append(f"   {s.title}")
+  934             lines.append(f"   Kilde: {s.source}")
+  935             lines.append(f"   Score: {s.score} | {', '.join(s.categories[:3])}")
+  936             if s.deadline:
+  937                 lines.append(f"   FRIST: {s.deadline}")
+  938             lines.append(f"   Nokkelord: {', '.join(s.matched_keywords[:5])}")
+  939             lines.append(f"   {s.url}")
+  940             lines.append("")
+  941     
+  942     # === VIKTIGE SIGNALER ===
+  943     if important:
+  944         lines.append("")
+  945         lines.append("=" * 70)
+  946         lines.append("   VIKTIG - PLANLEGG RESPONS")
+  947         lines.append("=" * 70)
+  948         lines.append("")
+  949         
+  950         for s in important[:10]:
+  951             lines.append(f"   {s.title[:60]}...")
+  952             lines.append(f"   {s.source} | Score: {s.score}")
+  953             lines.append(f"   {s.url}")
+  954             lines.append("")
+  955     
+  956     # === INFO SIGNALER (kort) ===
+  957     if info:
+  958         lines.append("")
+  959         lines.append("=" * 70)
+  960         lines.append("   INFO - FOLG MED")
+  961         lines.append("=" * 70)
+  962         lines.append("")
+  963         
+  964         for s in info[:5]:
+  965             lines.append(f"   - {s.title[:55]}... ({s.source})")
+  966         lines.append("")
+  967     
+  968     # === INGEN SIGNALER ===
+  969     if not sonar and not radar:
+  970         lines.append("")
+  971         lines.append("   Ingen nye signaler denne uken.")
+  972         lines.append("   Neste rapport: mandag kl. 07:00")
+  973         lines.append("")
+  974     
+  975     # === FOOTER ===
+  976     lines.append("")
+  977     lines.append("=" * 70)
+  978     lines.append("   SLUTT PA RAPPORT")
+  979     lines.append(f"   LovSonar v{VERSION} | github.com/Majac999/Lovsonar")
+  980     lines.append("=" * 70)
+  981     lines.append("")
+  982     
+  983     return "\n".join(lines)
+  984 
+  985 
+  986 def send_email(report: str, signal_count: int, radar_count: int) -> bool:
+  987     """Sender rapport via e-post."""
+  988     user = os.getenv("EMAIL_USER", "").strip()
+  989     password = os.getenv("EMAIL_PASS", "").strip()
+  990     recipient = os.getenv("EMAIL_RECIPIENT", "").strip()
+  991     
+  992     if not all([user, password, recipient]):
+  993         logger.warning("E-post ikke konfigurert")
+  994         return False
+  995     
+  996     if signal_count == 0 and radar_count == 0:
+  997         logger.info("Ingen signaler - sender ikke e-post")
+  998         return False
+  999     
+ 1000     now = datetime.now()
+ 1001     
+ 1002     msg = MIMEMultipart()
+ 1003     msg["Subject"] = f"LovSonar Uke {now.isocalendar()[1]}: {signal_count} signaler, {radar_count} lovendringer"
+ 1004     msg["From"] = user
+ 1005     msg["To"] = recipient
+ 1006     
+ 1007     msg.attach(MIMEText(report, "plain", "utf-8"))
+ 1008     
+ 1009     try:
+ 1010         with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
+ 1011             server.login(user, password)
+ 1012             server.send_message(msg)
+ 1013         
+ 1014         logger.info(f"Rapport sendt til {recipient}")
+ 1015         return True
+ 1016         
+ 1017     except Exception as e:
+ 1018         logger.error(f"E-post feil: {e}")
+ 1019         return False
+ 1020 
+ 1021 
+ 1022 # =============================================================================
+ 1023 # HOVEDPROGRAM
+ 1024 # =============================================================================
+ 1025 
+ 1026 def main():
+ 1027     """Hovedfunksjon."""
+ 1028     logger.info("=" * 60)
+ 1029     logger.info(f"LovSonar v{VERSION} starter")
+ 1030     logger.info("=" * 60)
+ 1031     
+ 1032     # Initialiser
+ 1033     session = create_session()
+ 1034     db = Database()
+ 1035     analyzer = Analyzer()
+ 1036     
+ 1037     all_sonar: List[Signal] = []
+ 1038     all_radar: List[Signal] = []
+ 1039     
+ 1040     try:
+ 1041         # 1. Stortinget
+ 1042         stortinget = StortingetSource(session, analyzer, db)
+ 1043         all_sonar.extend(stortinget.fetch())
+ 1044         
+ 1045         # 2. Regjeringen
+ 1046         regjeringen = RegjeringenSource(session, analyzer, db)
+ 1047         all_sonar.extend(regjeringen.fetch())
+ 1048         
+ 1049         # 3. Forbrukertilsynet
+ 1050         forbrukertilsynet = ForbrukertilsynetSource(session, analyzer, db)
+ 1051         all_sonar.extend(forbrukertilsynet.fetch())
+ 1052         
+ 1053         # 4. Lovdata Radar
+ 1054         radar = LovdataRadar(session, db)
+ 1055         all_radar.extend(radar.fetch())
+ 1056         
+ 1057         # 5. Korreler
+ 1058         correlations = correlate_signals(all_radar, all_sonar)
+ 1059         
+ 1060         # 6. Generer rapport
+ 1061         report = generate_report(all_sonar, all_radar, correlations)
+ 1062         print(report)
+ 1063         
+ 1064         # 7. Lagre rapport
+ 1065         report_path = Path("lovsonar_rapport.txt")
+ 1066         with open(report_path, 'w', encoding='utf-8') as f:
+ 1067             f.write(report)
+ 1068         logger.info(f"Rapport lagret: {report_path}")
+ 1069         
+ 1070         # 8. Send e-post
+ 1071         send_email(report, len(all_sonar), len(all_radar))
+ 1072         
+ 1073         # 9. Cleanup
+ 1074         db.cleanup_old()
+ 1075         
+ 1076         logger.info("=" * 60)
+ 1077         logger.info(f"Ferdig: {len(all_sonar)} signaler, {len(all_radar)} lovendringer")
+ 1078         logger.info("=" * 60)
+ 1079         
+ 1080     finally:
+ 1081         db.close()
+ 1082     
+ 1083     return 0
+ 1084 
+ 1085 
+ 1086 if __name__ == "__main__":
+ 1087     sys.exit(main())
