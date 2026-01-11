#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LovSonar v3.0 - Strategisk Fremtidsovervåking for Varehandel
=============================================================
Formål: Tidlig varsling av politiske forslag, EU-direktiver og regulatoriske
trender som påvirker bærekraft og compliance i varehandelen.

Strategiske hypoteser:
- Kostnadskontroll: Identifisere kommende avgifter/krav tidlig
- Nivellering: Innsikt i når krav tvinger frem lik standard for alle
- EMV-innsikt: Analyse av hvordan Private Labels påvirkes av EU-krav

Datakilder:
- Stortinget API (data.stortinget.no) - Lovforslag, representantforslag
- Forbrukertilsynet RSS - Markedsføringskrav, grønnvasking
- Miljødirektoratet RSS - Miljøreguleringer
- Lovdata HTML-scraping - Lovendringer (med caching)
"""

import os
import json
import hashlib
import smtplib
import re
import logging
import sqlite3
import feedparser
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup
from difflib import SequenceMatcher
from dataclasses import dataclass
from enum import Enum

# =============================================================================
# KONFIGURASJON
# =============================================================================

USER_AGENT = "LovSonar/3.0 (Strategisk Pilot for Varehandel)"
DB_PATH = os.getenv("LOVSONAR_DB", "lovsonar_v3.db")
CACHE_FILE = os.getenv("LOVSONAR_CACHE", "lovsonar_cache.json")
MAX_AGE_DAYS = int(os.getenv("LOVSONAR_MAX_AGE_DAYS", "180"))
CHANGE_THRESHOLD = 0.5  # Minimum % endring for varsling

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("LovSonar")

# =============================================================================
# PRIORITET & NØKKELORD
# =============================================================================

class Priority(Enum):
    CRITICAL = 1  # Umiddelbar handling kreves
    HIGH = 2      # Planlegg respons
    MEDIUM = 3    # Følg med
    LOW = 4       # Informasjon

@dataclass
class Keyword:
    term: str
    weight: float
    category: str
    description: str = ""

# Segmentnøkkelord - identifiserer om saken gjelder varehandel/bygg
KEYWORDS_SEGMENT = [
    Keyword("byggevare", 2.0, "core", "Kjernevirksomhet"),
    Keyword("byggevarehus", 2.0, "core", "Kjernevirksomhet"),
    Keyword("trelast", 1.5, "core", "Hovedsortiment"),
    Keyword("jernvare", 1.5, "core", "Hovedsortiment"),
    Keyword("detaljhandel", 1.0, "retail", "Bransje"),
    Keyword("varehandel", 1.0, "retail", "Bransje"),
    Keyword("dagligvare", 0.8, "retail", "Relatert"),
    Keyword("forbruker", 0.8, "retail", "Målgruppe"),
]

# Temanøkkelord - EU-regulering og bærekraft
KEYWORDS_EU_SUSTAINABILITY = [
    # EU Green Deal - Kjernereguleringer
    Keyword("espr", 3.0, "eu_core", "Ecodesign for Sustainable Products Regulation"),
    Keyword("ecodesign", 2.5, "eu_core", "Økodesign-krav"),
    Keyword("digitalt produktpass", 3.0, "digital", "Digital Product Passport (DPP)"),
    Keyword("dpp", 2.5, "digital", "Digital Product Passport"),
    Keyword("produktpass", 2.5, "digital", "Produktdokumentasjon"),

    # Emballasje og avfall
    Keyword("ppwr", 2.5, "packaging", "Packaging and Packaging Waste Regulation"),
    Keyword("emballasje", 2.0, "packaging", "Emballasjekrav"),
    Keyword("engangsplast", 2.0, "packaging", "SUP-direktiv"),
    Keyword("produsentansvar", 2.0, "packaging", "EPR - Extended Producer Responsibility"),

    # Skog og avskoging
    Keyword("eudr", 2.5, "deforestation", "EU Deforestation Regulation"),
    Keyword("avskoging", 2.0, "deforestation", "Avskogingsfri"),
    Keyword("sporbarhet", 2.0, "deforestation", "Verdikjede-sporbarhet"),

    # Miljødeklarasjoner
    Keyword("miljødeklarasjon", 2.0, "sustainability", "EPD"),
    Keyword("epd", 2.0, "sustainability", "Environmental Product Declaration"),
    Keyword("klimaavtrykk", 1.5, "sustainability", "Carbon footprint"),
    Keyword("livsløpsanalyse", 1.5, "sustainability", "LCA"),
    Keyword("bærekraft", 1.5, "sustainability", "Generell bærekraft"),
    Keyword("sirkulær", 1.5, "sustainability", "Sirkulærøkonomi"),
    Keyword("ombruk", 1.5, "sustainability", "Gjenbruk"),

    # Kjemikalier
    Keyword("reach", 2.0, "chemicals", "REACH-forordningen"),
    Keyword("pfas", 2.5, "chemicals", "PFAS-forbud"),
    Keyword("farlige stoffer", 2.0, "chemicals", "Kjemikalieregulering"),
    Keyword("biocid", 1.5, "chemicals", "Biocidforordningen"),
]

# Norske reguleringer og compliance
KEYWORDS_NORWEGIAN = [
    Keyword("åpenhetsloven", 2.5, "compliance", "Aktsomhetsvurderinger"),
    Keyword("aktsomhet", 2.0, "compliance", "Due diligence"),
    Keyword("menneskerettigheter", 1.5, "compliance", "Menneskerettigheter i verdikjeden"),
    Keyword("grønnvasking", 2.5, "marketing", "Villedende miljøpåstander"),
    Keyword("miljøpåstand", 2.0, "marketing", "Green claims"),
    Keyword("markedsføringsloven", 1.5, "marketing", "Markedsføringsregler"),
    Keyword("forbrukertilsynet", 1.5, "marketing", "Tilsyn"),
    Keyword("tek17", 2.0, "building", "Byggeforskrift"),
    Keyword("byggteknisk", 1.5, "building", "Byggeregelverk"),
    Keyword("dok-forskriften", 2.0, "building", "Dokumentasjon av byggevarer"),
]

# Kritiske hendelser (frister, ikrafttredelse)
KEYWORDS_CRITICAL = [
    Keyword("høringsfrist", 3.0, "deadline", "Frist for innspill"),
    Keyword("høringsnotat", 2.0, "deadline", "Høringsdokument"),
    Keyword("ikrafttredelse", 2.5, "deadline", "Lov trer i kraft"),
    Keyword("trer i kraft", 2.5, "deadline", "Tidspunkt for virkning"),
    Keyword("forbud", 2.5, "legal", "Forbud mot stoff/praksis"),
    Keyword("påbud", 2.0, "legal", "Nytt krav"),
    Keyword("overtredelsesgebyr", 2.0, "legal", "Sanksjoner"),
]

ALL_KEYWORDS = KEYWORDS_SEGMENT + KEYWORDS_EU_SUSTAINABILITY + KEYWORDS_NORWEGIAN + KEYWORDS_CRITICAL

# =============================================================================
# DATAKILDER
# =============================================================================

# RSS-feeds som faktisk fungerer
RSS_SOURCES = {
    "⚖️ Forbrukertilsynet": {
        "url": "https://www.forbrukertilsynet.no/feed",
        "type": "consumer",
        "description": "Grønnvasking, markedsføring, forbrukerrettigheter"
    },
    "🏗️ DiBK Nyheter": {
        "url": "https://dibk.no/nyheter/rss/",
        "type": "building",
        "description": "Byggteknisk forskrift, TEK17, byggevarer"
    },
}

# Stortinget API - strukturerte data
STORTINGET_API = {
    "saker": "https://data.stortinget.no/eksport/saker?sesjonid={sesjon}",
    "horinger": "https://data.stortinget.no/eksport/horinger?sesjonid={sesjon}",
}

# Lover å overvåke for endringer (HTML-scraping)
LAWS_TO_MONITOR = {
    "Åpenhetsloven": "https://lovdata.no/dokument/NL/lov/2021-06-18-99",
    "Produktkontrolloven": "https://lovdata.no/dokument/NL/lov/1976-06-11-79",
    "Markedsføringsloven": "https://lovdata.no/dokument/NL/lov/2009-01-09-2",
    "Forbrukerkjøpsloven": "https://lovdata.no/dokument/NL/lov/2002-06-21-34",
}

# Forskrifter å overvåke
REGULATIONS_TO_MONITOR = {
    "Byggevareforskriften (DOK)": "https://lovdata.no/dokument/SF/forskrift/2014-12-17-1714",
    "Avfallsforskriften": "https://lovdata.no/dokument/SF/forskrift/2004-06-01-930",
    "Produktforskriften": "https://lovdata.no/dokument/SF/forskrift/2004-06-01-922",
}

# Relevante stortingskomiteer
RELEVANT_COMMITTEES = [
    "næringskomiteen",
    "energi- og miljøkomiteen",
    "finanskomiteen",
    "familie- og kulturkomiteen",  # Forbrukersaker
]

# =============================================================================
# DATABASE
# =============================================================================

def setup_db() -> sqlite3.Connection:
    """Opprett database for historikk og compliance-dokumentasjon."""
    conn = sqlite3.connect(DB_PATH)

    # Sett items (deduplisering)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_items (
            item_id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            date_seen TEXT
        )
    """)

    # Sonar-treff (RSS/API-funn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sonar_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            title TEXT,
            link TEXT,
            priority INTEGER,
            score REAL,
            deadline TEXT,
            matched_keywords TEXT,
            category TEXT,
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Radar-treff (lovendringer)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS radar_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            law_name TEXT,
            url TEXT,
            change_percent REAL,
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    return conn

