import os
import sys
import json
import hashlib
import logging
import sqlite3
import smtplib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Any, Optional, Set
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# =============================================================================
# KONFIGURASJON
# =============================================================================

VERSION = "6.1"
APP_NAME = "LovSonar"
USER_AGENT = f"LovSonar/{VERSION} (Obs BYGG Compliance Monitor; github.com/Majac999/Lovsonar)"

# Database og cache
DB_PATH = os.getenv("LOVSONAR_DB", "lovsonar_v6.db")
CACHE_FILE = os.getenv("LOVSONAR_CACHE", "lovsonar_cache_v6.json")
MAX_AGE_DAYS = 180
REQUEST_TIMEOUT = 30
CHANGE_THRESHOLD = 0.5

# Logging
LOG_LEVEL = os.getenv("LOVSONAR_LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper()),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(APP_NAME)

# =============================================================================
# DATAMODELLER
# =============================================================================

@dataclass(frozen=True)
class Keyword:
    """Immutable nokkelord med vekt og kategori."""
    term: str
    weight: float
    category: str
    description: str = ""


@dataclass
class Signal:
    """Regulatorisk signal fra en kilde."""
    source: str
    signal_id: str
    title: str
    url: str
    signal_type: str = "sonar"  # "sonar" eller "radar"

    # Analysefelt
    score: float = 0.0
    priority: int = 3  # 1=kritisk, 2=viktig, 3=info
    matched_keywords: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)
    deadline: Optional[str] = None
    change_percent: Optional[float] = None  # For radar

    def to_dict(self) -> Dict:
        return {
            "source": self.source,
            "signal_id": self.signal_id,
            "title": self.title,
            "url": self.url,
            "signal_type": self.signal_type,
            "score": self.score,
            "priority": self.priority,
            "matched_keywords": self.matched_keywords,
            "categories": self.categories,
            "deadline": self.deadline,
            "change_percent": self.change_percent,
        }


@dataclass
class Correlation:
    """Kobling mellom radar og sonar."""
    radar_law: str
    radar_change: float
    sonar_title: str
    sonar_source: str
    connection_keywords: List[str]
    action: str


# =============================================================================
# NOKKELORD-DATABASE - KOMPLETT FOR BYGGEVARE 2026
# =============================================================================

