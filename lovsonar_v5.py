#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LovSonar v5.0 - Strategisk Fremtidsovervåking for Byggevarehandel
==================================================================
Optimalisert for Obs BYGG / Coop Norge.
Inkluderer: Radar, Sonar, Korrelasjonsanalyse og E-post-varsling.
"""

import os
import json
import hashlib
import smtplib
import re
import logging
import sqlite3
import requests
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup
from difflib import SequenceMatcher
from dataclasses import dataclass

# =============================================================================
# KONFIGURASJON
# =============================================================================

VERSION = "5.0"
USER_AGENT = "LovSonar/5.0 (+https://github.com/Majac999/Lovsonar; Obs BYGG Compliance)"
DB_PATH = os.getenv("LOVSONAR_DB", "lovsonar_v5.db")
CACHE_FILE = os.getenv("LOVSONAR_CACHE", "lovsonar_cache_v5.json")
MAX_AGE_DAYS = int(os.getenv("LOVSONAR_MAX_AGE_DAYS", "180"))
CHANGE_THRESHOLD = 0.5
REQUEST_TIMEOUT = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("LovSonar")

@dataclass
class Keyword:
    term: str
    weight: float
    category: str
    description: str = ""

# Strategisk utvalgte nøkkelord for 2026
ALL_KEYWORDS = [
    Keyword("byggevare", 2.5, "core"),
    Keyword("trelast", 2.0, "core"),
    Keyword("digitalt produktpass", 3.0, "digital"),
    Keyword("dpp", 2.5, "digital"),
    Keyword("espr", 3.0, "eu_core"),
    Keyword("csrd", 3.0, "reporting"),
    Keyword("green claims", 3.0, "marketing"),
    Keyword("grønnvasking", 3.0, "marketing"),
    Keyword("miljøpåstand", 2.5, "marketing"),
    Keyword("klimanøytral", 3.0, "marketing"),
    Keyword("eudr", 3.0, "deforestation"),
    Keyword("avskogingsfri", 3.0, "deforestation"),
    Keyword("pfas", 3.0, "chemicals"),
    Keyword("reach", 2.5, "chemicals"),
    Keyword("tek17", 2.5, "building"),
    Keyword("dok-forskriften", 2.5, "building"),
    Keyword("høringsfrist", 3.0, "deadline"),
    Keyword("ikrafttredelse", 3.0, "deadline")
]

# =============================================================================
# DATABASE & LOGIKK
# =============================================================================

def setup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen_items (item_id TEXT PRIMARY KEY, source TEXT, title TEXT, date_seen TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS sonar_hits (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, title TEXT, link TEXT, priority INTEGER, score REAL, deadline TEXT, matched_keywords TEXT, category TEXT, detected_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS radar_hits (id INTEGER PRIMARY KEY AUTOINCREMENT, law_name TEXT, url TEXT, change_percent REAL, detected_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS correlations (id INTEGER PRIMARY KEY AUTOINCREMENT, radar_law TEXT, sonar_signal TEXT, connection_keywords TEXT, detected_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    return conn

def analyze_relevance(text: str) -> Dict[str, Any]:
    t = text.lower()
    matches = [kw for kw in ALL_KEYWORDS if kw.term.lower() in t]
    score = sum(kw.weight for kw in matches)
    is_relevant = score >= 3.0 or any(kw.weight >= 3.0 for kw in matches)
    
    priority = 4 # LOW
    if is_relevant:
        if score > 8: priority = 1 # CRITICAL
        elif score > 5: priority = 2 # HIGH
        else: priority = 3 # MEDIUM

    return {
        "is_relevant": is_relevant,
        "score": round(score, 1),
        "priority": priority,
        "matched_keywords": [kw.term for kw in matches],
        "category": matches[0].category if matches else "Generelt"
    }

# =============================================================================
# E-POST FUNKSJONALITET
# =============================================================================

def send_email_report(text_report: str, json_data: List[Dict]):
    """Sender rapporten til din e-post ved hjelp av miljøvariabler."""
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASS")
    recipient = os.getenv("EMAIL_RECIPIENT")
    
    if not all([user, password, recipient]):
        logger.warning("E-post-konfigurasjon mangler. Sjekk EMAIL_USER, EMAIL_PASS og EMAIL_RECIPIENT.")
        return

    msg = MIMEMultipart()
    msg['Subject'] = f"🚀 LovSonar v{VERSION}: Strategisk Rapport - {datetime.now().strftime('%d.%m.%Y')}"
    msg['From'] = user
    msg['To'] = recipient

    # Brødtekst
    msg.attach(MIMEText(text_report, 'plain'))

    # JSON-vedlegg for data-analyse
    if json_data:
        json_attachment = MIMEApplication(json.dumps(json_data, indent=2, ensure_ascii=False).encode('utf-8'))
        json_attachment.add_header('Content-Disposition', 'attachment', filename=f"lovsonar_data_{datetime.now().strftime('%Y%m%d')}.json")
        msg.attach(json_attachment)

    try:
        # Standard for Gmail/Outlook/Coop SMTP (Port 465 for SSL)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(user, password)
            server.send_message(msg)
        logger.info(f"Rapport vellykket sendt til {recipient}")
    except Exception as e:
        logger.error(f"Feil ved sending av e-post: {e}")

# =============================================================================
# FETCHERE (FORKORTET FOR GitHub)
# =============================================================================

def fetch_stortinget_data(conn):
    hits = []
    # Forenklet for demo, her kan du legge inn full API-logikk fra v4.0
    return hits

def check_law_changes(conn):
    # Radar-logikk som sammenligner hashes
    return []

# =============================================================================
# MAIN LOOP
# =============================================================================

def main():
    logger.info(f"=== LovSonar v{VERSION} starter skanning for Obs BYGG ===")
    conn = setup_db()
    
    try:
        # 1. Kjør skanninger
        sonar_hits = fetch_stortinget_data(conn) # Legg til fetch_regjeringen_horinger her også
        radar_hits = check_law_changes(conn)
        
        # 2. Generer rapport hvis vi har funn
        if sonar_hits or radar_hits:
            report_text = f"LOVSONAR v{VERSION} - STRATEGISK OPPDATERING\n"
            report_text += "="*40 + "\n"
            report_text += f"Detektert: {len(sonar_hits)} signaler og {len(radar_hits)} lovendringer.\n\n"
            
            for hit in sonar_hits:
                report_text += f"[{hit['priority']}] {hit['title']}\nKilde: {hit['source']} | Score: {hit['score']}\n"
            
            # Send e-post
            send_email_report(report_text, sonar_hits)
            print(report_text)
        else:
            logger.info("Ingen nye kritiske endringer funnet denne uken.")

        conn.commit()
    except Exception as e:
        logger.error(f"Kritisk feil i systemet: {e}")
        print(traceback.format_exc())
    finally:
        conn.close()

if __name__ == "__main__":
    main()
