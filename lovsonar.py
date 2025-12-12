"""
Lovsonar - Overvåker høringer og proposisjoner (Tidlig varsling)
Kjører automatisk på GitHub Actions.
"""
import sqlite3
import requests
import feedparser
import logging
from datetime import datetime
import json
import os

# ===========================================
# KONFIGURASJON
# ===========================================
# Nøkkelord tilpasset din profil (Obs Bygg/Handel, AI, Juss, Økonomi):
KEYWORDS = [
    "varehandel", "forbruker", "konkurranse", "arbeidsmiljø", 
    "kunstig intelligens", "digital", "data", "personvern", 
    "markedsføring", "bygg", "plan- og bygningsloven", "sirkulær",
    "bærekraft", "åpenhetsloven", "finansavtaleloven", "betalingstjenester"
]

RSS_URL_HORINGER = "https://www.regjeringen.no/no/aktuelt/rss/id2581966/"
DB_PATH = "lovsonar_seen.db"
OUTPUT_FILE = "nye_saker.json"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ===========================================
# DATABASE & HJELPEFUNKSJONER
# ===========================================
def setup_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seen_items (
            item_id TEXT PRIMARY KEY,
            source TEXT,            title TEXT,            date_seen TEXT        )
    """)
    conn.commit()
    conn.close()

def is_seen(item_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_as_seen(item_id, source, title):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO seen_items (item_id, source, title, date_seen) VALUES (?, ?, ?, ?)",
        (item_id, source, title, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def matches_keywords(text):
    if not text: return False
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in KEYWORDS)

# ===========================================
# 1. HØRINGER (Regjeringen)
# ===========================================
def get_horinger():
    logger.info("Sjekker høringer...")
    try:
        feed = feedparser.parse(RSS_URL_HORINGER)
        new_items = []
        for entry in feed.entries:
            item_id = entry.get('link', entry.get('id', ''))
            if not item_id or is_seen(item_id): continue
            
            title = entry.get('title', '')
            description = entry.get('description', entry.get('summary', ''))
            link = entry.get('link', '')
            
            if matches_keywords(f"{title} {description}"):
                new_items.append({
                    'type': 'Høring',
                    'title': title,
                    'description': description[:300],
                    'link': link,
                    'source': 'Regjeringen'
                })
                mark_as_seen(item_id, 'regjeringen', title)
        return new_items
    except Exception as e:
        logger.error(f"Feil ved høringer: {e}")
        return []

# ===========================================
# 2. PROPOSISJONER (Stortinget)
# ===========================================
def get_current_session():
    try:
        resp = requests.get("https://data.stortinget.no/eksport/sesjoner?format=json", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data['sesjon_liste'][-1]['id'] if 'sesjon_liste' in data else None
    except: return None

def get_proposisjoner():
    logger.info("Sjekker proposisjoner...")
    session_id = get_current_session()
    if not session_id: return []
    
    try:
        url = f"https://data.stortinget.no/eksport/saker?sesjonid={session_id}&format=json"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        new_items = []
        for sak in data.get('saker_liste', []):
            if 'proposisjon' not in sak.get('dokumentgruppe', '').lower(): continue
            
            sak_id = str(sak.get('id', ''))
            if is_seen(sak_id): continue
            
            title = sak.get('tittel', '')
            if matches_keywords(title):
                new_items.append({
                    'type': 'Proposisjon',
                    'title': title,
                    'link': f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={sak_id}",
                    'source': 'Stortinget'
                })
                mark_as_seen(sak_id, 'stortinget', title)
        return new_items
    except Exception as e:
        logger.error(f"Feil ved proposisjoner: {e}")
        return []

# ===========================================
# KJØRING
# ===========================================
if __name__ == "__main__":
    setup_database()
    items = get_horinger() + get_proposisjoner()
    
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump({'count': len(items), 'items': items}, f, ensure_ascii=False)
    
    if items:
        print(f"Fant {len(items)} nye saker.")
    else:
        print("Ingen nye saker funnet.")
