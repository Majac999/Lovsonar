#!/usr/bin/env python3
"""
LovSonar – overvåkning av relevante kilder fra regjeringen.no og Stortinget.
"""

import logging
import os
import re
import sqlite3
import sys
import smtplib
import time
import requests
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.header import Header
from html import unescape
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup  # krever bs4
import feedparser

# Prøv å importere pdf_leser (valgfritt)
try:
    import pdf_leser  # type: ignore
except ImportError:
    pdf_leser = None
    print("⚠️ ADVARSEL: Fant ikke pdf_leser.py. PDF-analyse vil ikke fungere.")

# ===========================================
# 1. KONFIGURASJON
# ===========================================

KW_SEGMENT = [
    "byggevare", "byggevarehus", "trelast", "jernvare", "lavpris",
    "discount", "billigkjede", "gjør-det-selv", "gds", "diy",
    "ombruk", "materialbank", "produktdatabase", "byggtjeneste",
    "varehandel", "konkurransetilsynet", "samvirkelag", "coop"
]

KW_TOPIC = [
    "bærekraft", "sirkulær", "gjenvinning", "miljøkrav", "taksonomi",
    "esg", "espr", "ecodesign", "ppwr", "cbam", "csrd", "csddd",
    "aktsomhet", "green claims", "grønnvasking", "reach", "clp",
    "pfas", "eudr", "epbd", "byggevareforordning", "cpr",
    "plastløftet", "emballasje", "klimaavgift", "digitale produktpass", "dpp",
    "arbeidsmiljøloven", "avhendingslova", "plan- og bygningsloven"
]

DB_PATH = "lovsonar_seen.db"

# RSS-kandidat-URLer per kilde (prøv i rekkefølge)
RSS_CANDIDATES = {
    "📢 Høringer": [
        "https://www.regjeringen.no/no/aktuelt/hoyringar/id1763/?rss=true",
        "https://www.regjeringen.no/no/aktuelt/horinger/id1763/?rss=true",
        "https://www.regjeringen.no/no/aktuelt/hoyringar/id1763/?ep=RSS",
        "https://www.regjeringen.no/no/aktuelt/horinger/id1763/?ep=RSS",
    ],
    "📚 NOU (Utredninger)": [
        "https://www.regjeringen.no/no/dokument/nou-ar/id1767/?rss=true",
        "https://www.regjeringen.no/no/dokument/nou-er/id1767/?rss=true",
        "https://www.regjeringen.no/no/dokument/nou-ar/id1767/?ep=RSS",
        "https://www.regjeringen.no/no/dokument/nou-er/id1767/?ep=RSS",
    ],
    "📜 Lovforslag/Prop": [
        "https://www.regjeringen.no/no/dokument/proposisjoner-og-meldinger/id1754/?rss=true",
        "https://www.regjeringen.no/no/dokument/proposisjoner-og-meldinger/id1754/?ep=RSS",
    ],
    "🇪🇺 EØS-notater": [
        "https://www.regjeringen.no/no/tema/europapolitikk/eos-notater/id669358/?rss=true",
        "https://www.regjeringen.no/no/tema/europapolitikk/eos-notater/id669358/?ep=RSS",
    ],
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "no-NO,no;q=0.9,en-US;q=0.8,en;q=0.7"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# ===========================================
# 2. HJELPEFUNKSJONER
# ===========================================

def get_http_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session

def clean_text(text):
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split()).strip()

def matches_composite_logic(text):
    if not text:
        return False
    text_lower = text.lower()
    has_segment = any(k in text_lower for k in KW_SEGMENT)
    has_topic   = any(k in text_lower for k in KW_TOPIC)
    return has_segment and has_topic

def is_old(date_obj, days=90):
    limit = datetime.utcnow() - timedelta(days=days)
    return date_obj < limit

# ===========================================
# 3. DATABASE
# ===========================================

def setup_database():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                item_id TEXT PRIMARY KEY,
                source TEXT,
                title TEXT,
                date_seen TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weekly_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                title TEXT,
                description TEXT,
                link TEXT,
                pub_date TEXT,
                excerpt TEXT,
                detected_at TEXT
            )
        """)
        conn.commit()

def purge_old_data(days_to_keep=180):
    cutoff = (datetime.utcnow() - timedelta(days=days_to_keep)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM seen_items WHERE date_seen < ?", (cutoff,))
        conn.execute("DELETE FROM weekly_hits WHERE detected_at < ?", (cutoff,))
        conn.commit()

def is_seen(item_id):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (str(item_id),)).fetchone() is not None

def register_hit(item_id, source, title, desc, link, pub_date, excerpt):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)",
                     (str(item_id), source, title, datetime.utcnow().isoformat()))
        conn.execute("""
            INSERT INTO weekly_hits (source, title, description, link, pub_date, excerpt, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (source, title, desc, link, pub_date.isoformat(), excerpt, datetime.utcnow().isoformat()))
        conn.commit()

