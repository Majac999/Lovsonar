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
- DiBK RSS - Byggteknisk forskrift, TEK17
- Lovdata HTML-scraping - Lovendringer (med caching)
"""

import os
import json
import hashlib
import smtplib
import re
import logging
import sqlite3
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
CHANGE_THRESHOLD = 0.5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("LovSonar")

# =============================================================================
# PRIORITET & NØKKELORD
# =============================================================================

class Priority(Enum):
    CRITICAL = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4

@dataclass
class Keyword:
    term: str
    weight: float
    category: str
    description: str = ""

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

KEYWORDS_EU_SUSTAINABILITY = [
    Keyword("espr", 3.0, "eu_core", "Ecodesign for Sustainable Products Regulation"),
    Keyword("ecodesign", 2.5, "eu_core", "Økodesign-krav"),
    Keyword("digitalt produktpass", 3.0, "digital", "Digital Product Passport (DPP)"),
    Keyword("dpp", 2.5, "digital", "Digital Product Passport"),
    Keyword("produktpass", 2.5, "digital", "Produktdokumentasjon"),
    Keyword("ppwr", 2.5, "packaging", "Packaging and Packaging Waste Regulation"),
    Keyword("emballasje", 2.0, "packaging", "Emballasjekrav"),
    Keyword("engangsplast", 2.0, "packaging", "SUP-direktiv"),
    Keyword("produsentansvar", 2.0, "packaging", "EPR - Extended Producer Responsibility"),
    Keyword("eudr", 2.5, "deforestation", "EU Deforestation Regulation"),
    Keyword("avskoging", 2.0, "deforestation", "Avskogingsfri"),
    Keyword("sporbarhet", 2.0, "deforestation", "Verdikjede-sporbarhet"),
    Keyword("miljødeklarasjon", 2.0, "sustainability", "EPD"),
    Keyword("epd", 2.0, "sustainability", "Environmental Product Declaration"),
    Keyword("klimaavtrykk", 1.5, "sustainability", "Carbon footprint"),
    Keyword("livsløpsanalyse", 1.5, "sustainability", "LCA"),
    Keyword("bærekraft", 1.5, "sustainability", "Generell bærekraft"),
    Keyword("sirkulær", 1.5, "sustainability", "Sirkulærøkonomi"),
    Keyword("ombruk", 1.5, "sustainability", "Gjenbruk"),
    Keyword("reach", 2.0, "chemicals", "REACH-forordningen"),
    Keyword("pfas", 2.5, "chemicals", "PFAS-forbud"),
    Keyword("farlige stoffer", 2.0, "chemicals", "Kjemikalieregulering"),
    Keyword("biocid", 1.5, "chemicals", "Biocidforordningen"),
]

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

RSS_SOURCES = {
    "⚖️ Forbrukertilsynet": {
        "url": "https://www.forbrukertilsynet.no/feed",
        "type": "consumer",
    },
    "🏗️ DiBK Nyheter": {
        "url": "https://dibk.no/nyheter/rss/",
        "type": "building",
    },
}

STORTINGET_API = {
    "saker": "https://data.stortinget.no/eksport/saker?sesjonid={sesjon}",
    "horinger": "https://data.stortinget.no/eksport/horinger?sesjonid={sesjon}",
}

LAWS_TO_MONITOR = {
    "Åpenhetsloven": "https://lovdata.no/dokument/NL/lov/2021-06-18-99",
    "Produktkontrolloven": "https://lovdata.no/dokument/NL/lov/1976-06-11-79",
    "Markedsføringsloven": "https://lovdata.no/dokument/NL/lov/2009-01-09-2",
    "Forbrukerkjøpsloven": "https://lovdata.no/dokument/NL/lov/2002-06-21-34",
}

REGULATIONS_TO_MONITOR = {
    "Byggevareforskriften (DOK)": "https://lovdata.no/dokument/SF/forskrift/2014-12-17-1714",
    "Avfallsforskriften": "https://lovdata.no/dokument/SF/forskrift/2004-06-01-930",
    "Produktforskriften": "https://lovdata.no/dokument/SF/forskrift/2004-06-01-922",
}

# =============================================================================
# DATABASE
# =============================================================================

def setup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_items (
            item_id TEXT PRIMARY KEY, source TEXT, title TEXT, date_seen TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sonar_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT, title TEXT, link TEXT, priority INTEGER,
            score REAL, deadline TEXT, matched_keywords TEXT,
            category TEXT, detected_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS radar_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            law_name TEXT, url TEXT, change_percent REAL,
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn

# =============================================================================
# ANALYSE
# =============================================================================

def extract_deadline(text: str) -> Optional[str]:
    patterns = [
        r'(?:høringsfrist|frist)[:\s]+(\d{1,2})[.\s]+([a-zA-ZæøåÆØÅ]+)\s+(\d{4})',
        r'(?:frist|deadline)[:\s]+(\d{1,2})\.(\d{1,2})\.(\d{4})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0)
    return None

def analyze_relevance(text: str, source_type: str = "") -> Dict[str, Any]:
    t = text.lower()

    segment_matches = [(kw.term, kw.weight, kw.category) for kw in KEYWORDS_SEGMENT if kw.term.lower() in t]
    topic_matches = [(kw.term, kw.weight, kw.category) for kw in KEYWORDS_EU_SUSTAINABILITY if kw.term.lower() in t]
    norwegian_matches = [(kw.term, kw.weight, kw.category) for kw in KEYWORDS_NORWEGIAN if kw.term.lower() in t]
    critical_matches = [(kw.term, kw.weight, kw.category) for kw in KEYWORDS_CRITICAL if kw.term.lower() in t]

    all_matches = segment_matches + topic_matches + norwegian_matches + critical_matches

    segment_score = sum(w for _, w, _ in segment_matches) * 1.5
    topic_score = sum(w for _, w, _ in topic_matches)
    norwegian_score = sum(w for _, w, _ in norwegian_matches)
    critical_score = sum(w for _, w, _ in critical_matches)
    total_score = segment_score + topic_score + norwegian_score + critical_score

    has_high_priority = any(w >= 2.5 for _, w, _ in topic_matches + norwegian_matches)
    is_relevant = total_score >= 3.0 or has_high_priority

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
        "main_category": main_category,
        "deadline": extract_deadline(text),
    }

# =============================================================================
# STORTINGET API
# =============================================================================

def get_current_session() -> str:
    now = datetime.now()
    year = now.year
    if now.month >= 10:
        return f"{year}-{year + 1}"
    return f"{year - 1}-{year}"

def fetch_stortinget_data(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
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
            items = root.findall(".//s:sak", ns) or root.findall(".//s:horing", ns)

            for item in items:
                title = item.findtext("s:tittel", "", ns) or item.findtext("s:kort_tittel", "", ns)
                item_id = item.findtext("s:id", "", ns)
                komite = item.findtext("s:komite/s:navn", "", ns) or ""

                hash_id = hashlib.sha256(f"{item_id}{title}".encode()).hexdigest()[:16]
                if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (hash_id,)).fetchone():
                    continue

                analysis = analyze_relevance(f"{title} {komite}", "stortinget")
                conn.execute(
                    "INSERT INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
                    (hash_id, "Stortinget", title, datetime.now().isoformat())
                )

                if analysis["is_relevant"]:
                    link = f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={item_id}"
                    hit = {
                        "source": "🏛️ Stortinget", "title": title, "link": link,
                        "priority": analysis["priority"], "score": analysis["score"],
                        "matched_keywords": analysis["matched_keywords"],
                        "category": analysis["main_category"], "deadline": analysis["deadline"],
                    }
                    hits.append(hit)
                    conn.execute(
                        """INSERT INTO sonar_hits (source, title, link, priority, score, deadline, matched_keywords, category)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        ("Stortinget", title, link, analysis["priority"].value, analysis["score"],
                         analysis["deadline"], ",".join(analysis["matched_keywords"]), analysis["main_category"])
                    )
                    logger.info(f"  [{analysis['priority'].name}] {title[:60]}...")

            logger.info(f"  Prosessert {len(items)} fra {endpoint_name}")
        except Exception as e:
            logger.error(f"Feil Stortinget ({endpoint_name}): {e}")

    return hits

