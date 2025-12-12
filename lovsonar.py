"""
Lovsonar - Fiks for blokkering (User-Agent)
"""
import requests
import feedparser
import json
import os

# TVUNGET TREFF PÅ DISSE ORDENE (DEBUG):
KEYWORDS = ["om", "i", "og", "er", "har", "til"] 
RSS_URL = "https://www.regjeringen.no/no/aktuelt/rss/id2581966/"
OUTPUT_FILE = "nye_saker.json"

# HER ER FIKSEN: Vi later som vi er en vanlig nettleser
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

def matches(text):
    if not text: return False
    return any(k in text.lower() for k in KEYWORDS)

def scan():
    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    print("!!! TESTER MED NY LEGITIMASJON (USER-AGENT) !!!")
    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    
    items = []
    
    # 1. SJEKKER REGJERINGEN (MED HEADERS)
    print(f"DEBUG: Laster RSS fra {RSS_URL}...")
    try:
        # Vi henter innholdet manuelt først for å bruke headers
        resp = requests.get(RSS_URL, headers=HEADERS, timeout=10)
        feed = feedparser.parse(resp.content)
        print(f"DEBUG: Fant {len(feed.entries)} saker i feeden.")
        
        for e in feed.entries:
            t = e.get('title', '')
            if matches(t):
                # Vi tar bare med de 5 første for å ikke spamme loggen
                if len(items) < 5:
                    print(f"DEBUG Treff Regjeringen: {t}")
                items.append({'type': 'Høring', 'title': t, 'link': e.get('link',''), 'source': 'Regjeringen'})
    except Exception as e:
        print(f"DEBUG ERROR RSS: {e}")

    # 2. SJEKKER STORTINGET (MED HEADERS)
    print("DEBUG: Sjekker Stortinget...")
    try:
        sesj_url = "https://data.stortinget.no/eksport/sesjoner?format=json"
        sesj_resp = requests.get(sesj_url, headers=HEADERS, timeout=10)
        session_id = sesj_resp.json()['sesjon_liste'][-1]['id']
        print(f"DEBUG: Gjeldende sesjon er {session_id}")
        
        url = f"https://data.stortinget.no/eksport/saker?sesjonid={session_id}&format=json"
        data = requests.get(url, headers=HEADERS, timeout=15).json()
        saker = data.get('saker_liste', [])
        print(f"DEBUG: Fant {len(saker)} saker totalt hos Stortinget.")
        
        for s in saker:
            if 'proposisjon' in s.get('dokumentgruppe', '').lower():
                t = s.get('tittel', '')
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