# ===========================================
# 4. ANALYSE
# ===========================================

def analyze_item(source_name, title, description, link, pub_date, item_id):
    if not item_id:
        return
    if is_old(pub_date, days=180):
        return
    if is_seen(item_id):
        return

    full_text = f"{title} {description}"
    excerpt = description[:300] + "..."

    # PDF-sjekk: bare hvis lenken ender på .pdf
    should_check_pdf = pdf_leser and link.lower().endswith(".pdf")

    if should_check_pdf:
        logger.info(f"   🔎 Leser PDF: {title[:60]}")
        try:
            tilleggs_tekst = pdf_leser.hent_pdf_tekst(link, maks_sider=10)
            if tilleggs_tekst and "FEIL" not in tilleggs_tekst:
                full_text += " " + tilleggs_tekst
                excerpt = f"[PDF]: {tilleggs_tekst[:600]}..."
                time.sleep(1)  # høflighet
            else:
                logger.warning("   ⚠️ PDF ga tomt eller feil resultat: %s", link)
        except Exception as e:
            logger.warning("   ⚠️ Kunne ikke lese PDF: %s", e)

    if matches_composite_logic(full_text):
        logger.info(f"✅ TREFF! {title}")
        if "høring" in title.lower():
            title = "📢 [HØRING] " + title
        if "proposisjon" in title.lower():
            title = "📜 [PROP] " + title
        register_hit(item_id, source_name, title, description, link, pub_date, excerpt)
    else:
        # Marker som sett for å unngå re-prosessering
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)",
                         (str(item_id), source_name, title, datetime.utcnow().isoformat()))
            conn.commit()

# ===========================================
# 5. INNSAMLING (Regjeringen via RSS+HTML, Stortinget API)
# ===========================================

def get_publish_date(entry):
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6])
    return datetime.utcnow()

def fetch_rss_with_candidates(name, candidates, session):
    """Prøv flere URLer til en feed; returner feed (eller None)."""
    for url in candidates:
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 404:
                continue
            feed = feedparser.parse(resp.content)
            if getattr(feed, "bozo", False):
                continue
            if feed.entries:
                return feed
        except Exception:
            continue
    return None

def check_rss_feeds():
    session = get_http_session()
    for name, candidates in RSS_CANDIDATES.items():
        logger.info("📡 Sjekker %s ...", name)
        feed = fetch_rss_with_candidates(name, candidates, session)
        if not feed:
            logger.warning("⚠️ Fant ingen fungerende RSS for %s. Hopper over.", name)
            continue
        for entry in feed.entries:
            item_id = entry.get("id") or entry.get("guid") or entry.get("link")
            raw_desc = entry.get("summary") or entry.get("description") or ""
            pub_date = get_publish_date(entry)
            analyze_item(
                source_name=name,
                title=clean_text(entry.get("title", "")),
                description=clean_text(raw_desc),
                link=entry.get("link", ""),
                pub_date=pub_date,
                item_id=item_id,
            )

def check_regjeringen_nettside():
    """
    Fallback: henter høringer via HTML om RSS ikke fungerer.
    """
    url = "https://www.regjeringen.no/no/aktuelt/horinger/id1763/"
    name = "📢 Regjeringen (Høringer) HTML"
    logger.info(f"🌐 Sjekker {name} via nettsiden...")
    session = get_http_session()
    try:
        res = session.get(url, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "html.parser")
        links = soup.find_all("h3", class_="a-text-title")
        if not links:
            main_content = soup.find(id="mainContent") or soup
            links = main_content.find_all("a", href=True)

        count = 0
        for element in links:
            link_tag = element.find("a") if element.name != "a" else element
            if not link_tag or not link_tag.has_attr("href"):
                continue
            tittel = link_tag.get_text().strip()
            href = link_tag["href"]
            if len(tittel) < 10 or "javascript" in href.lower():
                continue
            if href.startswith("/"):
                full_url = "https://www.regjeringen.no" + href
            else:
                full_url = href
            if "/id" not in full_url and "/dokumenter/" not in full_url and "/aktuelt/" not in full_url:
                continue
            item_id = full_url
            analyze_item(
                source_name=name,
                title=tittel,
                description="Hentet fra Regjeringen.no",
                link=full_url,
                pub_date=datetime.utcnow(),
                item_id=item_id,
            )
            count += 1
        logger.info(f"   Fant {count} lenker på høringssiden.")
    except Exception as e:
        logger.error(f"❌ Feil ved lesing av nettside: {e}")

