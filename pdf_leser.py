import io
import requests
from pypdf import PdfReader
import re

def hent_pdf_tekst(pdf_url, maks_sider=10):
    try:
        headers = {"User-Agent": "LovSonar/PDF-Bot 1.0"}
        response = requests.get(pdf_url, headers=headers, timeout=10)
        
        # Sjekk om det faktisk er en PDF (eller content-type sier det)
        is_pdf = response.headers.get("Content-Type", "").lower().find("pdf") != -1
        if not is_pdf and not pdf_url.lower().endswith(".pdf"):
            return "" # Ikke en PDF, vi ignorerer den stille

        pdf_fil = io.BytesIO(response.content)
        leser = PdfReader(pdf_fil)
        
        full_tekst = []
        lese_grense = min(len(leser.pages), maks_sider)

        for i in range(lese_grense):
            tekst = leser.pages[i].extract_text()
            if tekst: full_tekst.append(tekst)

        return vask_tekst("\n".join(full_tekst))
    except Exception:
        return "" # Ved feil returnerer vi tom tekst så programmet ikke krasjer

def vask_tekst(tekst):
    if not tekst: return ""
    tekst = re.sub(r'\n+', '\n', tekst)
    tekst = re.sub(r'\s+', ' ', tekst)
    return tekst.strip()