KEYWORDS = [
    # --- KJERNEVIRKSOMHET ---
    Keyword("byggevare", 2.5, "core", "Byggevarehandel"),
    Keyword("byggevarehus", 2.5, "core", "Byggevarehus"),
    Keyword("trelast", 2.0, "core", "Trelast"),
    Keyword("jernvare", 2.0, "core", "Jernvare"),
    Keyword("byggemarked", 2.0, "core", "Byggemarked"),
    Keyword("byggebransjen", 2.0, "core", "Byggebransjen"),
    Keyword("detaljhandel", 1.5, "retail", "Detaljhandel"),
    Keyword("varehandel", 1.5, "retail", "Varehandel"),
    Keyword("forbruker", 1.0, "retail", "Forbruker"),

    # --- EU GREEN DEAL ---
    Keyword("espr", 3.5, "eu_core", "Ecodesign for Sustainable Products"),
    Keyword("ecodesign", 3.0, "eu_core", "Okodesign"),
    Keyword("okodesign", 3.0, "eu_core", "Okodesign"),
    Keyword("digitalt produktpass", 3.5, "digital", "Digital Product Passport"),
    Keyword("dpp", 3.0, "digital", "DPP"),
    Keyword("produktpass", 3.0, "digital", "Produktpass"),
    Keyword("materialpass", 3.0, "digital", "Materialpass"),

    # --- CORPORATE SUSTAINABILITY ---
    Keyword("csrd", 3.5, "reporting", "Corporate Sustainability Reporting"),
    Keyword("csddd", 3.5, "due_diligence", "Corporate Due Diligence"),
    Keyword("barekraftsrapportering", 3.0, "reporting", "Barekraftsrapportering"),
    Keyword("esrs", 3.0, "reporting", "European Sustainability Reporting Standards"),
    Keyword("taksonomi", 2.5, "reporting", "EU Taxonomy"),

    # --- GREEN CLAIMS (KRITISK) ---
    Keyword("green claims", 3.5, "marketing", "Green Claims Directive"),
    Keyword("gronnvasking", 3.5, "marketing", "Gronnvasking"),
    Keyword("miljopastand", 3.0, "marketing", "Miljopastand"),
    Keyword("miljopastander", 3.0, "marketing", "Miljopastander"),
    Keyword("klimanoytral", 3.5, "marketing", "Forbudt fra 2026"),
    Keyword("karbonnoytral", 3.5, "marketing", "Forbudt fra 2026"),
    Keyword("co2-noytral", 3.5, "marketing", "Forbudt fra 2026"),
    Keyword("klimakompensasjon", 3.0, "marketing", "Klimakompensasjon"),
    Keyword("pef", 2.5, "marketing", "Product Environmental Footprint"),
    Keyword("villedende", 2.5, "marketing", "Villedende markedsforing"),

    # --- EMBALLASJE ---
    Keyword("ppwr", 3.0, "packaging", "Packaging Waste Regulation"),
    Keyword("emballasje", 2.5, "packaging", "Emballasje"),
    Keyword("engangsplast", 2.5, "packaging", "Engangsplast"),
    Keyword("produsentansvar", 2.5, "packaging", "Produsentansvar"),
    Keyword("pantesystem", 2.0, "packaging", "Pantesystem"),
    Keyword("resirkulert", 2.0, "packaging", "Resirkulert innhold"),

    # --- EUDR AVSKOGING (KRITISK FOR TRELAST) ---
    Keyword("eudr", 3.5, "deforestation", "EU Deforestation Regulation"),
    Keyword("avskoging", 3.0, "deforestation", "Avskoging"),
    Keyword("avskogingsfri", 3.5, "deforestation", "Avskogingsfri"),
    Keyword("sporbarhet", 3.0, "deforestation", "Sporbarhet"),
    Keyword("geolokalisering", 3.0, "deforestation", "Geolokalisering"),
    Keyword("tommer", 2.5, "deforestation", "Tommer"),
    Keyword("tommerforordning", 3.0, "deforestation", "Tommerforordning"),
    Keyword("risikoland", 2.5, "deforestation", "Risikoland"),
    Keyword("benchmarking", 2.5, "deforestation", "EUDR benchmarking"),

    # --- SIRKULAROKONOMI ---
    Keyword("sirkular", 2.5, "circular", "Sirkularokonomi"),
    Keyword("sirkularokonomi", 2.5, "circular", "Sirkularokonomi"),
    Keyword("ombruk", 2.0, "circular", "Ombruk"),
    Keyword("gjenvinning", 2.0, "circular", "Gjenvinning"),
    Keyword("reparerbarhet", 3.0, "circular", "Right to repair"),
    Keyword("reparasjonsindeks", 2.5, "circular", "Reparasjonsindeks"),
    Keyword("avfallshierarki", 2.0, "circular", "Avfallshierarki"),
    Keyword("livslopanalyse", 2.5, "circular", "LCA"),
    Keyword("lca", 2.5, "circular", "Life Cycle Assessment"),

    # --- KJEMIKALIER (KRITISK) ---
    Keyword("reach", 3.0, "chemicals", "REACH"),
    Keyword("pfas", 3.5, "chemicals", "PFAS-forbud kritisk"),
    Keyword("svhc", 3.0, "chemicals", "Substances of Very High Concern"),
    Keyword("farlige stoffer", 2.5, "chemicals", "Farlige stoffer"),
    Keyword("biocid", 2.5, "chemicals", "Biocid"),
    Keyword("mikroplast", 3.0, "chemicals", "Mikroplast"),
    Keyword("rohs", 2.5, "chemicals", "RoHS"),
    Keyword("clp", 2.5, "chemicals", "CLP-forordningen"),
    Keyword("formaldehyd", 3.0, "chemicals", "Formaldehyd i treprodukter"),
    Keyword("voc", 2.5, "chemicals", "VOC"),
    Keyword("kandidatlisten", 3.0, "chemicals", "REACH kandidatliste"),

    # --- MILJODEKLARASJONER ---
    Keyword("miljodeklarasjon", 2.5, "sustainability", "EPD"),
    Keyword("epd", 2.5, "sustainability", "Environmental Product Declaration"),
    Keyword("klimaavtrykk", 2.5, "sustainability", "Klimaavtrykk"),
    Keyword("karbonfotavtrykk", 2.5, "sustainability", "Karbonfotavtrykk"),
    Keyword("klimagass", 2.5, "sustainability", "Klimagass"),
    Keyword("scope 3", 2.5, "sustainability", "Scope 3 utslipp"),

    # --- NORSKE REGULERINGER ---
    Keyword("apenhetsloven", 3.0, "compliance", "Apenhetsloven"),
    Keyword("aktsomhet", 2.5, "compliance", "Aktsomhet"),
    Keyword("aktsomhetsvurdering", 3.0, "compliance", "Aktsomhetsvurdering"),
    Keyword("menneskerettigheter", 2.0, "compliance", "Menneskerettigheter"),
    Keyword("markedsforingsloven", 2.5, "marketing", "Markedsforingsloven"),
    Keyword("forbrukertilsynet", 2.5, "marketing", "Forbrukertilsynet"),
    Keyword("eos-avtalen", 2.5, "eu", "EOS-avtalen"),

    # --- BYGGEREGELVERK ---
    Keyword("tek17", 3.0, "building", "TEK17"),
    Keyword("tek", 2.5, "building", "Byggteknisk forskrift"),
    Keyword("byggteknisk", 2.5, "building", "Byggteknisk"),
    Keyword("dok-forskriften", 3.0, "building", "DOK-forskriften"),
    Keyword("ce-merking", 3.0, "building", "CE-merking"),
    Keyword("ytelseserklering", 3.0, "building", "Ytelseserklering"),
    Keyword("ns 3720", 2.5, "building", "NS 3720"),
    Keyword("breeam", 2.0, "building", "BREEAM"),
    Keyword("breeam-nor", 2.0, "building", "BREEAM-NOR"),
    Keyword("energimerking", 2.5, "building", "Energimerking"),
    Keyword("byggevareforordningen", 3.0, "building", "CPR"),
    Keyword("cpr", 2.5, "building", "Construction Products Regulation"),

    # --- FRISTER OG PROSESS ---
    Keyword("horingsfrist", 3.5, "deadline", "Horingsfrist"),
    Keyword("horingsnotat", 3.0, "deadline", "Horingsnotat"),
    Keyword("horing", 2.5, "deadline", "Horing"),
    Keyword("ikrafttredelse", 3.5, "deadline", "Ikrafttredelse"),
    Keyword("trer i kraft", 3.5, "deadline", "Trer i kraft"),
    Keyword("forbud", 3.5, "legal", "Forbud"),
    Keyword("pabud", 3.0, "legal", "Pabud"),
    Keyword("overtredelsesgebyr", 3.0, "legal", "Overtredelsesgebyr"),
    Keyword("implementering", 3.0, "deadline", "Implementering"),
    Keyword("overgangsperiode", 2.5, "deadline", "Overgangsperiode"),
]