def check_stortinget():
    logger.info("🏛️ Sjekker Stortinget (API)...")
    session = get_http_session()
    try:
        sesjon_res = session.get("https://data.stortinget.no/eksport/sesjoner?format=json", timeout=10)
        sesjon_res.raise_for_status()
        innevaerende = sesjon_res.json().get("innevaerende_sesjon")
        if not innevaerende:
            logger.warning("⚠️ Fant ikke inneværende sesjon via API-et.")
            return
        sid = innevaerende.get("id")
        if not sid:
            logger.warning("⚠️ Sesjons-ID mangler i API-responsen.")
            return
        saker_res = session.get(
            f"https://data.stortinget.no/eksport/saker?sesjonid={sid}&format=json",
            timeout=10,
        )
        saker_res.raise_for_status()
        data = saker_res.json()
        saker = data.get("saker_liste", [])
        logger.info("   Fant %d saker på Stortinget. Analyserer...", len(saker))

        for sak in saker:
            dg = str(sak.get("dokumentgruppe") or "").lower()
            if any(x in dg for x in ["spørsmål", "interpellasjon", "referat", "skriftlig"]):
                continue
            item_id = f"STORTINGET-{sak.get('id')}"
            tittel = sak.get("tittel", "Uten tittel")
            tema = sak.get("tema", "") or ""
            analyze_item(
                source_name="🏛️ Stortingssak",
                title=clean_text(tittel),
                description=f"Type: {dg or 'ukjent'}. Tema: {tema}.",
                link=f"https://stortinget.no/sak/{sak.get('id')}",
                pub_date=datetime.utcnow(),
                item_id=item_id,
            )
    except Exception as e:
        logger.error("❌ Feil mot Stortinget API: %s", e)

# ===========================================
# 6. RAPPORTERING
# ===========================================

def send_weekly_report():
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        rows = conn.execute(
            """
            SELECT source, title, link, excerpt, pub_date
            FROM weekly_hits
            WHERE detected_at >= ?
            ORDER BY pub_date DESC
        """,
            (cutoff,),
        ).fetchall()

    if not rows:
        logger.info("Ingen treff denne uken – rapport sendes ikke.")
        return

    md_chunks = [
        f"# 🛡️ LovSonar: {len(rows)} treff",
        f"Rapportdato: {datetime.now():%d.%m.%Y}\n",
    ]
    for source, title, link, excerpt, pub_date in rows:
        try:
            d_str = datetime.fromisoformat(pub_date).strftime("%d.%m")
        except Exception:
            d_str = "N/A"
        md_chunks.append(f"## {title}")
        md_chunks.append(f"**Kilde:** {source} | **Dato:** {d_str}")
        md_chunks.append(f"[Les saken]({link})\n")
        md_chunks.append(f"> {excerpt[:800]}...\n")
        md_chunks.append("---")

    company_context = os.environ.get("COMPANY_CONTEXT") or "Ingen profil definert."
    md_chunks.append("\n### 🤖 ANALYSE-KONTEKST (Kopier til AI)")
    md_chunks.append(company_context)
    md_chunks.append("\n**OPPGAVE:** Analyser sakene over. Påvirker dette Obs BYGG/Coop?")

    body = "\n".join(md_chunks)

    email_user = os.environ.get("EMAIL_USER")
    email_pass = os.environ.get("EMAIL_PASS")
    email_to = os.environ.get("EMAIL_RECIPIENT") or email_user
    smtp_host = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "465"))
    use_starttls = os.environ.get("EMAIL_USE_STARTTLS", "false").lower() == "true"

    if not (email_user and email_pass and email_to):
        logger.warning("⚠️ E-postvariabler mangler – rapporten sendes ikke.")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(f"LovSonar: {len(rows)} treff", "utf-8")
    msg["From"] = email_user
    msg["To"] = email_to

    try:
        if use_starttls:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(email_user, email_pass)
                smtp.sendmail(email_user, [email_to], msg.as_string())
        else:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as smtp:
                smtp.login(email_user, email_pass)
                smtp.sendmail(email_user, [email_to], msg.as_string())
        logger.info("✉️  Ukentlig rapport sendt til %s.", email_to)
    except Exception as e:
        logger.error("❌ Klarte ikke å sende e-post: %s", e)

# ===========================================
# 7. HOVEDKJØRING
# ===========================================

def main():
    setup_database()
    purge_old_data()
    check_rss_feeds()            # prøver RSS (med kandidater)
    check_regjeringen_nettside() # fallback HTML for høringer
    check_stortinget()
    send_weekly_report()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Avsluttet av bruker.")
        sys.exit(0)
