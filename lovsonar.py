#!/usr/bin/env python3
"""
LovSonar v1.0 - Strategisk Fremtidsovervåkning
Byggevarebransjen

Fokus: Overvåker FREMTIDIGE reguleringer (ikke gjeldende lover)
- Norske forslag: NOU-er, Stortingsforslag, høringer
- EU-direktiver: Green Deal, ESPR, PPWR, DPP
- Regulatoriske trender i bærekraft
"""

import os
import json
import hashlib
import smtplib
import re
import asyncio
import aiohttp
import logging
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dataclasses import dataclass, field, asdict
from typing import Optional
from collections import Counter
from bs4 import BeautifulSoup
import feedparser

# --- KONFIGURASJON ---

@dataclass
class Signal:
    """Et fremtidssignal (NOU, forslag, høring, EU-direktiv)."""
    type: str  # "nou", "stortingsforslag", "horing", "eu_direktiv", "nyhet"
    kilde: str
    kategori: str
    tittel: str
    url: str
    sammendrag: str = ""
    keywords: list = field(default_factory=list)
    prioritet: int = 3  # 1=Kritisk, 2=Viktig, 3=Info
    sannsynlighet: str = "Ukjent"  # Høy/Medium/Lav
    konsekvens: str = "Ukjent"  # Høy/Medium/Lav
    tidshorisont: str = "Ukjent"  # <1år, 1-3år, >3år
    deadline: str = ""
    publisert: Optional[date] = None

    def __post_init__(self):
        """Beregn prioritet."""
        # Høy prioritet hvis høring med frist
        if self.type == "horing" and self.deadline:
            self.prioritet = 1
        # Høy prioritet hvis kritiske nøkkelord
        kritiske = {"frist", "høringsfrist", "ikrafttredelse", "krav", "forbud", "pålegg"}
        if any(k in str(self.keywords).lower() for k in kritiske):
            self.prioritet = min(self.prioritet, 2)
        # EU-direktiver er ofte viktige
        if self.type == "eu_direktiv":
            self.prioritet = 2


# Strategiske kilder - FREMTIDSRETTEDE
SONAR_KILDER = {
    "regjeringen_dokumenter": {
        "url": "https://www.regjeringen.no/no/dokument/id2000006/?type=rss",
        "type": "rss",
        "kategori": "proposisjon"
    },
    "regjeringen_nyheter": {
        "url": "https://www.regjeringen.no/no/aktuelt/nyheter/id2006120/?type=rss",
        "type": "rss",
        "kategori": "nyhet"
    },
    "regjeringen_horinger_aktive": {
        "url": "https://www.regjeringen.no/no/dokument/hoeringer/id438325/?type=rss",
        "type": "rss",
        "kategori": "horing"
    }
}

# Nøkkelord tilpasset FREMTIDIGE reguleringer
FREMTID_KEYWORDS = {
    "sirkulær_økonomi": [
        "sirkulær", "produktpass", "dpp", "digital produktpass", "reparerbarhet",
        "levetid", "modularitet", "resirkulering", "gjenvinning", "gjenbruk",
        "ecodesign", "espr", "økodesign"
    ],
    "emballasje": [
        "emballasje", "ppwr", "packaging", "plastemballasje", "gjenbruksemballasje",
        "emballasjeforordningen", "produsentansvar", "pant"
    ],
    "klima_energi": [
        "klimagass", "co2", "karbonavtrykk", "klimanøytral", "nullutslipp",
        "grønn", "fornybar", "energimerking", "energikrav"
    ],
    "kjemikalier": [
        "reach", "svhc", "farlige stoffer", "kjemikalier", "biocid", "clp",
        "mikroplast", "pfas", "evige kjemikalier"
    ],
    "sporbarhet": [
        "sporbarhet", "dokumentasjon", "leverandørkjede", "due diligence",
        "åpenhet", "menneskerettigheter", "tømmer", "eutr", "konfliktmineraler"
    ],
    "grønnvasking": [
        "grønnvasking", "greenwashing", "miljøpåstand", "bærekraftspåstand",
        "markedsføring", "villedende", "dokumenterbar"
    ],
    "bygg_produkter": [
        "byggevare", "byggprodukt", "ce-merking", "dok", "produktdokumentasjon",
        "tek", "byggteknisk", "energieffektiv"
    ]
}