# Lag indeks for rask oppslag
KEYWORD_INDEX = {kw.term.lower(): kw for kw in KEYWORDS}


# =============================================================================
# HTTP SESSION MED RETRY
# =============================================================================

def create_session() -> requests.Session:
    """Oppretter robust HTTP-session."""
    session = requests.Session()

    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=["GET", "HEAD"],
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})

    return session


# =============================================================================
# DATABASE
# =============================================================================

class Database:
    """SQLite database for deduplisering og historikk."""

    def __init__(self, path: str = DB_PATH):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        """Oppretter tabeller."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS seen_items (
                item_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT,
                first_seen TEXT NOT NULL,
                last_checked TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT UNIQUE,
                source TEXT,
                title TEXT,
                url TEXT,
                signal_type TEXT,
                score REAL,
                priority INTEGER,
                keywords TEXT,
                categories TEXT,
                detected_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS radar_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                law_name TEXT,
                url TEXT,
                change_percent REAL,
                detected_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_seen_date ON seen_items(last_checked);
            CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(detected_at);
        """)
        self.conn.commit()

    def is_seen(self, item_id: str) -> bool:
        """Sjekk om element er sett."""
        cursor = self.conn.execute(
            "SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,)
        )
        return cursor.fetchone() is not None

    def mark_seen(self, signal: Signal):
        """Marker element som sett."""
        now = datetime.utcnow().isoformat()
        self.conn.execute("""
            INSERT OR REPLACE INTO seen_items
            (item_id, source, title, first_seen, last_checked)
            VALUES (?, ?, ?, ?, ?)
        """, (signal.signal_id, signal.source, signal.title[:500], now, now))

        self.conn.execute("""
            INSERT OR IGNORE INTO signals
            (signal_id, source, title, url, signal_type, score, priority, keywords, categories)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal.signal_id, signal.source, signal.title, signal.url,
            signal.signal_type, signal.score, signal.priority,
            ",".join(signal.matched_keywords), ",".join(signal.categories)
        ))
        self.conn.commit()

    def save_radar_hit(self, law_name: str, url: str, change_percent: float):
        """Lagrer radar-treff."""
        self.conn.execute(
            "INSERT INTO radar_hits (law_name, url, change_percent) VALUES (?, ?, ?)",
            (law_name, url, change_percent)
        )
        self.conn.commit()

    def cleanup_old(self, days: int = MAX_AGE_DAYS):
        """Fjern gamle poster."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        cursor = self.conn.execute(
            "DELETE FROM seen_items WHERE last_checked < ?", (cutoff,)
        )
        logger.info(f"Database cleanup: {cursor.rowcount} gamle poster fjernet")
        self.conn.commit()

    def close(self):
        """Lukk tilkobling."""
        self.conn.close()


