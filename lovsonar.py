"""
Lovsonar - PRODUKSJONSVERSJON
Overvåker høringer og proposisjoner med User-Agent fix.
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
# DEBUG-ORD (For å teste at det virker nå):
KEYWORDS = ["om", "i", "og", "varehandel", "bygg", "ai"]

# VIKTIG: Bruker hoved-feeden for høringer (id1763) i stedet for den spesifikke vi testet
RSS_URL_HORINGER = "https://www.regjeringen.no/no/rss/Horinger/id1763/"
DB_PATH = "lovsonar_final.db"
OUTPUT_FILE = "nye_saker.json"

# HER ER NØKKELEN TIL SUKSESS (User-Agent):
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ===========================================
# DATABASE
# ===========================================
def setup_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seen_items (
            item_id TEXT PRIMARY KEY,
            source TEXT, title TEXT, date_seen TEXT
        )
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
        # Laster ned manuelt med headers først
        resp = requests.get(RSS_URL_HORINGER, headers=HEADERS, timeout=15)
        feed = feedparser.parse(resp.content)
        
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
def get_proposisjoner():
    logger.info("Sjekker proposisjoner...")
    try:
        # Henter sesjon med headers
        sesj_url = "https://data.stortinget.no/eksport/sesjoner?format=json"
        sesj_resp = requests.get(sesj_url, headers=HEADERS, timeout=10)
        # HER VAR FEILEN: 'sesjoner_liste' (flertall), ikke 'sesjon_liste'
        session_id = sesj_resp.json()['sesjoner_liste'][-1]['id']
        
        # Henter saker
        url = f"https://data.stortinget.no/eksport/saker?sesjonid={session_id}&format=json"
        data = requests.get(url, headers=HEADERS, timeout=15).json()
        
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
