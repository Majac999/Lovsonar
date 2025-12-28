"""
LovSonar v7.0 - Profesjonell regulatorisk radar for byggevarehandel
"""
import sqlite3, feedparser, logging, os, smtplib, re, hashlib, asyncio, aiohttp, json
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

# ===========================================
# 1. KONFIGURASJON & PRIORITET
# ===========================================

class Priority(Enum):
    CRITICAL = 1; HIGH = 2; MEDIUM = 3; LOW = 4

@dataclass
class Keyword:
    term: str; weight: float = 1.0; category: str = "general"; require_word_boundary: bool = True

# Spesifikke nøkkelord for Obs BYGG / Byggevare
KEYWORDS_SEGMENT = [
    Keyword("byggevare", 2.0, "core"), Keyword("trelast", 1.5, "core"),
    Keyword("obs bygg", 2.0, "core", False), Keyword("coop", 2.0, "core", False),
    Keyword("detaljhandel", 1.0, "retail"), Keyword("ombruk", 1.5, "sustainability")
]

KEYWORDS_TOPIC = [
    Keyword("byggevareforordning", 3.0), Keyword("espr", 2.5), Keyword("ppwr", 2.5),
    Keyword("dpp", 2.5), Keyword("åpenhetsloven", 2.5), Keyword("grønnvasking", 2.0),
    Keyword("reach", 2.0), Keyword("pfas", 2.5), Keyword("tek17", 2.0)
]

KEYWORDS_CRITICAL = [
    Keyword("høringsfrist", 3.0), Keyword("frist", 2.0), Keyword("ikrafttredelse", 2.5),
    Keyword("trer i kraft", 2.5), Keyword("vedtak", 1.5)
]

KEYWORDS_NOISE = ["skriv ut", "verktøylinje", "del paragraf", "logg inn", "søk"]

RSS_SOURCES = {
    "📢 Høringer": {"url": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/?show=rss", "max_age_days": 90, "base_priority": Priority.HIGH},
    "🇪🇺 Europapolitikk": {"url": "https://www.regjeringen.no/no/tema/europapolitikk/id1160/?show=rss", "max_age_days": 120, "base_priority": Priority.HIGH},
    "🏗️ DiBK Nyheter": {"url": "https://dibk.no/rss", "max_age_days": 90, "base_priority": Priority.HIGH},
    "🌿 Miljødirektoratet": {"url": "https://www.miljodirektoratet.no/rss/aktuelt/", "max_age_days": 90, "base_priority": Priority.MEDIUM}
}

DB_PATH = "lovsonar_v7.db"
USER_AGENT = "LovSonar/7.0 (Coop Obs BYGG Compliance Intelligence)"
MAX_PDF_SIZE = 10_000_000

# ===========================================
# 2. LOGIKK & ANALYSE
# ===========================================

@dataclass
class AnalysisResult:
    is_relevant: bool = False; segment_score: float = 0.0; topic_score: float = 0.0
    critical_score: float = 0.0; total_score: float = 0.0; priority: Priority = Priority.LOW
    matched_keywords: list = field(default_factory=list); categories: set = field(default_factory=set)
    deadline: Optional[datetime] = None; deadline_text: str = ""

@dataclass
class Item:
    source: str; title: str; description: str; link: str; pub_date: datetime; item_id: str
    full_text: str = ""; analysis: Optional[AnalysisResult] = None

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

def analyze_content(text: str, source_name: str) -> AnalysisResult:
    res = AnalysisResult(); t = text.lower()
    if sum(1 for n in KEYWORDS_NOISE if n in t) > 5: return res

    for kw in KEYWORDS_SEGMENT:
        if kw.term.lower() in t: res.segment_score += kw.weight; res.matched_keywords.append(kw.term)
    for kw in KEYWORDS_TOPIC:
        if kw.term.lower() in t: res.topic_score += kw.weight; res.matched_keywords.append(kw.term)
    for kw in KEYWORDS_CRITICAL:
        if kw.term.lower() in t: res.critical_score += kw.weight; res.matched_keywords.append(kw.term)

    res.deadline, res.deadline_text = extract_deadline(text)
    res.total_score = (res.segment_score * 1.5) + res.topic_score + (res.critical_score * 2.0)
    
    res.is_relevant = res.total_score >= 5.0 or "høring" in t
    
    if res.is_relevant:
        if res.deadline and (res.deadline - datetime.now()).days <= 30: res.priority = Priority.CRITICAL
        elif res.total_score >= 10.0: res.priority = Priority.HIGH
        else: res.priority = Priority.MEDIUM
    return res

# --- (Asynkron henting og DB-håndtering følger her - samme struktur som v7-diffen) ---
# [Her implementeres de tekniske delene for aiohttp og sqlite3]

# --- (Rapportgenerering i HTML følger her) ---

# ... (resten av koden din for e-post og main) ...