# =============================================================================
# RELEVANSANALYSE
# =============================================================================

class Analyzer:
    """Analyserer tekst mot nokkelord-database."""

    CRITICAL_THRESHOLD = 8.0
    HIGH_THRESHOLD = 5.0
    MINIMUM_THRESHOLD = 3.0

    def __init__(self):
        self.index = KEYWORD_INDEX

    def analyze(self, text: str, context: str = "") -> Dict[str, Any]:
        """Analyser tekst for relevans."""
        if not text:
            return self._empty_result()

        combined = f"{text} {context}".lower()
        matches = []

        for term, keyword in self.index.items():
            if term in combined:
                matches.append(keyword)

        # Scoring
        base_score = sum(kw.weight for kw in matches)
        categories = set(kw.category for kw in matches)
        category_bonus = len(categories) * 0.3

        # Boost for kjernevirksomhet
        core_matches = [kw for kw in matches if kw.category == "core"]
        core_bonus = len(core_matches) * 0.5

        score = base_score + category_bonus + core_bonus

        # Prioritet
        has_deadline = any(kw.category == "deadline" for kw in matches)

        if score >= self.CRITICAL_THRESHOLD or (has_deadline and score >= 6):
            priority = 1
        elif score >= self.HIGH_THRESHOLD or has_deadline:
            priority = 2
        elif score >= self.MINIMUM_THRESHOLD:
            priority = 3
        else:
            priority = 4  # Ikke relevant

        # Deadline-ekstraksjon
        deadline = self._extract_deadline(combined)

        return {
            "is_relevant": score >= self.MINIMUM_THRESHOLD,
            "score": round(score, 1),
            "priority": priority,
            "matched_keywords": [kw.term for kw in matches],
            "categories": sorted(categories),
            "deadline": deadline,
        }

    def _extract_deadline(self, text: str) -> Optional[str]:
        """Ekstraher frist fra tekst."""
        patterns = [
            r'(?:frist|horingsfrist)[:\s]+(\d{1,2})[.\s]+(\w+)\s+(\d{4})',
            r'(?:trer i kraft|ikrafttredelse)[:\s]+(\d{1,2})[.\s]+(\w+)\s+(\d{4})',
            r'innen\s+(\d{1,2})[.\s]+(\w+)\s+(\d{4})',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0)
        return None

    def _empty_result(self) -> Dict:
        return {
            "is_relevant": False,
            "score": 0.0,
            "priority": 4,
            "matched_keywords": [],
            "categories": [],
            "deadline": None,
        }


# =============================================================================
# DATAKILDER
# =============================================================================

