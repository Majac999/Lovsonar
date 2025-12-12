"""
Lovsonar - CONNECTION TEST
Vi sjekker om døren er låst (Statuskoder).
"""
import requests
import feedparser

# Vi bruker headers som ser ut som en vanlig PC
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, application/xml, text/html'
}

def test_connection():
    print("!!! STARTER FORBINDELSESTEST !!!")
    
    # ------------------------------------------------
    # 1. TESTER REGJERINGEN
    # ------------------------------------------------
    rss_url = "https://www.regjeringen.no/no/aktuelt/rss/id2581966/"
    print(f"\n1. Kobler til Regjeringen ({rss_url})...")
    try:
        resp = requests.get(rss_url, headers=HEADERS, timeout=15)
        print(f"STATUSKODE: {resp.status_code}") # 200 er bra, 403 er blokkert
        
        if resp.status_code == 200:
            print("   -> Tilkobling vellykket!")
            print(f"   -> Lastet ned {len(resp.content)} bytes.")
            # Prøv å parse
            feed = feedparser.parse(resp.content)
            print(f"   -> Feedparser fant {len(feed.entries)} elementer.")
        else:
            print("   -> FEIL: Serveren slapp oss ikke inn.")
            print(f"   -> Svar fra server: {resp.text[:200]}...") # Vis starten av feilmeldingen
            
    except Exception as e:
        print(f"   -> KRITISK FEIL (Regjeringen): {e}")

    # ------------------------------------------------
    # 2. TESTER STORTINGET
    # ------------------------------------------------
    storting_url = "https://data.stortinget.no/eksport/sesjoner?format=json"
    print(f"\n2. Kobler til Stortinget ({storting_url})...")
    try:
        resp = requests.get(storting_url, headers=HEADERS, timeout=15)
        print(f"STATUSKODE: {resp.status_code}")
        
        if resp.status_code == 200:
            print("   -> Tilkobling vellykket!")
            data = resp.json()
            # Sjekker om nøkkelen finnes
            if 'sesjon_liste' in data:
                 print(f"   -> Fant sesjon_liste! Siste sesjon: {data['sesjon_liste'][-1]['id']}")
            else:
                 print("   -> JSON er gyldig, men mangler 'sesjon_liste'.")
                 print(f"   -> Hele svaret: {data}")
        else:
            print("   -> FEIL: Serveren slapp oss ikke inn.")
            print(f"   -> Svar fra server: {resp.text[:200]}...")

    except Exception as e:
        print(f"   -> KRITISK FEIL (Stortinget): {e}")

if __name__ == "__main__":
    test_connection()
