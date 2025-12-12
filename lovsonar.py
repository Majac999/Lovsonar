"""
LOVSONAR - Proaktiv overvåking
Kilder:
1. Regjeringen.no (Høringer - Tidlig fase)
2. Stortinget.no (Proposisjoner - Konkret fase)
"""
import sqlite3
import requests
import feedparser
import logging
from datetime import datetime
import json
import os

# ===========================================
# 1. DINE SØKEORD (Endre disse!)
# ===========================================
# Her skriver du ordene du vil at sonaren skal lete etter.
KEYWORDS = [
    "bank", 
    "finans", 
    "teknologi", 
    "digital", 
    "kunstig intelligens", 
    "krypto", 
    "hvitvasking", 
    "bærekraft",
    "eu-direktiv",
    "forordning"
]

# ===========================================
# KONFIGURASJON (Ikke endre)
# ===========================================
RSS_URL_HORINGER = "https://www.regjeringen.no/no/aktuelt/rss/id2581966/"
DB_PATH = "lovsonar_seen.db"
OUTPUT_FILE = "nye_treff.json"

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
            source TEXT,
            title TEXT,
            date_seen TEXT
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
# MODUL 1: HØRINGER (Regjeringen)
# ===========================================
def get_horinger():
    logger.info("Søker i høringer...")
    try:
        feed = feedparser.parse(RSS_URL_HORINGER)
        new_items = []
        
        for entry in feed.entries:
            item_id = entry.get('link', entry.get('id', ''))
            if not item_id or is_seen(item_id): continue
            
            title = entry.get('title', '')
            description = entry.get('description', entry.get('summary', ''))
            link = entry.get('link', '')
            published = entry.get('published', '')
            
            # Sjekk om søkeord finnes i tittel eller beskrivelse
            if matches_keywords(f"{title} {description}"):
                new_items.append({
                    'type': '📢 Høring',
                    'title': title,
                    'description': description[:300] + "...",
                    'link': link,
                    'source': 'Regjeringen.no'
                })
                mark_as_seen(item_id, 'regjeringen', title)
        return new_items
    except Exception as e:
        logger.error(f"Feil ved høringer: {e}")
        return []

# ===========================================
# MODUL 2: PROPOSISJONER (Stortinget)
# ===========================================
def get_current_session():
    # Finner automatisk riktig sesjon (f.eks. 2024-2025)
    try:
        resp = requests.get("https://data.stortinget.no/eksport/sesjoner?format=json", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get('sesjon_liste'):
            return data['sesjon_liste'][-1]['id']
    except:
        return None

def get_proposisjoner():
    logger.info("Søker i proposisjoner...")
    session_id = get_current_session()
    if not session_id: return []
        
    url = f"https://data.stortinget.no/eksport/saker?sesjonid={session_id}&format=json"
    
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        new_items = []
        
        for sak in data.get('saker_liste', []):
            dok_gruppe = sak.get('dokumentgruppe', '').lower()
            if 'proposisjon' not in dok_gruppe: continue
            
            sak_id = str(sak.get('id', ''))
            title = sak.get('tittel', '')
            
            if is_seen(sak_id): continue
            
            if matches_keywords(title):
                new_items.append({
                    'type': '📜 Proposisjon (Lovforslag)',
                    'title': title,
                    'description': sak.get('henvisning', ''),
                    'link': f"https://www.stortinget.no/no/Saker-og-publikasjoner/Saker/Sak/?p={sak_id}",
                    'source': 'Stortinget'
                })
                mark_as_seen(sak_id, 'stortinget', title)
        return new_items
    except Exception as e:
        logger.error(f"Feil ved proposisjoner: {e}")
        return []

# ===========================================
# START PROGRAM
# ===========================================
def main():
    print("--- STARTER LOVSONAR ---")
    setup_database()
    
    hits = []
    hits.extend(get_horinger())
    hits.extend(get_proposisjoner())
    
    # Lagre resultat til fil for GitHub Actions
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump({'count': len(hits), 'items': hits}, f, ensure_ascii=False, indent=2)
    
    if hits:
        print(f"\n🔥 Fant {len(hits)} nye treff!")
        for item in hits:
            print(f"{item['type']}: {item['title']}")
    else:
        print("\n✅ Ingen nye treff i dag.")

if __name__ == "__main__":
    main()