class StortingetSource:
    """Henter saker fra Stortingets API."""

    API_BASE = "https://data.stortinget.no/eksport"
    WEB_BASE = "https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/"

    def __init__(self, session: requests.Session, analyzer: Analyzer, db: Database):
        self.session = session
        self.analyzer = analyzer
        self.db = db

    def _get_sessions(self) -> List[str]:
        """Beregn aktive sesjoner."""
        now = datetime.now()
        year = now.year
        if now.month >= 10:
            return [f"{year}-{year+1}"]
        return [f"{year-1}-{year}"]

    def fetch(self) -> List[Signal]:
        """Hent saker."""
        signals = []

        for session_id in self._get_sessions():
            for endpoint in ["saker", "horinger"]:
                url = f"{self.API_BASE}/{endpoint}?sesjonid={session_id}"

                try:
                    resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                    resp.raise_for_status()

                    signals.extend(self._parse_xml(resp.content, session_id))

                except Exception as e:
                    logger.error(f"Stortinget {endpoint} feil: {e}")

        logger.info(f"Stortinget: {len(signals)} nye signaler")
        return signals

    def _parse_xml(self, content: bytes, session_id: str) -> List[Signal]:
        """Parser Stortinget XML."""
        signals = []

        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            logger.error(f"Stortinget XML parse feil: {e}")
            return []

        ns = {"s": "http://data.stortinget.no"}

        for sak in root.findall(".//s:sak", ns) + root.findall(".//s:horing", ns):
            sak_id = self._get_text(sak, "s:id", ns)
            tittel = self._get_text(sak, "s:tittel", ns) or self._get_text(sak, "s:kort_tittel", ns)

            if not sak_id or not tittel:
                continue

            signal_id = f"stortinget:{sak_id}"

            if self.db.is_seen(signal_id):
                continue

            komite = self._get_text(sak, ".//s:komite/s:navn", ns, "")
            analysis = self.analyzer.analyze(tittel, komite)

            if not analysis["is_relevant"]:
                continue

            signal = Signal(
                source=f"Stortinget ({session_id})",
                signal_id=signal_id,
                title=tittel,
                url=f"{self.WEB_BASE}?p={sak_id}",
                signal_type="sonar",
                score=analysis["score"],
                priority=analysis["priority"],
                matched_keywords=analysis["matched_keywords"],
                categories=analysis["categories"],
                deadline=analysis["deadline"],
            )

            self.db.mark_seen(signal)
            signals.append(signal)

        return signals

    def _get_text(self, elem: ET.Element, path: str, ns: Dict, default: str = "") -> str:
        """Hent tekst fra XML-element."""
        found = elem.find(path, ns)
        return found.text.strip() if found is not None and found.text else default


class RegjeringenSource:
    """Henter horinger fra Regjeringen.no."""

    SOURCES = {
        "Klima og miljo": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?ownerid=668",
        "Naering": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?ownerid=709",
        "Forbruker": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?ownerid=298",
        "Kommunal": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?ownerid=543",
    }

    SELECTORS = [
        "a[href*='/hoeringer/']",
        "a[href*='/horinger/']",
        "[class*='LI'] a",
    ]

    def __init__(self, session: requests.Session, analyzer: Analyzer, db: Database):
        self.session = session
        self.analyzer = analyzer
        self.db = db

    def fetch(self) -> List[Signal]:
        """Hent horinger."""
        signals = []

        for name, url in self.SOURCES.items():
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()

                soup = BeautifulSoup(resp.content, "html.parser")

                # Prov selektorer
                links = []
                for selector in self.SELECTORS:
                    try:
                        links = soup.select(selector)[:15]
                        if links:
                            break
                    except Exception:
                        continue  # Ugyldig selektor, prov neste

                for link in links:
                    title = link.get_text(strip=True)
                    href = link.get("href", "")

                    if not title or len(title) < 10:
                        continue

                    if not href.startswith("http"):
                        href = "https://www.regjeringen.no" + href

                    signal_id = f"regjeringen:{hashlib.sha256(href.encode()).hexdigest()[:12]}"

                    if self.db.is_seen(signal_id):
                        continue

                    analysis = self.analyzer.analyze(title)

                    if not analysis["is_relevant"]:
                        continue

                    signal = Signal(
                        source=f"Regjeringen ({name})",
                        signal_id=signal_id,
                        title=title,
                        url=href,
                        signal_type="sonar",
                        score=analysis["score"],
                        priority=analysis["priority"],
                        matched_keywords=analysis["matched_keywords"],
                        categories=analysis["categories"],
                        deadline=analysis["deadline"],
                    )

                    self.db.mark_seen(signal)
                    signals.append(signal)

            except Exception as e:
                logger.error(f"Regjeringen {name} feil: {e}")

        logger.info(f"Regjeringen: {len(signals)} nye signaler")
        return signals


