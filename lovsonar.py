"""
Lovsonar - DEBUG VERSJON
"""
import requests
import feedparser
import json
import os

# TVUNGET TREFF PÅ DISSE ORDENE:
KEYWORDS = ["om", "i", "og", "er", "har", "til"] 
RSS_URL = "https://www.regjeringen.no/no/aktuelt/rss/id2581966/"
OUTPUT_FILE = "nye_saker.json"

def matches(text):
    if not text: return False
    return any(k in text.lower() for k in KEYWORDS)

def scan():
    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    print("!!! STARTER DEBUG MODUS - NÅ SKJER DET TING !!!")
    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    
    items = []
    
    # 1. SJEKKER REGJERINGEN
    print(f"DEBUG: Laster RSS fra {RSS_URL}...")
    try:
        feed = feedparser.parse(RSS_URL)
        print(f"DEBUG: Fant {len(feed.entries)} saker i feeden.")
        for e in feed.entries:
            t = e.get('title', '')
            print(f"DEBUG Tittel: {t}")
            if matches(t):
                items.append({'type': 'Høring', 'title': t, 'link': e.get('link',''), 'source': 'Regjeringen'})
    except Exception as e:
        print(f"DEBUG ERROR RSS: {e}")

    # 2. SJEKKER STORTINGET
    print("DEBUG: Sjekker Stortinget...")
    try:
        sesj = requests.get("https://data.stortinget.no/eksport/sesjoner?format=json").json()['sesjon_liste'][-1]['id']
        url = f"https://data.stortinget.no/eksport/saker?sesjonid={sesj}&format=json"
        print(f"DEBUG: Henter fra {url}")
        data = requests.get(url).json()
        saker = data.get('saker_liste', [])
        print(f"DEBUG: Fant {len(saker)} saker totalt.")
        
        for s in saker:
            if 'proposisjon' in s.get('dokumentgruppe', '').lower():
                t = s.get('tittel', '')
                print(f"DEBUG Prop: {t}")
                if matches(t):
                    items.append({'type': 'Prop', 'title': t, 'link': '', 'source': 'Stortinget'})
    except Exception as e:
        print(f"DEBUG ERROR STORTING: {e}")

    # LAGRE RESULTAT
    print(f"DEBUG: Totalt antall treff som lagres: {len(items)}")
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump({'count': len(items), 'items': items}, f, ensure_ascii=False)

if __name__ == "__main__":
    scan()