# =============================================================================
# ANALYSE
# =============================================================================

def extract_deadline(text: str) -> Optional[str]:
    """Ekstraher høringsfrist fra tekst."""
    patterns = [
        r'(?:høringsfrist|frist)[:\s]+(\d{1,2})[.\s]+([a-zA-ZæøåÆØÅ]+)\s+(\d{4})',
        r'(?:frist|deadline)[:\s]+(\d{1,2})\.(\d{1,2})\.(\d{4})',
        r'innen\s+(\d{1,2})[.\s]+([a-zA-ZæøåÆØÅ]+)\s+(\d{4})',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0)
    return None

def analyze_relevance(text: str, source_type: str = "") -> Dict[str, Any]:
    """Analyser tekst for relevans til varehandel/bærekraft."""
    t = text.lower()

    # Finn matchende nøkkelord
    segment_matches = [(kw.term, kw.weight, kw.category) for kw in KEYWORDS_SEGMENT if kw.term.lower() in t]
    topic_matches = [(kw.term, kw.weight, kw.category) for kw in KEYWORDS_EU_SUSTAINABILITY if kw.term.lower() in t]
    norwegian_matches = [(kw.term, kw.weight, kw.category) for kw in KEYWORDS_NORWEGIAN if kw.term.lower() in t]
    critical_matches = [(kw.term, kw.weight, kw.category) for kw in KEYWORDS_CRITICAL if kw.term.lower() in t]

    all_matches = segment_matches + topic_matches + norwegian_matches + critical_matches

    # Beregn score
    segment_score = sum(w for _, w, _ in segment_matches) * 1.5  # Vektet høyere
    topic_score = sum(w for _, w, _ in topic_matches)
    norwegian_score = sum(w for _, w, _ in norwegian_matches)
    critical_score = sum(w for _, w, _ in critical_matches)

    total_score = segment_score + topic_score + norwegian_score + critical_score

    # Bestem relevans
    has_high_priority_topic = any(w >= 2.5 for _, w, _ in topic_matches + norwegian_matches)
    is_relevant = total_score >= 3.0 or has_high_priority_topic

    # Bestem prioritet
    priority = Priority.LOW
    if is_relevant:
        deadline = extract_deadline(text)
        has_deadline = deadline is not None or critical_score > 0

        if has_deadline and total_score > 8:
            priority = Priority.CRITICAL
        elif total_score > 6 or has_deadline:
            priority = Priority.HIGH
        elif total_score > 4:
            priority = Priority.MEDIUM
        else:
            priority = Priority.LOW

    # Kategoriser hovedtema
    categories = set(cat for _, _, cat in all_matches)
    main_category = "general"
    if "eu_core" in categories or "digital" in categories:
        main_category = "EU Green Deal"
    elif "packaging" in categories or "deforestation" in categories:
        main_category = "EU Verdikjede"
    elif "chemicals" in categories or "sustainability" in categories:
        main_category = "Miljø/Kjemikalier"
    elif "compliance" in categories or "marketing" in categories:
        main_category = "Compliance/Markedsføring"
    elif "building" in categories:
        main_category = "Byggeregelverk"

    return {
        "is_relevant": is_relevant,
        "score": round(total_score, 1),
        "priority": priority,
        "matched_keywords": [term for term, _, _ in all_matches],
        "categories": list(categories),
        "main_category": main_category,
        "deadline": extract_deadline(text),
    }