class ForbrukertilsynetSource:
    """Henter nyheter fra Forbrukertilsynet RSS."""

    URL = "https://www.forbrukertilsynet.no/feed"

    def __init__(self, session: requests.Session, analyzer: Analyzer, db: Database):
        self.session = session
        self.analyzer = analyzer
        self.db = db

    def fetch(self) -> List[Signal]:
        """Hent RSS-feed."""
        signals = []

        try:
            resp = self.session.get(self.URL, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()

            root = ET.fromstring(resp.content)

            for item in root.findall(".//item")[:20]:
                title = self._get_text(item, "title")
                link = self._get_text(item, "link")
                description = self._get_text(item, "description")

                if not title or not link:
                    continue

                signal_id = f"forbrukertilsynet:{hashlib.sha256(link.encode()).hexdigest()[:12]}"

                if self.db.is_seen(signal_id):
                    continue

                # Fjern HTML fra description
                if description:
                    description = BeautifulSoup(description, "html.parser").get_text()

                analysis = self.analyzer.analyze(title, description or "")

                if not analysis["is_relevant"]:
                    continue

                signal = Signal(
                    source="Forbrukertilsynet",
                    signal_id=signal_id,
                    title=title,
                    url=link,
                    signal_type="sonar",
                    score=analysis["score"],
                    priority=analysis["priority"],
                    matched_keywords=analysis["matched_keywords"],
                    categories=analysis["categories"],
                    deadline=analysis["deadline"],
                )

                self.db.mark_seen(signal)
                signals.append(signal)

        except Exception as e:
            logger.error(f"Forbrukertilsynet feil: {e}")

        logger.info(f"Forbrukertilsynet: {len(signals)} nye signaler")
        return signals

    def _get_text(self, elem: ET.Element, tag: str) -> str:
        """Hent tekst fra element."""
        found = elem.find(tag)
        return found.text.strip() if found is not None and found.text else ""


class LovdataRadar:
    """Overvaker endringer i lover og forskrifter."""

    LAWS = {
        "Apenhetsloven": "https://lovdata.no/dokument/NL/lov/2021-06-18-99",
        "Markedsforingsloven": "https://lovdata.no/dokument/NL/lov/2009-01-09-2",
        "Produktkontrolloven": "https://lovdata.no/dokument/NL/lov/1976-06-11-79",
        "Forbrukerkjopsloven": "https://lovdata.no/dokument/NL/lov/2002-06-21-34",
    }

    REGULATIONS = {
        "TEK17": "https://lovdata.no/dokument/SF/forskrift/2017-06-19-840",
        "Byggevareforskriften": "https://lovdata.no/dokument/SF/forskrift/2013-12-17-1579",
        "Avfallsforskriften": "https://lovdata.no/dokument/SF/forskrift/2004-06-01-930",
        "REACH-forskriften": "https://lovdata.no/dokument/SF/forskrift/2008-05-30-516",
        "Produktforskriften": "https://lovdata.no/dokument/SF/forskrift/2004-06-01-922",
    }

    def __init__(self, session: requests.Session, db: Database):
        self.session = session
        self.db = db
        self.cache = self._load_cache()

    def _load_cache(self) -> Dict:
        """Last inn cache."""
        if Path(CACHE_FILE).exists():
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    def _save_cache(self):
        """Lagre cache."""
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)

    def fetch(self) -> List[Signal]:
        """Sjekk for endringer."""
        signals = []
        all_docs = {**self.LAWS, **self.REGULATIONS}

        for name, url in all_docs.items():
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()

                soup = BeautifulSoup(resp.content, "html.parser")

                # Fjern stoy
                for elem in soup(["script", "style", "nav", "footer", "header"]):
                    elem.decompose()

                text = re.sub(r'\s+', ' ', soup.get_text()).strip()
                new_hash = hashlib.sha256(text.encode()).hexdigest()

                prev = self.cache.get(name, {})

                if prev and new_hash != prev.get("hash"):
                    # Beregn endringsprosent
                    prev_text = prev.get("text", "")[:5000]
                    curr_text = text[:5000]
                    similarity = SequenceMatcher(None, prev_text, curr_text).ratio()
                    change_pct = round((1 - similarity) * 100, 2)

                    if change_pct >= CHANGE_THRESHOLD:
                        signal = Signal(
                            source="Lovdata Radar",
                            signal_id=f"radar:{hashlib.sha256(url.encode()).hexdigest()[:12]}:{datetime.now().strftime('%Y%m%d')}",
                            title=f"Endring detektert: {name}",
                            url=url,
                            signal_type="radar",
                            score=change_pct,
                            priority=1 if change_pct > 5 else 2,
                            change_percent=change_pct,
                        )

                        self.db.save_radar_hit(name, url, change_pct)
                        signals.append(signal)

                        logger.info(f"Radar: {name} endret {change_pct}%")

                # Oppdater cache
                self.cache[name] = {
                    "hash": new_hash,
                    "text": text[:5000],
                    "checked": datetime.now().isoformat(),
                }

            except Exception as e:
                logger.error(f"Radar {name} feil: {e}")

        self._save_cache()
        logger.info(f"Radar: {len(signals)} endringer detektert")
        return signals


