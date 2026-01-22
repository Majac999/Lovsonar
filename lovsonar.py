#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LovSonar v5.3 - Strategisk Fremtidsovervåking for Byggevarehandel
==================================================================
Optimalisert for Obs BYGG / Coop Norge.
Nå med: EMV-spesifikk overvåking (Emballasje, Design, Dokumentasjon).
Fokus: ESPR, PPWR og Digitale Produktpass for egne merkevarer.
"""

import os
import json
import smtplib
import logging
import sqlite3
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from xml.etree import ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Any

# =============================================================================
# KONFIGURASJON
# =============================================================================

VERSION = "5.3"
USER_AGENT = "LovSonar/5.3 (+https://github.com/Majac999/Lovsonar; Obs BYGG Strategic Pilot)"
DB_PATH = os.getenv("LOVSONAR_DB", "lovsonar.db")
REQUEST_TIMEOUT = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("LovSonar")

@dataclass
class Keyword:
    term: str
    weight: float
    category: str

# Utvidet liste med fokus på EMV og produsentansvar (PPWR/ESPR)
ALL_KEYWORDS = [
    # Digitale og sirkulære krav
    Keyword("digitalt produktpass", 3.0, "digital"),
    Keyword("dpp", 2.5, "digital"),
    Keyword("espr", 3.0, "emv_design"),
    Keyword("ecodesign", 3.0, "emv_design"),
    
    # Emballasje og Merking (Kritisk for EMV)
    Keyword("ppwr", 3.5, "emv_packaging"),
    Keyword("emballasje", 2.5, "emv_packaging"),
    Keyword("packaging", 2.0, "emv_packaging"),
    Keyword("merking", 2.0, "emv_labeling"),
    Keyword("labeling", 2.0, "emv_labeling"),
    
    # Bransjespesifikt
    Keyword("byggevare", 2.0, "core"),
    Keyword("trelast", 2.0, "core"),
    Keyword("eudr", 3.0, "deforestation"),
    
    # Markedsføring og Jus
    Keyword("grønnvasking", 3.0, "marketing"),
    Keyword("green claims", 3.0, "marketing"),
    Keyword("høringsfrist", 3.0, "deadline")
]

# =============================================================================
# KJERNELOGIKK
# =============================================================================

def setup_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen_items (item_id TEXT PRIMARY KEY, source TEXT, title TEXT, date_seen TEXT)")
    conn.commit()
    return conn

def analyze_relevance(text: str) -> Dict[str, Any]:
    t = text.lower()
    matches = [kw for kw in ALL_KEYWORDS if kw.term.lower() in t]
    score = sum(kw.weight for kw in matches)
    
    # Spesifikk flagg for EMV-relevans
    is_emv_relevant = any(kw.category.startswith("emv_") for kw in matches)
    
    priority = 3
    if score > 7 or is_emv_relevant: priority = 1 # EMV-treff får høy prioritet
    elif score > 4: priority = 2
    
    return {
        "is_relevant": score >= 2.5,
        "score": round(score, 1),
        "priority": priority,
        "is_emv": is_emv_relevant,
        "matched_keywords": [kw.term for kw in matches]
    }

# =============================================================================
# FETCHERE
# =============================================================================

def fetch_stortinget_data(conn) -> List[Dict]:
    hits = []
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
                            "title": f"{'[EMV ALERT] ' if analysis['is_emv'] else ''}{tittel}", 
                            "link": link, 
                            "priority": analysis["priority"], 
                            "score": analysis["score"]
                        })
                        cursor.execute("INSERT INTO seen_items VALUES (?, ?, ?, ?)", (sak_id, "Stortinget", tittel, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Stortinget API feil: {e}")
    return hits

def fetch_eu_signals(conn) -> List[Dict]:
    hits = []
    url = "https://ec.europa.eu/environment/news/rss_en" 
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            for item in root.findall('.//item'):
                tittel = item.find('title').text
                link = item.find('link').text
                guid = item.find('guid').text if item.find('guid') is not None else link
                
                analysis = analyze_relevance(tittel)
                if analysis["is_relevant"]:
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (guid,))
                    if not cursor.fetchone():
                        hits.append({
                            "source": "EU Commission", 
                            "title": f"{'[EMV ALERT] ' if analysis['is_emv'] else ''}{tittel}", 
                            "link": link, 
                            "priority": analysis["priority"], 
                            "score": analysis["score"]
                        })
                        cursor.execute("INSERT INTO seen_items VALUES (?, ?, ?, ?)", (guid, "EU_Signals", tittel, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"EU RSS feil: {e}")
    return hits

# =============================================================================
# RAPPORTERING
# =============================================================================

def send_email_report(hits: List[Dict]):
    user = os.getenv("EMAIL_USER")
    pw = os.getenv("EMAIL_PASS")
    to = os.getenv("EMAIL_RECIPIENT")
    if not all([user, pw, to]) or not hits: return

    report_text = f"LOVSONAR v{VERSION} - STRATEGISK OPPDATERING (EMV FOKUS)\n" + "="*55 + "\n"
    report_text += "Dette varselet inneholder tidlige signaler om regulatoriske krav\nsom kan påvirke Coop/Obs BYGGs egne merkevarer.\n\n"
    
    for h in sorted(hits, key=lambda x: x['priority']):
        report_text += f"[{h['priority']}] {h['title']}\nKilde: {h['source']} | Score: {h['score']}\nLink: {h['link']}\n\n"

    msg = MIMEMultipart()
    msg['Subject'] = f"🛡️ LovSonar v{VERSION}: EMV & Compliance Rapport - {datetime.now().strftime('%d.%m')}"
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
    logger.info(f"=== LovSonar v{VERSION} starter strategisk skanning ===")
    conn = setup_db()
    all_hits = []
    
    all_hits.extend(fetch_stortinget_data(conn))
    all_hits.extend(fetch_eu_signals(conn))
    
    if all_hits:
        send_email_report(all_hits)
    else:
        logger.info("Ingen nye kritiske signaler funnet.")
    
    conn.commit()
    conn.close()

if __name__ == "__main__":
    main()