# =============================================================================
# STORTINGET API
# =============================================================================

def get_current_session() -> str:
    """Hent gjeldende stortingssesjon."""
    now = datetime.now()
    year = now.year
    if now.month >= 10:
        return f"{year}-{year + 1}"
    return f"{year - 1}-{year}"

def fetch_stortinget_data(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Hent saker fra Stortingets API."""
    logger.info("🏛️ Henter data fra Stortinget...")
    hits = []
    session = get_current_session()

    for endpoint_name, url_template in STORTINGET_API.items():
        url = url_template.format(sesjon=session)

        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            response.raise_for_status()

            root = ET.fromstring(response.content)
            ns = {"s": "http://data.stortinget.no"}

            # Parse saker eller høringer
            items = root.findall(".//s:sak", ns) or root.findall(".//s:horing", ns)

            for item in items:
                title = item.findtext("s:tittel", "", ns) or item.findtext("s:kort_tittel", "", ns)
                item_id = item.findtext("s:id", "", ns)
                komite = item.findtext("s:komite/s:navn", "", ns) or ""

                # Generer unik ID
                hash_id = hashlib.sha256(f"{item_id}{title}".encode()).hexdigest()[:16]

                # Sjekk om allerede sett
                if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (hash_id,)).fetchone():
                    continue

                # Analyser relevans
                full_text = f"{title} {komite}"
                analysis = analyze_relevance(full_text, "stortinget")

                # Lagre som sett
                conn.execute(
                    "INSERT INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
                    (hash_id, "Stortinget", title, datetime.now().isoformat())
                )

                if analysis["is_relevant"]:
                    link = f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={item_id}"

                    hit = {
                        "source": "🏛️ Stortinget",
                        "title": title,
                        "link": link,
                        "priority": analysis["priority"],
                        "score": analysis["score"],
                        "matched_keywords": analysis["matched_keywords"],
                        "category": analysis["main_category"],
                        "deadline": analysis["deadline"],
                        "committee": komite,
                    }
                    hits.append(hit)

                    conn.execute(
                        """INSERT INTO sonar_hits
                           (source, title, link, priority, score, deadline, matched_keywords, category)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        ("Stortinget", title, link, analysis["priority"].value,
                         analysis["score"], analysis["deadline"],
                         ",".join(analysis["matched_keywords"]), analysis["main_category"])
                    )

                    logger.info(f"  [{analysis['priority'].name}] {title[:60]}...")

            logger.info(f"  Prosessert {len(items)} elementer fra {endpoint_name}")

        except Exception as e:
            logger.error(f"Feil ved henting fra Stortinget ({endpoint_name}): {e}")

    return hits

