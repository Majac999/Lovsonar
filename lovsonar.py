#!/usr/bin/env python3
"""
LovSonar – Proff versjon (v3.1)
Optimalisert basert på code review:
- Safer PDF text extraction
- Robust URL joining
- Increased pagination limits
"""

import logging
import sqlite3
import requests
import re
import os
import io
import smtplib
from datetime import datetime, timedelta
from html import unescape
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from email.mime.text import MIMEText
from email.header import Header

# =============================
# KONFIGURASJON
# =============================

DB_PATH = "lovsonar_seen.db"

KEYWORDS = [
    "coop", "samvirke", "varehandel", "byggevare", "bygg",
    "bærekraft", "miljø", "emballasje", "avfall",
    "sirkulær", "gjenvinning", "eøs", "esg",
    "csrd", "taksonomi", "aktsomhet", "arbeidsmiljø",
    "plan- og bygningsloven", "avhendingslova", "håndverkertjenesteloven",
    "teknisk forskrift", "tek17", "dok", "forbrukervern", "konkurransetilsynet"
]

def get_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    })
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
log = logging.getLogger("lovsonar")

# =============================
# DATABASE & HJELPEFUNKSJONER
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
        return con.execute("SELECT 1 FROM seen WHERE id = ?", (id_,)).fetchone() is not None

def mark_seen(id_, title, source, url):
    with get_db() as con:
        try:
            con.execute(
                "INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?, ?)",
                (id_, title, source, url, datetime.utcnow().isoformat())
            )
        except Exception as e:
            log.error(f"Database error: {e}")