# =============================================================================
# KORRELASJON
# =============================================================================

def correlate_signals(radar: List[Signal], sonar: List[Signal]) -> List[Correlation]:
    """Kobler radar og sonar signaler."""
    correlations = []

    keyword_map = {
        "TEK17": {"tek17", "tek", "byggteknisk", "byggevare"},
        "Byggevareforskriften": {"dok", "ce-merking", "ytelseserklering", "byggevare"},
        "REACH-forskriften": {"reach", "svhc", "kjemikalier", "farlige stoffer", "pfas"},
        "Markedsforingsloven": {"markedsforing", "gronnvasking", "villedende", "miljopastand"},
        "Apenhetsloven": {"apenhet", "aktsomhet", "menneskerettigheter"},
    }

    for r in radar:
        law_name = r.title.replace("Endring detektert: ", "")
        law_keywords = keyword_map.get(law_name, set())

        for s in sonar:
            sonar_keywords = set(kw.lower() for kw in s.matched_keywords)
            overlap = law_keywords & sonar_keywords

            if overlap:
                correlations.append(Correlation(
                    radar_law=law_name,
                    radar_change=r.change_percent or 0,
                    sonar_title=s.title,
                    sonar_source=s.source,
                    connection_keywords=list(overlap),
                    action=f"Undersok om endring i {law_name} henger sammen med: {s.title[:50]}..."
                ))

    return correlations


# =============================================================================
# RAPPORT - BRUKERVENNLIG
# =============================================================================

def generate_report(
    sonar: List[Signal],
    radar: List[Signal],
    correlations: List[Correlation]
) -> str:
    """Genererer brukervennlig ukentlig rapport."""

    now = datetime.now()

    # Sorter etter prioritet
    sonar.sort(key=lambda s: (s.priority, -s.score))

    lines = []

    # === HEADER ===
    lines.append("")
    lines.append("=" * 70)
    lines.append("   LOVSONAR UKENTLIG RAPPORT - OBS BYGG")
    lines.append("=" * 70)
    lines.append(f"   Uke {now.isocalendar()[1]}, {now.year}")
    lines.append(f"   Generert: {now.strftime('%A %d. %B %Y kl. %H:%M')}")
    lines.append("=" * 70)
    lines.append("")

    # === OPPSUMMERING ===
    lines.append("OPPSUMMERING")
    lines.append("-" * 40)

    critical = [s for s in sonar if s.priority == 1]
    important = [s for s in sonar if s.priority == 2]
    info = [s for s in sonar if s.priority == 3]

    lines.append(f"   Totalt signaler:     {len(sonar)}")
    lines.append(f"   Lovendringer:        {len(radar)}")
    lines.append(f"   Korrelasjoner:       {len(correlations)}")
    lines.append("")
    lines.append(f"   KRITISK (handling):  {len(critical)}")
    lines.append(f"   VIKTIG (planlegg):   {len(important)}")
    lines.append(f"   INFO (folg med):     {len(info)}")
    lines.append("")

    # === LOVENDRINGER (RADAR) ===
    if radar:
        lines.append("")
        lines.append("=" * 70)
        lines.append("   LOVENDRINGER DETEKTERT")
        lines.append("=" * 70)
        lines.append("")

        for r in radar:
            law_name = r.title.replace("Endring detektert: ", "")
            lines.append(f"   {law_name}")
            lines.append(f"   Endring: {r.change_percent}%")
            lines.append(f"   {r.url}")
            lines.append(f"   -> Sjekk hva som er endret")
            lines.append("")

    # === KORRELASJONER ===
    if correlations:
        lines.append("")
        lines.append("=" * 70)
        lines.append("   KOBLINGER FUNNET (Radar + Sonar)")
        lines.append("=" * 70)
        lines.append("")

        for c in correlations:
            lines.append(f"   Lov: {c.radar_law} ({c.radar_change}% endret)")
            lines.append(f"   Signal: {c.sonar_title[:55]}...")
            lines.append(f"   Kobling: {', '.join(c.connection_keywords)}")
            lines.append(f"   -> {c.action}")
            lines.append("")

    # === KRITISKE SIGNALER ===
    if critical:
        lines.append("")
        lines.append("=" * 70)
        lines.append("   KRITISK - KREVER HANDLING")
        lines.append("=" * 70)
        lines.append("")

        for s in critical:
            lines.append(f"   {s.title}")
            lines.append(f"   Kilde: {s.source}")
            lines.append(f"   Score: {s.score} | {', '.join(s.categories[:3])}")
            if s.deadline:
                lines.append(f"   FRIST: {s.deadline}")
            lines.append(f"   Nokkelord: {', '.join(s.matched_keywords[:5])}")
            lines.append(f"   {s.url}")
            lines.append("")

    # === VIKTIGE SIGNALER ===
    if important:
        lines.append("")
        lines.append("=" * 70)
        lines.append("   VIKTIG - PLANLEGG RESPONS")
        lines.append("=" * 70)
        lines.append("")

        for s in important[:10]:
            lines.append(f"   {s.title[:60]}...")
            lines.append(f"   {s.source} | Score: {s.score}")
            lines.append(f"   {s.url}")
            lines.append("")

    # === INFO SIGNALER (kort) ===
    if info:
        lines.append("")
        lines.append("=" * 70)
        lines.append("   INFO - FOLG MED")
        lines.append("=" * 70)
        lines.append("")

        for s in info[:5]:
            lines.append(f"   - {s.title[:55]}... ({s.source})")
        lines.append("")

    # === INGEN SIGNALER ===
    if not sonar and not radar:
        lines.append("")
        lines.append("   Ingen nye signaler denne uken.")
        lines.append("   Neste rapport: mandag kl. 07:00")
        lines.append("")

    # === FOOTER ===
    lines.append("")
    lines.append("=" * 70)
    lines.append("   SLUTT PA RAPPORT")
    lines.append(f"   LovSonar v{VERSION} | github.com/Majac999/Lovsonar")
    lines.append("=" * 70)
    lines.append("")

    return "\n".join(lines)