# =============================================================================
# RSS-FEEDS
# =============================================================================

def fetch_rss_feeds(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Hent og analyser RSS-feeds."""
    logger.info("📡 Sjekker RSS-feeds...")
    hits = []

    for name, config in RSS_SOURCES.items():
        try:
            response = requests.get(
                config["url"],
                headers={"User-Agent": USER_AGENT},
                timeout=30
            )
            response.raise_for_status()

            feed = feedparser.parse(response.content)

            for entry in feed.entries[:15]:  # Siste 15 innlegg
                link = getattr(entry, 'link', '')
                title = getattr(entry, 'title', '')
                summary = getattr(entry, 'summary', '')

                # Generer unik ID
                hash_id = hashlib.sha256(link.encode()).hexdigest()[:16]

                # Sjekk om allerede sett
                if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (hash_id,)).fetchone():
                    continue

                # Analyser relevans
                full_text = f"{title} {summary}"
                analysis = analyze_relevance(full_text, config["type"])

                # Lagre som sett
                conn.execute(
                    "INSERT INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
                    (hash_id, name, title, datetime.now().isoformat())
                )

                if analysis["is_relevant"]:
                    hit = {
                        "source": name,
                        "title": title,
                        "link": link,
                        "priority": analysis["priority"],
                        "score": analysis["score"],
                        "matched_keywords": analysis["matched_keywords"],
                        "category": analysis["main_category"],
                        "deadline": analysis["deadline"],
                    }
                    hits.append(hit)

                    conn.execute(
                        """INSERT INTO sonar_hits
                           (source, title, link, priority, score, deadline, matched_keywords, category)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (name, title, link, analysis["priority"].value,
                         analysis["score"], analysis["deadline"],
                         ",".join(analysis["matched_keywords"]), analysis["main_category"])
                    )

                    logger.info(f"  [{analysis['priority'].name}] {title[:60]}...")

            logger.info(f"  ✓ {name}: {len(feed.entries)} innlegg sjekket")

        except Exception as e:
            logger.error(f"  ✗ {name}: {e}")

    return hits

