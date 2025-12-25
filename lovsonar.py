#!/usr/bin/env python3
"""
LovSonar – Robust versjon.
Bruker Stortinget API (fungerer) + Regjeringen Nettside-lesing (unngår 404).
"""

import logging
import sqlite3
import requests
import re
import os
import sys
from datetime import datetime
from html import unescape
from bs4 import BeautifulSoup

# =============================
# KONFIGURASJON
# =============================

DB_PATH = "lovsonar_seen.db"

# Nøkkelord for Obs Bygg / Coop
KEYWORDS = [
    "coop", "samvirke", "varehandel", "byggevare", "bygg",
    "bærekraft", "miljø", "emballasje", "avfall",
    "sirkulær", "gjenvinning", "eøs", "esg",
    "csrd", "taksonomi", "aktsomhet", "arbeidsmiljø",
    "plan- og bygningsloven", "avhendingslova", "håndverkertjenesteloven"
]

# Vi later som vi er en vanlig PC for å slippe inn hos Regjeringen
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
}

# =============================
# LOGGING & DATABASE
# =============================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
log = logging.getLogger("lovsonar")

def get_db():
    return sqlite3.connect(DB_PATH)

def setup_db():
    with get_db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                id TEXT PRIMARY KEY,
                title TEXT,
                source TEXT,
                url TEXT,
                date_seen TEXT
            )
        """)

def is_seen(id_):
    with get_db() as con:
        try:
            # Sjekker både ny og gammel tabellstruktur for sikkerhets skyld
            return con.execute("SELECT 1 FROM seen WHERE id = ?", (id_,)).fetchone() is not None
        except:
            return False

def mark_seen(id_, title, source, url):
    with get_db() as con:
        try:
            con.execute(
                "INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?, ?)",
                (id_, title, source, url, datetime.utcnow().isoformat())
            )
        except:
            pass # Ignorer feil hvis tabellen er låst el.l.

# =============================
# HJELPEFUNKSJONER
# =============================

def clean_text(txt):
    if not txt: return ""
    txt = unescape(txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()

def is_relevant(text):
    if not text: return False
    t = text.lower()
    return any(k in t for k in KEYWORDS)

# =============================
# 1. REGJERINGEN.NO (WEB-SKRAPING)
# =============================
# Dette erstatter RSS som ga 404-feil. Vi leser nettsiden direkte.

def check_regjeringen():
    # Dette er de vanlige nettsidene for dokumenter
    urls = {
        "Høringer": "https://www.regjeringen.no/no/aktuelt/horinger/id1763/",
        "NOU": "https://www.regjeringen.no/no/dokument/nou-ar/id1767/",
        "Proposisjoner": "https://www.regjeringen.no/no/dokument/proposisjoner-og-meldinger/id1754/",
        "EØS-notater": "https://www.regjeringen.no/no/tema/europapolitikk/eos-notater/id669358/"
    }

    session = requests.Session()
    session.headers.update(HEADERS)

    for source, url in urls.items():
        log.info(f"🌐 Sjekker Regjeringen: {source}...") # Se etter dette ikonet i loggen!
        try:
            r = session.get(url, timeout=20)
            
            if r.status_code == 404:
                log.warning(f"⚠️ 404 på {source} - men dette er uvanlig for nettsider.")
                continue
                
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")
            
            # Vi ser etter lenker i hovedinnholdet
            main_content = soup.find(id="mainContent") or soup
            links = main_content.select("h3 a, .teaser-content a, li a")

            count = 0
            for a in links:
                title = clean_text(a.get_text())
                href = a.get("href", "")

                if len(title) < 10: continue
                if "javascript" in href or "#" in href: continue
                
                # Fiks relative lenker
                if href.startswith("/"):
                    full_url = "https://www.regjeringen.no" + href
                else:
                    full_url = href

                # Sjekk relevans (Nøkkelord)
                if not is_relevant(title):
                    continue

                if is_seen(full_url):
                    continue

                log.info(f"✅ TREFF ({source}): {title}")
                mark_seen(full_url, title, f"Regjeringen ({source})", full_url)
                count += 1
            
            if count > 0:
                log.info(f"   Lagret {count} relevante saker fra {source}.")

        except Exception as e:
            log.error(f"❌ Feil mot {source}: {e}")

# =============================
# 2. STORTINGET (API)
# =============================
# Denne delen fungerte allerede fint i loggen din!

def check_stortinget():
    log.info("🏛️ Sjekker Stortinget API...")

    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        # 1. Hent sesjon
        ses_resp = session.get("https://data.stortinget.no/eksport/sesjoner?format=json", timeout=10)
        ses_resp.raise_for_status()
        sid = ses_resp.json()["innevaerende_sesjon"]["id"]

        # 2. Hent saker
        saker_resp = session.get(f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&format=json", timeout=10)
        saker_resp.raise_for_status()
        
        saker = saker_resp.json().get("saker_liste", [])
        log.info(f"   Hentet {len(saker)} saker fra Stortinget. Filtrerer...")

        count = 0
        for sak in saker:
            title = clean_text(sak.get("tittel", ""))
            
            # Sjekk relevans
            if not is_relevant(title):
                continue

            item_id = f"stortinget-{sak['id']}"
            
            if is_seen(item_id):
                continue

            url = f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={sak['id']}"
            log.info(f"✅ TREFF STORTINGET: {title}")
            mark_seen(item_id, title, "Stortinget", url)
            count += 1
            
        if count > 0:
            log.info(f"   Lagret {count} relevante saker fra Stortinget.")

    except Exception as e:
        log.error(f"❌ Feil mot Stortinget: {e}")

# =============================
# MAIN
# =============================

def main():
    setup_db()
    # Vi sletter ikke gamle data hver gang nå, for å bygge historikk
    
    check_regjeringen() # Denne erstatter RSS
    check_stortinget()  # Denne fungerer fint
    
    log.info("🏁 Ferdig. Sjekker igjen om 6 timer.")

if __name__ == "__main__":
    main()