ALLE_FREMTID_KEYWORDS = []
for kategori_keywords in FREMTID_KEYWORDS.values():
    ALLE_FREMTID_KEYWORDS.extend(kategori_keywords)

CONFIG = {
    "cache_file": "lovsonar_cache.json",
    "request_timeout": 30,
    "retry_attempts": 3,
    "retry_delay": 2,
    "rate_limit_delay": 0.5,
    "max_entries": 20,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("LovSonar")

# Norske måneder
NORWEGIAN_MONTHS = {
    "januar": 1, "februar": 2, "mars": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12
}


# --- HJELPEFUNKSJONER ---

def parse_norsk_dato(text: str) -> Optional[date]:
    """Parser norske datoformater."""
    if not text:
        return None
    text_lower = text.lower()

    # dd.mm.yyyy
    m1 = re.search(r'\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b', text_lower)
    if m1:
        try:
            d, m, y = map(int, m1.groups())
            return date(y, m, d)
        except ValueError:
            pass

    # d. måned yyyy
    m2 = re.search(r'\b(\d{1,2})\.\s*([a-zæøå]+)\s+(\d{4})\b', text_lower)
    if m2:
        try:
            d = int(m2.group(1))
            month_word = m2.group(2)
            y = int(m2.group(3))
            month_num = NORWEGIAN_MONTHS.get(month_word)
            if month_num:
                return date(y, month_num, d)
        except ValueError:
            pass

    return None


def ekstraher_deadline(text: str) -> Optional[str]:
    """Finn frister/deadlines."""
    if not text:
        return None

    patterns = [
        r'(høringsfrist|frist)\s*[:\-]?\s*\d{1,2}\.\d{1,2}\.\d{4}',
        r'(trer i kraft|ikrafttredelse)\s*[:\-]?\s*\d{1,2}\.\d{1,2}\.\d{4}',
        r'(høringsfrist|frist)\s*[:\-]?\s*\d{1,2}\.\s*[a-zæøå]+\s+\d{4}',
        r'(senest|innen)\s+\d{1,2}\.\s*[a-zæøå]+\s+\d{4}',
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0)

    return None


def estimat_tidshorisont(text: str, publisert: Optional[date]) -> str:
    """Estimat når reguleringen kan tre i kraft."""
    if not text:
        return "Ukjent"

    text_lower = text.lower()

    # Sjekk eksplisitte tidspunkter
    if re.search(r'(2026|umiddelbar|straks|med virkning fra)', text_lower):
        return "<1 år"
    if re.search(r'(2027|2028)', text_lower):
        return "1-3 år"
    if re.search(r'(2029|2030|langsiktig)', text_lower):
        return ">3 år"

    # Basert på type dokument
    if "høring" in text_lower or "forslag" in text_lower:
        return "1-3 år"
    if "nou" in text_lower or "utredning" in text_lower:
        return ">3 år"

    return "Ukjent"


def vurder_sannsynlighet(text: str, type: str) -> str:
    """Vurder sannsynlighet for at forslaget blir vedtatt."""
    if not text:
        return "Ukjent"

    text_lower = text.lower()

    # Høy sannsynlighet
    if type == "proposisjon" or "regjeringen foreslår" in text_lower:
        return "Høy"
    if "eu-direktiv" in text_lower or "eu-forordning" in text_lower:
        return "Høy"

    # Medium sannsynlighet
    if type == "horing" or "høring" in text_lower:
        return "Medium"

    # Lav sannsynlighet
    if type == "stortingsforslag" or "representantforslag" in text_lower:
        return "Lav"

    return "Ukjent"


def vurder_konsekvens(keywords: list, kategori: str) -> str:
    """Vurder konsekvens for byggevarebransjen."""
    kw_str = " ".join(keywords).lower()

    # Høy konsekvens
    hoye_impact = ["forbud", "krav", "pålegg", "dokumentasjon", "produktpass", "sporbarhet"]
    if any(k in kw_str for k in hoye_impact):
        return "Høy"

    # Emballasje og kjemikalier er ofte høy konsekvens
    if kategori in ["emballasje", "kjemikalier", "sporbarhet"]:
        return "Høy"

    # Medium konsekvens
    if kategori in ["sirkulær_økonomi", "grønnvasking"]:
        return "Medium"

    return "Lav"


def format_prioritet(prioritet: int) -> str:
    return {
        1: "🔴 Kritisk",
        2: "🟠 Viktig",
        3: "🟢 Info"
    }.get(prioritet, "🟢 Info")


def foreslå_handling(signal: Signal) -> str:
    """Foreslå handling for signal."""
    if signal.deadline:
        return "Forbered høringssvar - sett ansvarlig denne uken."

    if signal.sannsynlighet == "Høy" and signal.konsekvens == "Høy":
        return "Kritisk - start umiddelbar scenarioanalyse og kostnadsestimering."

    if signal.konsekvens == "Høy":
        return "Analyser påvirkning på produktportefølje og leverandører."

    if signal.type == "eu_direktiv":
        return "Følg implementering i EU/EØS - typisk 1-2 års varsel."

    if "emballasje" in signal.kategori:
        return "Vurder emballasjedesign og produsentansvar-løsninger."

    if "kjemikalier" in signal.kategori:
        return "Innhent dokumentasjon fra leverandører (SDS, SVHC-lister)."

    return "Bevar i fremtidsoversikt - følg med på utvikling."


# --- HOVEDMOTOR ---

class LovSonar:
    def __init__(self):
        self.cache = self._last_cache()
        self.signaler = []
        self.feil = []

    def _last_cache(self) -> dict:
        if os.path.exists(CONFIG["cache_file"]):
            try:
                with open(CONFIG["cache_file"], 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Kunne ikke laste cache: {e}")
        return {"sett_urls": [], "siste_kjoring": None}

    def _lagre_cache(self):
        self.cache["siste_kjoring"] = datetime.now().isoformat()
        try:
            with open(CONFIG["cache_file"], 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Kunne ikke lagre cache: {e}")

    async def _fetch_med_retry(self, session: aiohttp.ClientSession, url: str) -> Optional[str]:
        for attempt in range(CONFIG["retry_attempts"]):
            try:
                async with session.get(url, timeout=CONFIG["request_timeout"]) as response:
                    if response.status == 200:
                        return await response.text()
                    elif response.status == 429:
                        await asyncio.sleep(CONFIG["retry_delay"] * (attempt + 1))
                    else:
                        logger.warning(f"HTTP {response.status} for {url}")
                        return None
            except asyncio.TimeoutError:
                logger.warning(f"Timeout for {url} (forsøk {attempt + 1})")
            except Exception as e:
                logger.error(f"Feil ved {url}: {e}")

            if attempt < CONFIG["retry_attempts"] - 1:
                await asyncio.sleep(CONFIG["retry_delay"])

        return None

    async def _skann_rss_kilder(self, session: aiohttp.ClientSession):
        """Skann RSS-feeds fra Regjeringen."""
        logger.info("Skanner RSS-kilder (høringer, NOU-er, proposisjoner)...")

        rss_kilder = {k: v for k, v in SONAR_KILDER.items() if v["type"] == "rss"}

        for navn, config in rss_kilder.items():
            await asyncio.sleep(CONFIG["rate_limit_delay"])

            html = await self._fetch_med_retry(session, config["url"])
            if not html:
                continue

            try:
                feed = feedparser.parse(html)

                for entry in feed.entries[:CONFIG["max_entries"]]:
                    tittel = getattr(entry, 'title', '')
                    sammendrag = getattr(entry, 'summary', '')
                    link = getattr(entry, 'link', '')

                    # Sjekk om allerede sett
                    if link in self.cache.get("sett_urls", []):
                        continue

                    tekst = f"{tittel} {sammendrag}".lower()

                    # Finn matchende keywords
                    matchende_keywords = [kw for kw in ALLE_FREMTID_KEYWORDS if kw in tekst]

                    if matchende_keywords:
                        # Finn kategori
                        kategorier = []
                        for kat, kws in FREMTID_KEYWORDS.items():
                            if any(k in tekst for k in kws):
                                kategorier.append(kat)

                        hovedkategori = kategorier[0] if kategorier else "generelt"

                        deadline = ekstraher_deadline(tekst)
                        sannsynlighet = vurder_sannsynlighet(tekst, config["kategori"])
                        konsekvens = vurder_konsekvens(matchende_keywords, hovedkategori)
                        tidshorisont = estimat_tidshorisont(tekst, None)

                        self.signaler.append(Signal(
                            type=config["kategori"],
                            kilde=navn.replace("_", " ").title(),
                            kategori=hovedkategori,
                            tittel=tittel,
                            url=link,
                            sammendrag=sammendrag[:300],
                            keywords=matchende_keywords[:8],
                            deadline=deadline or "",
                            sannsynlighet=sannsynlighet,
                            konsekvens=konsekvens,
                            tidshorisont=tidshorisont
                        ))

                        # Legg til i cache
                        if "sett_urls" not in self.cache:
                            self.cache["sett_urls"] = []
                        self.cache["sett_urls"].append(link)

                        logger.info(f"Nytt signal: {tittel[:60]}...")

            except Exception as e:
                logger.error(f"Feil ved parsing av {navn}: {e}")

    async def kjor_skanning(self) -> dict:
        logger.info("=" * 70)
        logger.info("LovSonar v1.0 - Starter fremtidsovervåkning")
        logger.info("=" * 70)

        headers = {"User-Agent": CONFIG["user_agent"]}
        connector = aiohttp.TCPConnector(limit=5)

        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            await self._skann_rss_kilder(session)

        self._lagre_cache()

        rapport = {
            "tidspunkt": datetime.now().isoformat(),
            "signaler": [asdict(s) for s in self.signaler],
            "feil": self.feil,
            "statistikk": {
                "signaler_funnet": len(self.signaler),
                "kilder_sjekket": len([k for k, v in SONAR_KILDER.items() if v["type"] == "rss"])
            }
        }

        logger.info("-" * 70)
        logger.info(f"Skanning fullført: {len(self.signaler)} nye signaler")

        return rapport


# --- RAPPORTER ---

def generer_markdown_rapport(rapport: dict) -> str:
    """Generer fremtidsrettet Markdown-rapport."""
    now = datetime.now()
    uke = now.isocalendar().week

    signaler = [Signal(**s) for s in rapport["signaler"]]
    stats = rapport["statistikk"]

    # Sorter etter prioritet
    kritisk = [s for s in signaler if s.prioritet == 1]
    viktig = [s for s in signaler if s.prioritet == 2]
    info = [s for s in signaler if s.prioritet == 3]

    # Frister
    frister = []
    for s in signaler:
        if s.deadline:
            dato = parse_norsk_dato(s.deadline)
            frister.append((dato, s))
    frister.sort(key=lambda x: (x[0] is None, x[0] or date.max))

    # Statistikk
    per_kategori = Counter(s.kategori for s in signaler)
    per_type = Counter(s.type for s in signaler)
    per_konsekvens = Counter(s.konsekvens for s in signaler)

    lines = []
    lines.append(f"# 🔮 LovSonar v1.0 - Fremtidsrapport")
    lines.append(f"**Uke {uke}, {now.year}** | Generert: {now.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("## 📊 Strategisk Sammendrag")
    lines.append(f"- **Nye signaler:** {stats['signaler_funnet']}")
    lines.append(f"- **Prioritering:** 🔴 {len(kritisk)} | 🟠 {len(viktig)} | 🟢 {len(info)}")
    lines.append(f"- **Høringer med frist:** {len(frister)}")
    lines.append("")

    # Strategisk matrise
    lines.append("## 🎯 Strategisk Vurdering")
    lines.append("| Signal | Sannsynlighet | Konsekvens | Tidshorisont |")
    lines.append("|--------|---------------|-----------|--------------|")
    for s in sorted(signaler, key=lambda x: (x.prioritet, x.konsekvens))[:10]:
        lines.append(f"| {s.tittel[:40]} | {s.sannsynlighet} | {s.konsekvens} | {s.tidshorisont} |")
    lines.append("")

    # Topp handlinger
    lines.append("## 💡 Anbefalte Handlinger (Topp 5)")
    topp_signaler = sorted(signaler, key=lambda x: (x.prioritet, -ord(x.konsekvens[0])))[:5]
    if topp_signaler:
        for idx, s in enumerate(topp_signaler, 1):
            pri = format_prioritet(s.prioritet)
            handling = foreslå_handling(s)
            lines.append(f"{idx}. **{s.tittel[:80]}**")
            lines.append(f"   - {pri} | Konsekvens: {s.konsekvens} | Sannsynlighet: {s.sannsynlighet}")
            lines.append(f"   - Handling: {handling}")
            lines.append(f"   - [Les mer]({s.url})")
            lines.append("")
    else:
        lines.append("- Ingen nye signaler denne uken.")
    lines.append("")

    # Frister
    if frister:
        lines.append("## ⏰ Høringer med Frist")
        lines.append("| Frist | Tittel | Type |")
        lines.append("|-------|--------|------|")
        for dato, s in frister[:10]:
            dato_txt = dato.isoformat() if dato else s.deadline
            lines.append(f"| {dato_txt} | {s.tittel[:50]} | {s.type} |")
        lines.append("")

    # Tematisk fordeling
    if per_kategori:
        lines.append("## 📈 Tematisk Fordeling")
        for kat, antall in per_kategori.most_common():
            lines.append(f"- **{kat.replace('_', ' ').title()}**: {antall} signaler")
        lines.append("")

    # Detaljliste
    lines.append("## 📋 Detaljert Signalliste")
    for seksjon, items in [("🔴 Kritiske", kritisk), ("🟠 Viktige", viktig), ("🟢 Info", info)]:
        if items:
            lines.append(f"### {seksjon}")
            for s in items[:15]:
                kws = ", ".join(s.keywords[:5])
                dl = f" | Frist: {s.deadline}" if s.deadline else ""
                lines.append(f"- **[{s.type.upper()}] {s.tittel}**")
                lines.append(f"  - Kilde: {s.kilde}{dl}")
                lines.append(f"  - Vurdering: {s.sannsynlighet} sannsynlighet, {s.konsekvens} konsekvens, {s.tidshorisont}")
                lines.append(f"  - Nøkkelord: {kws}")
                lines.append(f"  - Handling: {foreslå_handling(s)}")
                lines.append(f"  - [Les dokumentet]({s.url})")
                lines.append("")

    lines.append("---")
    lines.append("*LovSonar v1.0 | Pilot - Strategisk Fremtidsovervåkning for Byggevarebransjen*")

    return "\n".join(lines)


def send_epost_rapport(rapport: dict, markdown: str):
    """Send e-post med rapport."""
    bruker = os.environ.get("EMAIL_USER", "").strip()
    passord = os.environ.get("EMAIL_PASS", "").strip()
    mottaker = os.environ.get("EMAIL_RECIPIENT", "").strip() or bruker

    if not all([bruker, passord, mottaker]):
        logger.warning("E-postkonfigurasjon mangler.")
        return False

    if not rapport["signaler"]:
        logger.info("Ingen nye signaler - hopper over e-post.")
        return False

    msg = MIMEMultipart("alternative")
    uke = datetime.now().isocalendar().week
    n_signaler = rapport['statistikk']['signaler_funnet']

    signaler_obj = [Signal(**s) for s in rapport["signaler"]]
    kritisk = len([s for s in signaler_obj if s.prioritet == 1])
    viktig = len([s for s in signaler_obj if s.prioritet == 2])

    emne = f"🔮 LovSonar uke {uke}: "
    if kritisk > 0:
        emne += f"🔴 {kritisk} kritisk, "
    if viktig > 0:
        emne += f"🟠 {viktig} viktig, "
    emne += f"{n_signaler} nye signaler"

    msg["Subject"] = emne
    msg["From"] = bruker
    msg["To"] = mottaker

    # Tekst-versjon
    tekst_versjon = markdown.replace("**", "").replace("##", "").replace("#", "")
    msg.attach(MIMEText(tekst_versjon, "plain", "utf-8"))

    # HTML-versjon (enkel)
    html = f"<html><body><pre style='font-family:Arial,sans-serif;white-space:pre-wrap'>{markdown}</pre></body></html>"
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(bruker, passord)
            server.sendmail(bruker, [mottaker], msg.as_string())
        logger.info(f"Rapport sendt til {mottaker}")
        return True
    except Exception as e:
        logger.error(f"E-postfeil: {e}")
        return False


# --- HOVEDPROGRAM ---

async def main():
    sonar = LovSonar()
    rapport = await sonar.kjor_skanning()

    # Lagre JSON
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    rapport_fil_json = f"lovsonar_rapport_{timestamp}.json"
    with open(rapport_fil_json, 'w', encoding='utf-8') as f:
        json.dump(rapport, f, indent=2, ensure_ascii=False)
    logger.info(f"JSON rapport lagret: {rapport_fil_json}")

    # Generer og lagre Markdown
    markdown = generer_markdown_rapport(rapport)
    rapport_fil_md = f"lovsonar_rapport_{timestamp}.md"
    with open(rapport_fil_md, 'w', encoding='utf-8') as f:
        f.write(markdown)
    logger.info(f"Markdown rapport lagret: {rapport_fil_md}")

    # Send e-post
    send_epost_rapport(rapport, markdown)

    return rapport


if __name__ == "__main__":
    asyncio.run(main())