# =============================================================================
# LOVENDRING-OVERVÅKING (RADAR)
# =============================================================================

def check_law_changes(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Sjekk lover og forskrifter for endringer."""
    logger.info("📜 Sjekker lover og forskrifter for endringer...")
    hits = []

    # Last cache
    cache = {}
    if Path(CACHE_FILE).exists():
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)

    all_documents = {**LAWS_TO_MONITOR, **REGULATIONS_TO_MONITOR}

    for name, url in all_documents.items():
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")

            # Fjern skript og stil
            for element in soup(["script", "style", "nav", "footer"]):
                element.decompose()

            # Hent hovedinnhold
            text = re.sub(r'\s+', ' ', soup.get_text()).strip()
            new_hash = hashlib.sha256(text.encode()).hexdigest()

            prev = cache.get(name, {})

            if prev and new_hash != prev.get("hash"):
                # Beregn endringsprosent
                prev_text = prev.get("text", "")[:5000]
                curr_text = text[:5000]
                similarity = SequenceMatcher(None, prev_text, curr_text).ratio()
                change_percent = round((1 - similarity) * 100, 2)

                if change_percent >= CHANGE_THRESHOLD:
                    hit = {
                        "name": name,
                        "url": url,
                        "change_percent": change_percent,
                    }
                    hits.append(hit)

                    conn.execute(
                        "INSERT INTO radar_hits (law_name, url, change_percent) VALUES (?,?,?)",
                        (name, url, change_percent)
                    )

                    logger.info(f"  ⚠️ {name}: {change_percent}% endring detektert")

            # Oppdater cache
            cache[name] = {
                "hash": new_hash,
                "text": text[:5000],
                "checked": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"  ✗ {name}: {e}")

    # Lagre cache
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    logger.info(f"  Sjekket {len(all_documents)} dokumenter")
    return hits

# =============================================================================
# RAPPORTERING
# =============================================================================

def generate_report(sonar_hits: List[Dict], radar_hits: List[Dict]) -> str:
    """Generer tekstrapport for AI-analyse."""
    report = []
    report.append("# LOVSONAR STRATEGISK RAPPORT")
    report.append(f"Generert: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    report.append("")
    report.append("Formål: Tidlig varsling av regulatoriske trender for varehandel.")
    report.append("Fokus: EU Green Deal, bærekraft, compliance, grønnvasking.")
    report.append("=" * 70)
    report.append("")

    # Lovendringer (Radar)
    if radar_hits:
        report.append("## 📜 LOVENDRINGER DETEKTERT (Radar)")
        report.append("")
        for hit in radar_hits:
            report.append(f"### ⚠️ {hit['name']}")
            report.append(f"- **Endring:** {hit['change_percent']}%")
            report.append(f"- **Lenke:** {hit['url']}")
            report.append("")

    # Nye signaler (Sonar)
    if sonar_hits:
        # Grupper etter prioritet
        critical = [h for h in sonar_hits if h["priority"] == Priority.CRITICAL]
        high = [h for h in sonar_hits if h["priority"] == Priority.HIGH]
        medium = [h for h in sonar_hits if h["priority"] == Priority.MEDIUM]

        if critical:
            report.append("## 🚨 KRITISKE SIGNALER (Krever handling)")
            report.append("")
            for hit in sorted(critical, key=lambda x: x["score"], reverse=True):
                report.append(f"### {hit['title']}")
                report.append(f"- **Kilde:** {hit['source']}")
                report.append(f"- **Score:** {hit['score']} | Kategori: {hit['category']}")
                report.append(f"- **Nøkkelord:** {', '.join(hit['matched_keywords'])}")
                if hit.get("deadline"):
                    report.append(f"- **Frist:** {hit['deadline']}")
                report.append(f"- **Lenke:** {hit['link']}")
                report.append("")

        if high:
            report.append("## ⚡ HØY PRIORITET (Planlegg respons)")
            report.append("")
            for hit in sorted(high, key=lambda x: x["score"], reverse=True):
                report.append(f"### {hit['title']}")
                report.append(f"- **Kilde:** {hit['source']} | Score: {hit['score']}")
                report.append(f"- **Kategori:** {hit['category']}")
                report.append(f"- **Nøkkelord:** {', '.join(hit['matched_keywords'])}")
                report.append(f"- **Lenke:** {hit['link']}")
                report.append("")

        if medium:
            report.append("## 📋 MEDIUM PRIORITET (Følg med)")
            report.append("")
            for hit in sorted(medium, key=lambda x: x["score"], reverse=True)[:10]:
                report.append(f"- **{hit['title'][:70]}...** ({hit['source']}, Score: {hit['score']})")
            report.append("")

    if not sonar_hits and not radar_hits:
        report.append("Ingen nye relevante signaler i denne skanningen.")

    return "\n".join(report)

def send_email(report: str, sonar_count: int, radar_count: int) -> bool:
    """Send rapport via e-post."""
    user = os.environ.get("EMAIL_USER", "").strip()
    pw = os.environ.get("EMAIL_PASS", "").strip()
    recipient = os.environ.get("EMAIL_RECIPIENT", "").strip()

    if not all([user, pw, recipient]):
        logger.warning("E-post-konfigurasjon mangler. Hopper over sending.")
        return False

    if sonar_count == 0 and radar_count == 0:
        logger.info("Ingen funn - sender ikke e-post.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🛡️ LovSonar: {sonar_count} signaler, {radar_count} lovendringer"
    msg["From"] = user
    msg["To"] = recipient

    # Ren tekst for AI-analyse
    msg.attach(MIMEText(report, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(user, pw)
            server.send_message(msg)
        logger.info(f"📧 Rapport sendt til {recipient}")
        return True
    except Exception as e:
        logger.error(f"Feil ved sending av e-post: {e}")
        return False

# =============================================================================
# HOVEDPROGRAM
# =============================================================================

def main():
    """Hovedfunksjon."""
    logger.info("=" * 60)
    logger.info("LovSonar v3.0 - Strategisk Fremtidsovervåking")
    logger.info("=" * 60)

    conn = setup_db()

    try:
        # 1. Hent fra Stortinget API
        stortinget_hits = fetch_stortinget_data(conn)

        # 2. Hent fra RSS-feeds
        rss_hits = fetch_rss_feeds(conn)

        # 3. Sjekk lovendringer
        radar_hits = check_law_changes(conn)

        # Kombiner alle sonar-treff
        all_sonar_hits = stortinget_hits + rss_hits

        # Generer og vis rapport
        report = generate_report(all_sonar_hits, radar_hits)
        print("\n" + report)

        # Send e-post
        send_email(report, len(all_sonar_hits), len(radar_hits))

        # Commit database
        conn.commit()

        # Rydd opp gamle entries
        cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)
        conn.execute("DELETE FROM seen_items WHERE date_seen < ?", (cutoff.isoformat(),))
        conn.commit()

        logger.info("=" * 60)
        logger.info(f"Ferdig. {len(all_sonar_hits)} nye signaler, {len(radar_hits)} lovendringer.")
        logger.info("=" * 60)

    finally:
        conn.close()

if __name__ == "__main__":
    main()
