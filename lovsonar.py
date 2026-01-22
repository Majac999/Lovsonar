#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LovSonar v5.1 - Strategisk Fremtidsovervåking for Byggevarehandel
==================================================================
Utviklet for Obs BYGG / Coop Norge.
Nå med aktive kilder: Stortinget API, Regjeringen RSS og Lovdata Radar.
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
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup
from dataclasses import dataclass
from typing import Dict, List, Any

# =============================================================================
# KONFIGURASJON
# =============================================================================

VERSION = "5.1"
USER_AGENT = "LovSonar/5.1 (+https://github.com/Majac999/Lovsonar; Obs BYGG Compliance)"
DB_PATH = os.getenv("LOVSONAR_DB", "lovsonar.db")
REQUEST_TIMEOUT = 30

# Lover som overvåkes for direkte tekstendringer
WATCHED_LAWS = {
    "Åpenhetsloven": "https://lovdata.no/dokument/NL/lov/2021-06-18-99",
    "Byggevareforskriften (DOK)": "https://lovdata.no/dokument/SF/forskrift/2014-12-17-1714",
    "TEK17": "https://lovdata.no/dokument/SF/forskrift/2017-06-19-840",
    "Avfallsforskriften": "https://lovdata.no/dokument/SF/forskrift/2004-06-01-930"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("LovSonar")

@dataclass
class Keyword:
    term: str
    weight: float
    category: str

ALL_KEYWORDS = [
    Keyword("digitalt produktpass", 3.0, "digital"),
    Keyword("dpp", 2.5, "digital"),
    Keyword("espr", 3.0, "eu_core"),
    Keyword("trelast", 2.0, "core"),
    Keyword("byggevare", 2.0, "core"),
    Keyword("grønnvasking", 3.0, "marketing"),
    Keyword("green claims", 3.0, "marketing"),
    Keyword("eudr", 3.0, "deforestation"),
    Keyword("høringsfrist", 3.0, "deadline")
]

# =============================================================================
# KJERNELOGIKK
# =============================================================================

def setup_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen_items (item_id TEXT PRIMARY KEY, source TEXT, title TEXT, date_seen TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS radar_hashes (law_name TEXT PRIMARY KEY, hash TEXT, last_checked TEXT)")
    conn.commit()
    return conn

def analyze_relevance(text: str) -> Dict[str, Any]:
    t = text.lower()
    matches = [kw for kw in ALL_KEYWORDS if kw.term.lower() in t]
    score = sum(kw.weight for kw in matches)
    priority = 3
    if score > 7: priority = 1
    elif score > 4: priority = 2
    
    return {
        "is_relevant": score >= 2.5,
        "score": round(score, 1),
        "priority": priority,
        "matched_keywords": [kw.term for kw in matches]
    }

# =============================================================================
# FETCHERE (MOTORENE)
# =============================================================================

def fetch_stortinget_data(conn) -> List[Dict]:
    """Henter saker fra Stortingets API."""
    hits = []
    # Henter saker for inneværende sesjon
    url = "https://data.stortinget.no/eksport/saker?sesjonid=2025-26"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            ns = {'ns': 'http://data.stortinget.no'}
            for sak in root.findall('.//ns:sak', ns):
                tittel = sak.find('ns:tittel', ns).text
                sak_id = sak.find('ns:id', ns).text
                link = f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={sak_id}"
                
                analysis = analyze_relevance(tittel)
                if analysis["is_relevant"]:
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (sak_id,))
                    if not cursor.fetchone():
                        hits.append({
                            "source": "Stortinget",
                            "title": tittel,
                            "link": link,
                            "priority": analysis["priority"],
                            "score": analysis["score"]
                        })
                        cursor.execute("INSERT INTO seen_items VALUES (?, ?, ?, ?)", 
                                     (sak_id, "Stortinget", tittel, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Stortinget API feil: {e}")
    return hits

def fetch_regjeringen_horinger(conn) -> List[Dict]:
    """Henter nye høringer via RSS."""
    hits = []
    url = "https://www.regjeringen.no/no/id94/?type=rss"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            for item in root.findall('.//item'):
                tittel = item.find('title').text
                link = item.find('link').text
                guid = item.find('guid').text
                
                analysis = analyze_relevance(tittel)
                if analysis["is_relevant"]:
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (guid,))
                    if not cursor.fetchone():
                        hits.append({
                            "source": "Regjeringen (Høring)",
                            "title": tittel,
                            "link": link,
                            "priority": analysis["priority"],
                            "score": analysis["score"]
                        })
                        cursor.execute("INSERT INTO seen_items VALUES (?, ?, ?, ?)", 
                                     (guid, "Regjeringen", tittel, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Regjeringen RSS feil: {e}")
    return hits

def check_law_changes(conn) -> List[Dict]:
    """Sjekker om selve lovteksten på Lovdata har endret seg (Radar)."""
    changes = []
    for name, url in WATCHED_LAWS.items():
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                # Vi henter kun hovedteksten for å unngå støy fra menyer
                content = soup.find("div", class_="dokumentBeholder") or soup.body
                text_hash = hashlib.sha256(content.get_text().encode()).hexdigest()
                
                cursor = conn.cursor()
                cursor.execute("SELECT hash FROM radar_hashes WHERE law_name = ?", (name,))
                row = cursor.fetchone()
                
                if row:
                    if row[0] != text_hash:
                        changes.append({"source": "Lovdata Radar", "title": f"ENDRING DETEKTERT: {name}", "link": url, "priority": 1, "score": 10.0})
                        cursor.execute("UPDATE radar_hashes SET hash = ?, last_checked = ? WHERE law_name = ?", (text_hash, datetime.now().isoformat(), name))
                else:
                    cursor.execute("INSERT INTO radar_hashes VALUES (?, ?, ?)", (name, text_hash, datetime.now().isoformat()))
        except Exception as e:
            logger.error(f"Lovdata sjekk feilet for {name}: {e}")
    return changes

# =============================================================================
# RAPPORTERING & MAIN
# =============================================================================

def send_email_report(hits: List[Dict]):
    user = os.getenv("EMAIL_USER")
    pw = os.getenv("EMAIL_PASS")
    to = os.getenv("EMAIL_RECIPIENT")
    if not all([user, pw, to]) or not hits: return

    report_text = f"LOVSONAR v{VERSION} - STRATEGISK OPPDATERING\n" + "="*45 + "\n\n"
    for h in sorted(hits, key=lambda x: x['priority']):
        report_text += f"[{h['priority']}] {h['title']}\nKilde: {h['source']} | Score: {h['score']}\nLink: {h['link']}\n\n"

    msg = MIMEMultipart()
    msg['Subject'] = f"🚀 LovSonar v{VERSION}: Strategisk Rapport - {datetime.now().strftime('%d.%m')}"
    msg['From'] = user
    msg['To'] = to
    msg.attach(MIMEText(report_text, 'plain'))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(user, pw)
            server.send_message(msg)
        logger.info(f"Rapport sendt til {to}")
    except Exception as e:
        logger.error(f"E-post feil: {e}")

def main():
    logger.info(f"=== LovSonar v{VERSION} starter skanning ===")
    conn = setup_db()
    all_hits = []
    
    all_hits.extend(fetch_stortinget_data(conn))
    all_hits.extend(fetch_regjeringen_horinger(conn))
    all_hits.extend(check_law_changes(conn))
    
    if all_hits:
        send_email_report(all_hits)
    else:
        logger.info("Ingen nye signaler funnet.")
    
    conn.commit()
    conn.close()

if __name__ == "__main__":
    main()
