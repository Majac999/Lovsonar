#!/usr/bin/env python3
"""
LovSonar – stabil overvåkning av Regjeringen.no (HTML) og Stortinget (API)
Fungerer i GitHub Actions uten 404-feil.
"""

import logging
import sqlite3
import requests
import re
import os
from datetime import datetime
from html import unescape
from bs4 import BeautifulSoup

# =============================
# KONFIGURASJON
# =============================

DB_PATH = "lovsonar_seen.db"  # Samme filnavn som før for å bevare historikk

# Dine nøkkelord for Obs Bygg / Coop
KEYWORDS = [
    "coop", "samvirke", "varehandel", "byggevare", "bygg",
    "bærekraft", "miljø", "emballasje", "avfall",
    "sirkulær", "gjenvinning", "eøs", "esg",
    "csrd", "taksonomi", "aktsomhet", "arbeidsmiljø",
    "plan- og bygningsloven", "avhendingslova"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
}

# =============================
# LOGGING
# =============================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
log = logging.getLogger("lovsonar")

# =============================
# DATABASE
# =============================

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
        # Sjekker både gammel tabell (seen_items) og ny (seen) for sikkerhets skyld
        try:
            return con.execute("SELECT 1 FROM seen WHERE id = ?", (id_,)).fetchone() is not None
        except sqlite3.OperationalError:
            return False

def mark_seen(id_, title, source, url):
    with get_db() as con:
        con.execute(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?, ?)",
            (id_, title, source, url, datetime.utcnow().isoformat())
        )

# =============================
# HJELPEFUNKSJONER
# =============================

def clean_text(txt):
    if not txt:
        return ""
    txt = unescape(txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()

def is_relevant(text):
    if not text: return False
    t = text.lower()
    return any(k in t for k in KEYWORDS)

# =============================
# REGJERINGEN.NO (HTML-SKRAPING)
# =============================

def check_regjeringen():
    # Dette er nettsidene vi leser direkte (ikke RSS)
    urls = {
        "Høringer": "https://www.regjeringen.no/no/aktuelt/horinger/id1763/",
        "NOU": "https://www.regjeringen.no/no/dokument/nou-ar/id1767/",
        "Proposisjoner": "https://www.regjeringen.no/no/dokument/proposisjoner-og-meldinger/id1754/",
        "EØS-notater": "https://www.regjeringen.no/no/tema/europapolitikk/eos-notater/id669358/"
    }

    session = requests.Session()
    session.headers.update(HEADERS)

    for source, url in urls.items():
        log.info(f"🌐 Sjekker Regjeringen: {source}...")
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")
            
            # Finner lenker i hovedinnholdet
            main_content = soup.find(id="mainContent") or soup
            links = main_content.select("h3 a, .teaser-content a, li a") # Bredere søk

            count = 0
            for a in links:
                title = clean_text(a.get_text())
                href = a.get("href", "")

                if len(title) < 10: continue # Ignorer korte lenker ("les mer" osv)
                
                # Fiks relative lenker
                if href.startswith("/"):
                    href = "https://www.regjeringen.no" + href
                
                # Filtrer uinteressante lenker
                if "javascript" in href or "#" in href: continue

                # Sjekk relevans mot nøkkelord
                if not is_relevant(title):
                    continue

                item_id = href # Bruker URL som ID

                if is_seen(item_id):
                    continue

                log.info(f"✅ NYTT TREFF: {title}")
                mark_seen(item_id, title, f"Regjeringen ({source})", href)
                count += 1
            
            if count == 0:
                log.info(f"   Ingen nye relevante saker funnet i {source}.")

        except Exception as e:
            log.error(f"❌ Feil mot {source}: {e}")

# =============================
# STORTINGET (API)
# =============================

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

        for sak in saker:
            title = clean_text(sak.get("tittel", ""))
            
            # Sjekk relevans
            if not is_relevant(title):
                continue

            item_id = f"stortinget-{sak['id']}"
            
            if is_seen(item_id):
                continue

            url = f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={sak['id']}"
            log.info(f"✅ NYTT TREFF STORTINGET: {title}")
            mark_seen(item_id, title, "Stortinget", url)

    except Exception as e:
        log.error(f"❌ Feil mot Stortinget: {e}")

# =============================
# MAIN
# =============================

def main():
    setup_db()
    check_regjeringen()
    check_stortinget()
    log.info("🏁 Ferdig for denne gang.")

if __name__ == "__main__":
    main()