def send_email(report: str, signal_count: int, radar_count: int) -> bool:
    """Sender rapport via e-post."""
    user = os.getenv("EMAIL_USER", "").strip()
    password = os.getenv("EMAIL_PASS", "").strip()
    recipient = os.getenv("EMAIL_RECIPIENT", "").strip()

    if not all([user, password, recipient]):
        logger.warning("E-post ikke konfigurert")
        return False

    if signal_count == 0 and radar_count == 0:
        logger.info("Ingen signaler - sender ikke e-post")
        return False

    now = datetime.now()

    msg = MIMEMultipart()
    msg["Subject"] = f"LovSonar Uke {now.isocalendar()[1]}: {signal_count} signaler, {radar_count} lovendringer"
    msg["From"] = user
    msg["To"] = recipient

    msg.attach(MIMEText(report, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(user, password)
            server.send_message(msg)

        logger.info(f"Rapport sendt til {recipient}")
        return True

    except Exception as e:
        logger.error(f"E-post feil: {e}")
        return False


# =============================================================================
# HOVEDPROGRAM
# =============================================================================

def main():
    """Hovedfunksjon."""
    logger.info("=" * 60)
    logger.info(f"LovSonar v{VERSION} starter")
    logger.info("=" * 60)

    # Initialiser
    session = create_session()
    db = Database()
    analyzer = Analyzer()

    all_sonar: List[Signal] = []
    all_radar: List[Signal] = []

    try:
        # 1. Stortinget
        stortinget = StortingetSource(session, analyzer, db)
        all_sonar.extend(stortinget.fetch())

        # 2. Regjeringen
        regjeringen = RegjeringenSource(session, analyzer, db)
        all_sonar.extend(regjeringen.fetch())

        # 3. Forbrukertilsynet
        forbrukertilsynet = ForbrukertilsynetSource(session, analyzer, db)
        all_sonar.extend(forbrukertilsynet.fetch())

        # 4. Lovdata Radar
        radar = LovdataRadar(session, db)
        all_radar.extend(radar.fetch())

        # 5. Korreler
        correlations = correlate_signals(all_radar, all_sonar)

        # 6. Generer rapport
        report = generate_report(all_sonar, all_radar, correlations)
        print(report)

        # 7. Lagre rapport
        report_path = Path("lovsonar_rapport.txt")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"Rapport lagret: {report_path}")

        # 8. Send e-post
        send_email(report, len(all_sonar), len(all_radar))

        # 9. Cleanup
        db.cleanup_old()

        logger.info("=" * 60)
        logger.info(f"Ferdig: {len(all_sonar)} signaler, {len(all_radar)} lovendringer")
        logger.info("=" * 60)

    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())