# =============================================================================
# RSS-FEEDS
# =============================================================================

def parse_rss_manually(content: bytes) -> List[Dict[str, str]]:
    """Parse RSS uten feedparser-avhengighet."""
    entries = []
    try:
        root = ET.fromstring(content)
        for item in root.findall(".//item"):
            entries.append({
                "title": item.findtext("title", ""),
                "link": item.findtext("link", ""),
                "summary": item.findtext("description", ""),
            })
    except Exception as e:
        logger.error(f"RSS parse error: {e}")
    return entries

def fetch_rss_feeds(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    logger.info("📡 Sjekker RSS-feeds...")
    hits = []

    for name, config in RSS_SOURCES.items():
        try:
            response = requests.get(config["url"], headers={"User-Agent": USER_AGENT}, timeout=30)
            response.raise_for_status()
            entries = parse_rss_manually(response.content)

            for entry in entries[:15]:
                link = entry.get("link", "")
                title = entry.get("title", "")
                summary = entry.get("summary", "")

                hash_id = hashlib.sha256(link.encode()).hexdigest()[:16]
                if conn.execute("SELECT 1 FROM seen_items WHERE item_id=?", (hash_id,)).fetchone():
                    continue

                analysis = analyze_relevance(f"{title} {summary}", config["type"])
                conn.execute(
                    "INSERT INTO seen_items (item_id, source, title, date_seen) VALUES (?,?,?,?)",
                    (hash_id, name, title, datetime.now().isoformat())
                )

                if analysis["is_relevant"]:
                    hit = {
                        "source": name, "title": title, "link": link,
                        "priority": analysis["priority"], "score": analysis["score"],
                        "matched_keywords": analysis["matched_keywords"],
                        "category": analysis["main_category"], "deadline": analysis["deadline"],
                    }
                    hits.append(hit)
                    conn.execute(
                        """INSERT INTO sonar_hits (source, title, link, priority, score, deadline, matched_keywords, category)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (name, title, link, analysis["priority"].value, analysis["score"],
                         analysis["deadline"], ",".join(analysis["matched_keywords"]), analysis["main_category"])
                    )
                    logger.info(f"  [{analysis['priority'].name}] {title[:60]}...")

            logger.info(f"  ✓ {name}: {len(entries)} innlegg")
        except Exception as e:
            logger.error(f"  ✗ {name}: {e}")

    return hits

# =============================================================================
# LOVENDRING-OVERVÅKING
# =============================================================================

def check_law_changes(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    logger.info("📜 Sjekker lover for endringer...")
    hits = []
    cache = {}

    if Path(CACHE_FILE).exists():
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)

    all_docs = {**LAWS_TO_MONITOR, **REGULATIONS_TO_MONITOR}

    for name, url in all_docs.items():
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            for el in soup(["script", "style", "nav", "footer"]):
                el.decompose()

            text = re.sub(r'\s+', ' ', soup.get_text()).strip()
            new_hash = hashlib.sha256(text.encode()).hexdigest()

            prev = cache.get(name, {})
            if prev and new_hash != prev.get("hash"):
                similarity = SequenceMatcher(None, prev.get("text", "")[:5000], text[:5000]).ratio()
                change = round((1 - similarity) * 100, 2)
                if change >= CHANGE_THRESHOLD:
                    hits.append({"name": name, "url": url, "change_percent": change})
                    conn.execute("INSERT INTO radar_hits (law_name, url, change_percent) VALUES (?,?,?)",
                                 (name, url, change))
                    logger.info(f"  ⚠️ {name}: {change}% endring")

            cache[name] = {"hash": new_hash, "text": text[:5000], "checked": datetime.now().isoformat()}
        except Exception as e:
            logger.error(f"  ✗ {name}: {e}")

    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    logger.info(f"  Sjekket {len(all_docs)} dokumenter")
    return hits

# =============================================================================
# RAPPORTERING
# =============================================================================

def generate_report(sonar_hits: List[Dict], radar_hits: List[Dict]) -> str:
    report = ["# LOVSONAR STRATEGISK RAPPORT",
              f"Generert: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
              "", "Fokus: EU Green Deal, bærekraft, compliance, grønnvasking.",
              "=" * 70, ""]

    if radar_hits:
        report.append("## 📜 LOVENDRINGER DETEKTERT\n")
        for h in radar_hits:
            report.extend([f"### ⚠️ {h['name']}", f"- Endring: {h['change_percent']}%",
                          f"- Lenke: {h['url']}", ""])

    if sonar_hits:
        critical = [h for h in sonar_hits if h["priority"] == Priority.CRITICAL]
        high = [h for h in sonar_hits if h["priority"] == Priority.HIGH]
        medium = [h for h in sonar_hits if h["priority"] == Priority.MEDIUM]

        if critical:
            report.append("## 🚨 KRITISKE SIGNALER\n")
            for h in sorted(critical, key=lambda x: x["score"], reverse=True):
                report.extend([f"### {h['title']}", f"- Kilde: {h['source']}",
                              f"- Score: {h['score']} | {h['category']}",
                              f"- Nøkkelord: {', '.join(h['matched_keywords'])}",
                              f"- Lenke: {h['link']}", ""])

        if high:
            report.append("## ⚡ HØY PRIORITET\n")
            for h in sorted(high, key=lambda x: x["score"], reverse=True):
                report.extend([f"### {h['title']}", f"- Kilde: {h['source']} | Score: {h['score']}",
                              f"- Nøkkelord: {', '.join(h['matched_keywords'])}",
                              f"- Lenke: {h['link']}", ""])

        if medium:
            report.append("## 📋 MEDIUM PRIORITET\n")
            for h in sorted(medium, key=lambda x: x["score"], reverse=True)[:10]:
                report.append(f"- {h['title'][:70]}... (Score: {h['score']})")
            report.append("")

    if not sonar_hits and not radar_hits:
        report.append("Ingen nye relevante signaler.")

    return "\n".join(report)

def send_email(report: str, sonar_count: int, radar_count: int) -> bool:
    user = os.environ.get("EMAIL_USER", "").strip()
    pw = os.environ.get("EMAIL_PASS", "").strip()
    recipient = os.environ.get("EMAIL_RECIPIENT", "").strip()

    if not all([user, pw, recipient]):
        logger.warning("E-post mangler konfigurasjon.")
        return False

    if sonar_count == 0 and radar_count == 0:
        logger.info("Ingen funn - sender ikke e-post.")
        return False

    msg = MIMEMultipart()
    msg["Subject"] = f"🛡️ LovSonar: {sonar_count} signaler, {radar_count} lovendringer"
    msg["From"] = user
    msg["To"] = recipient
    msg.attach(MIMEText(report, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(user, pw)
            server.send_message(msg)
        logger.info(f"📧 Rapport sendt til {recipient}")
        return True
    except Exception as e:
        logger.error(f"E-post feil: {e}")
        return False

# =============================================================================
# HOVEDPROGRAM
# =============================================================================

def main():
    logger.info("=" * 60)
    logger.info("LovSonar v3.0 - Strategisk Fremtidsovervåking")
    logger.info("=" * 60)

    conn = setup_db()

    try:
        stortinget_hits = fetch_stortinget_data(conn)
        rss_hits = fetch_rss_feeds(conn)
        radar_hits = check_law_changes(conn)

        all_sonar_hits = stortinget_hits + rss_hits
        report = generate_report(all_sonar_hits, radar_hits)
        print("\n" + report)

        send_email(report, len(all_sonar_hits), len(radar_hits))
        conn.commit()

        cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)
        conn.execute("DELETE FROM seen_items WHERE date_seen < ?", (cutoff.isoformat(),))
        conn.commit()

        logger.info("=" * 60)
        logger.info(f"Ferdig. {len(all_sonar_hits)} signaler, {len(radar_hits)} lovendringer.")
        logger.info("=" * 60)

    finally:
        conn.close()

if __name__ == "__main__":
    main()