def clean_text(txt):
    if not txt: return ""
    txt = unescape(txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()

def contains_keyword(text):
    if not text: return False
    text_lower = text.lower()
    for k in KEYWORDS:
        # Ordgrenser for korte ord (<= 3 tegn) for å unngå støy
        if len(k) <= 3:
            pattern = r'\b' + re.escape(k) + r'\b'
            if re.search(pattern, text_lower):
                return True
        elif k in text_lower:
            return True
    return False

# =============================
# DYBDEANALYSE (PDF + ARTIKKEL)
# =============================

def scan_pdf_content(pdf_url, session):
    try:
        log.info(f"   📄 Laster ned PDF: {pdf_url.split('/')[-1]}...")
        r = session.get(pdf_url, timeout=20)
        r.raise_for_status()
        f = io.BytesIO(r.content)
        reader = PdfReader(f)
        
        full_text = ""
        # Leser inntil 15 sider
        for i, page in enumerate(reader.pages):
            if i >= 15: break
            # Sikrere tekstuthenting (som foreslått i review)
            text = page.extract_text()
            full_text += (text or "").strip() + " "
            
        if contains_keyword(full_text):
            log.info("   🎯 TREFF I PDF!")
            return True
    except Exception as e:
        log.warning(f"   ⚠️ PDF-feil (hopper over): {e}")
    return False

def deep_scan_article(url, session):
    try:
        r = session.get(url, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        
        # Fallback selektorer for innhold
        main = soup.find(id="mainContent") or soup.find("main") or soup.find("div", class_="main-content") or soup
        
        if contains_keyword(main.get_text()):
            log.info("   🎯 Treff i artikkeltekst!")
            return True

        pdf_links = main.select("a[href$='.pdf']")
        for link in pdf_links:
            href = link.get('href')
            if href:
                # Sikrere URL-sammenslåing
                pdf_url = urljoin(url, href)
                if scan_pdf_content(pdf_url, session):
                    return True
    except Exception as e:
        log.warning(f"   ⚠️ Feil ved dybdesjekk: {e}")
    return False

# =============================
# INNSAMLING
# =============================

def check_regjeringen():
    urls = {
        "Høringer": "https://www.regjeringen.no/no/dokument/hoyringar/id1763/",
        "NOU": "https://www.regjeringen.no/no/dokument/nou-ar/id1767/",
        "Proposisjoner": "https://www.regjeringen.no/no/dokument/proposisjoner-og-meldinger/id1754/",
        "EØS-notater": "https://www.regjeringen.no/no/tema/europapolitikk/eos-notater/id669358/"
    }
    
    session = get_session()

    for source, list_url in urls.items():
        log.info(f"🌐 Sjekker Regjeringen: {source}...")
        try:
            r = session.get(list_url, timeout=20)
            soup = BeautifulSoup(r.text, "html.parser")
            main_content = soup.find(id="mainContent") or soup.find("ul", class_="result-list") or soup
            links = main_content.select("h3 a, .teaser-content a, li.result-item h2 a, li a")
            
            count = 0
            for a in links[:20]:
                title = clean_text(a.get_text())
                href = a.get("href", "")
                
                if len(title) < 5 or "javascript" in href: continue
                # Sikrere URL
                full_url = urljoin(list_url, href)
                
                # Sjekk også ingress/teaser hvis mulig
                ingress = ""
                parent = a.find_parent("li") or a.find_parent("div")
                if parent:
                    teaser = parent.select_one(".teaser-content, .intro, p")
                    if teaser: ingress = clean_text(teaser.get_text())

                combined_text = f"{title} {ingress}"
                
                if is_seen(full_url): continue

                hit = False
                hit_type = ""

                if contains_keyword(combined_text):
                    hit = True
                    hit_type = "Tittel/Ingress"
                else:
                    if deep_scan_article(full_url, session):
                        hit = True
                        hit_type = "Innhold/PDF"

                if hit:
                    log.info(f"✅ TREFF ({hit_type}): {title}")
                    mark_seen(full_url, title, f"Regjeringen ({source})", full_url)
                    count += 1
                else:
                    mark_seen(full_url, title, "Ignorert", full_url)
            
            if count > 0: log.info(f"   Lagret {count} nye saker.")

        except Exception as e:
            log.error(f"❌ Feil mot {source}: {e}")

def check_stortinget():
    log.info("🏛️ Sjekker Stortinget API...")
    session = get_session()
    
    try:
        ses_resp = session.get("https://data.stortinget.no/eksport/sesjoner?format=json", timeout=10)
        sid = ses_resp.json()["innevaerende_sesjon"]["id"]
        
        page = 1
        total_count = 0
        max_pages = 20 # Økt grense for sikkerhets skyld
        
        while page <= max_pages:
            api_url = f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&page={page}&pagesize=500&format=json"
            r = session.get(api_url, timeout=15)
            data = r.json()
            saker = data.get("saker_liste", [])
            
            if not saker: break 

            for sak in saker:
                title = clean_text(sak.get("tittel", ""))
                short_title = clean_text(sak.get("korttittel", ""))
                combined_text = f"{title} {short_title}"
                item_id = f"stortinget-{sak['id']}"
                
                if is_seen(item_id): continue 
                
                if contains_keyword(combined_text):
                    url = f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={sak['id']}"
                    log.info(f"✅ TREFF STORTINGET: {title}")
                    mark_seen(item_id, title, "Stortinget", url)
                    total_count += 1
                else:
                    mark_seen(item_id, title, "Ignorert", "")
            
            if len(saker) < 500: break
            page += 1

        if total_count > 0: log.info(f"   Fant {total_count} saker.")
        
    except Exception as e:
        log.error(f"❌ Feil mot Stortinget: {e}")

# =============================
# RAPPORT
# =============================

def send_weekly_report():
    log.info("📧 Lager ukesrapport...")
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    
    with get_db() as con:
        rows = con.execute("""
            SELECT title, source, url, date_seen 
            FROM seen 
            WHERE date_seen >= ? 
            AND source NOT LIKE 'Ignorert%'
            ORDER BY date_seen DESC
        """, (cutoff,)).fetchall()

    if not rows:
        log.info("Ingen nye relevante saker denne uken.")
        return

    msg_lines = [f"Hei! LovSonar-rapport uke {datetime.now().isocalendar()[1]}."]
    msg_lines.append(f"Fant {len(rows)} saker (Deep Scan):\n")
    
    for r in rows:
        title, source, url, date = r
        try: d_str = datetime.fromisoformat(date).strftime('%d.%m')
        except: d_str = "?"
        msg_lines.append(f"🔹 {title}")
        msg_lines.append(f"   Kilde: {source} ({d_str})")
        msg_lines.append(f"   Lenke: {url}\n")
    
    msg_lines.append("\n---\nMvh LovSonar Bot 🤖")
    body = "\n".join(msg_lines)

    email_user = os.environ.get("EMAIL_USER")
    email_pass = os.environ.get("EMAIL_PASS")
    email_to = os.environ.get("EMAIL_RECIPIENT", email_user)

    if email_user and email_pass:
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(f"LovSonar: {len(rows)} saker", "utf-8")
            msg["From"] = email_user
            msg["To"] = email_to
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(email_user, email_pass)
                server.send_message(msg)
            log.info("✅ E-post sendt!")
        except Exception as e:
            log.error(f"❌ E-post feilet: {e}")
    else:
        print(body)

def main():
    setup_db()
    mode = os.environ.get("LOVSONAR_MODE", "daily").lower()
    
    if mode == "weekly":
        send_weekly_report()
    else:
        check_regjeringen()
        check_stortinget()
        log.info("🏁 Ferdig.")

if __name__ == "__main__":
    main()
