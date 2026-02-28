#!/usr/bin/env python3
"""
LovRadar v14.1 - Strategisk Regulatorisk Overvåkning
Bærekraft & Handel for Byggevarebransjen

Ny i v14.1:
- Markdown-rapporter for enkel deling
- Prioritering av funn (Kritisk/Viktig/Info)
- Deadline-parsing fra nyheter
- Forbedrede handlingsforslag
"""

import os
import json
import hashlib
import smtplib
import difflib
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
class LovKilde:
    navn: str
    url: str
    kategori: str
    beskrivelse: str = ""

@dataclass
class RSSKilde:
    navn: str
    url: str
    kategori: str

# Strategisk Område 1: Miljø, Kjemikalier & Bærekraft
MILJO_LOVER = [
    LovKilde("REACH-forskriften", "https://lovdata.no/dokument/SF/forskrift/2008-05-30-516", "miljø", "Kjemikalier og stoffer"),
    LovKilde("CLP-forskriften", "https://lovdata.no/dokument/SF/forskrift/2012-06-16-622", "miljø", "Klassifisering og merking"),
    LovKilde("Avfallsforskriften", "https://lovdata.no/dokument/SF/forskrift/2004-06-01-930", "miljø", "Håndtering og sortering"),
    LovKilde("Biocidforskriften", "https://lovdata.no/dokument/SF/forskrift/2017-04-18-480", "miljø", "Impregnering og skadedyr"),
    LovKilde("Lov om bærekraftig finans", "https://lovdata.no/dokument/NL/lov/2021-12-22-161", "miljø", "Taksonomi"),
    LovKilde("Produktforskriften", "https://lovdata.no/dokument/SF/forskrift/2004-06-01-922", "miljø", "Farlige stoffer i produkter"),
]

# Strategisk Område 2: Bygg og Produktkrav
BYGG_LOVER = [
    LovKilde("DOK-forskriften", "https://lovdata.no/dokument/SF/forskrift/2013-12-17-1579", "bygg", "Dokumentasjon av byggevarer"),
    LovKilde("TEK17", "https://lovdata.no/dokument/SF/forskrift/2017-06-19-840", "bygg", "Byggteknisk forskrift"),
    LovKilde("TEK17 Kap 9 (Miljø)", "https://www.dibk.no/regelverk/byggteknisk-forskrift-tek17/9/9-1", "bygg", "Miljøkrav i bygg"),
    LovKilde("Produktkontrolloven", "https://lovdata.no/dokument/NL/lov/1976-06-11-79", "bygg", "Produktsikkerhet"),
    LovKilde("Tømmerforskriften", "https://lovdata.no/dokument/SF/forskrift/2015-04-24-406", "bygg", "Sporbarhet og import"),
    LovKilde("FEL-forskriften", "https://lovdata.no/dokument/SF/forskrift/1998-11-06-1060", "bygg", "Elektriske lavspenningsanlegg"),
    LovKilde("Internkontrollforskriften", "https://lovdata.no/dokument/SF/forskrift/1996-12-06-1127", "bygg", "HMS og rutiner"),
    LovKilde("Plan- og bygningsloven", "https://lovdata.no/dokument/NL/lov/2008-06-27-71", "bygg", "Hovedlov for bygging"),
]

# Strategisk Område 3: Handel og Forbruker
HANDEL_LOVER = [
    LovKilde("Forbrukerkjøpsloven", "https://lovdata.no/dokument/NL/lov/2002-06-21-34", "handel", "Reklamasjon og rettigheter"),
    LovKilde("Kjøpsloven", "https://lovdata.no/dokument/NL/lov/1988-05-13-27", "handel", "Næringskjøp"),
    LovKilde("Markedsføringsloven", "https://lovdata.no/dokument/NL/lov/2009-01-09-2", "handel", "Miljøpåstander/grønnvasking"),
    LovKilde("Åpenhetsloven", "https://lovdata.no/dokument/NL/lov/2021-06-18-99", "handel", "Leverandørkjeder"),
    LovKilde("Regnskapsloven", "https://lovdata.no/dokument/NL/lov/1998-07-17-56", "handel", "Bærekraftsrapportering/CSRD"),
    LovKilde("Angrerettloven", "https://lovdata.no/dokument/NL/lov/2014-06-20-27", "handel", "Fjernsalg"),
    LovKilde("Ehandelsloven", "https://lovdata.no/dokument/NL/lov/2003-05-23-35", "handel", "Elektronisk handel"),
]

ALLE_LOVER = MILJO_LOVER + BYGG_LOVER + HANDEL_LOVER

RSS_KILDER = [
    RSSKilde("Regjeringen: Nyheter", "https://www.regjeringen.no/no/aktuelt/nyheter/id2006120/?type=rss", "alle"),
    RSSKilde("Regjeringen: Dokumenter", "https://www.regjeringen.no/no/dokument/id2000006/?type=rss", "alle"),
    RSSKilde("Forbrukertilsynet", "https://www.forbrukertilsynet.no/feed", "handel"),
]

KEYWORDS = {
    "miljø": [
        "bærekraft", "sirkulær", "grønnvasking", "miljøkrav", "klimagass", "utslipp",
        "resirkulering", "gjenvinning", "avfall", "kjemikalier", "reach", "svhc",
        "miljødeklarasjon", "epd", "livssyklus", "karbonavtrykk", "taksonomi",
        "biocid", "clp", "faremerking", "miljøgift"
    ],
    "bygg": [
        "byggevare", "ce-merking", "dokumentasjon", "produktpass", "tek17",
        "energikrav", "u-verdi", "brannkrav", "sikkerhet", "kvalitet",
        "treverk", "import", "eutr", "sporbarhet", "internkontroll",
        "elektrisk", "installasjon", "byggeplass", "hms"
    ],
    "handel": [
        "emballasje", "reklamasjon", "garanti", "forbruker", "markedsføring",
        "miljøpåstand", "åpenhet", "leverandørkjede", "menneskerettigheter",
        "aktsomhet", "rapportering", "csrd", "esg", "compliance",
        "bærekraftsrapport", "verdikjede"
    ]
}

ALLE_KEYWORDS = list(set(KEYWORDS["miljø"] + KEYWORDS["bygg"] + KEYWORDS["handel"]))

CONFIG = {
    "cache_file": "lovradar_cache.json",
    "change_threshold_percent": 0.3,
    "request_timeout": 30,
    "retry_attempts": 3,
    "retry_delay": 2,
    "rate_limit_delay": 0.5,
    "max_rss_entries": 15,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("LovRadar")

# Norske måneder for deadline-parsing
NORWEGIAN_MONTHS = {
    "januar": 1, "februar": 2, "mars": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12
}


# --- HJELPEFUNKSJONER ---

def normaliser_tekst(tekst: str) -> str:
    if not tekst:
        return ""
    tekst = re.sub(r'\d{1,2}\.\d{1,2}\.\d{2,4}', '', tekst)
    tekst = re.sub(r'\d{4}-\d{2}-\d{2}', '', tekst)
    tekst = re.sub(r'[Vv]ersjon\s*\d+(\.\d+)*', '', tekst)
    tekst = re.sub(r'Sist\s+endret.*?(?=\s{2}|\n|$)', '', tekst, flags=re.IGNORECASE)
    tekst = re.sub(r'\s+', ' ', tekst)
    tekst = re.sub(r'[§\-–—•·]', ' ', tekst)
    return tekst.strip().lower()


def ekstraher_lovtekst(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "button", "form", "input", "select", "meta", "link",
                     "noscript", "iframe"]):
        tag.decompose()
    for selector in [".breadcrumb", ".navigation", ".sidebar", ".footer",
                     ".header", ".menu", ".pagination", ".share", ".print"]:
        for elem in soup.select(selector):
            elem.decompose()
    content = (soup.find("div", class_="LovdataParagraf") or
               soup.find("div", class_="LovdataLov") or
               soup.find("div", class_="dokumentBeholder") or
               soup.find("div", id="LovdataDokument") or
               soup.find("article") or
               soup.find("main") or
               soup.find("div", {"role": "main"}) or
               soup.find("div", class_="content") or
               soup.body)
    if not content:
        return ""
    tekst = content.get_text(separator=" ")
    return normaliser_tekst(tekst)


def beregn_endring(gammel: str, ny: str) -> tuple:
    if not gammel or not ny:
        return 0.0, []
    gammel_norm = normaliser_tekst(gammel)
    ny_norm = normaliser_tekst(ny)
    matcher = difflib.SequenceMatcher(None, gammel_norm, ny_norm)
    likhet = matcher.ratio()
    endring_prosent = round((1 - likhet) * 100, 2)
    endringer = []
    if endring_prosent > 0:
        differ = difflib.unified_diff(
            gammel_norm.split('. '),
            ny_norm.split('. '),
            lineterm=''
        )
        for line in differ:
            if line.startswith('+') and not line.startswith('+++'):
                endring = line[1:].strip()
                if len(endring) > 20:
                    endringer.append("Nytt: " + endring[:200] + "...")
            elif line.startswith('-') and not line.startswith('---'):
                endring = line[1:].strip()
                if len(endring) > 20:
                    endringer.append("Fjernet: " + endring[:200] + "...")
    return endring_prosent, endringer[:5]


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
    """Finn frister/deadlines i tekst."""
    if not text:
        return None

    patterns = [
        r'(høringsfrist|frist)\s*[:\-]?\s*\d{1,2}\.\d{1,2}\.\d{4}',
        r'(trer i kraft|ikrafttredelse)\s*[:\-]?\s*\d{1,2}\.\d{1,2}\.\d{4}',
        r'(høringsfrist|frist)\s*[:\-]?\s*\d{1,2}\.\s*[a-zæøå]+\s+\d{4}',
        r'innen\s+\d{1,2}\.\s*[a-zæøå]+\s+\d{4}',
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0)

    return None


def format_prioritet(prioritet: int) -> str:
    """Formater prioritet som emoji."""
    return {
        1: "🔴 Kritisk",
        2: "🟠 Viktig",
        3: "🟢 Info"
    }.get(prioritet, "🟢 Info")


def foreslå_handling(funn: Funn) -> str:
    """Foreslå konkret handling basert på type funn."""
    if funn.deadline:
        return "Sett ansvarlig + intern frist denne uken."

    kat = funn.kategori.lower()

    if kat == "miljø":
        if any(k in str(funn.keywords).lower() for k in ["kjemikalier", "reach", "svhc"]):
            return "Start leverandørsjekk og dokumentasjonskrav."
        return "Vurder påvirkning på produkter og dokumentasjon."

    if kat == "bygg":
        return "Informer innkjøp/kategori om regelverksendring."

    if kat == "handel":
        if any(k in str(funn.keywords).lower() for k in ["markedsføring", "grønnvasking"]):
            return "Gjennomgå markedsføringspåstander/claims."
        if any(k in str(funn.keywords).lower() for k in ["åpenhet", "leverandørkjede"]):
            return "Start due diligence på kritiske leverandører."
        return "Vurder påvirkning på salgs- og returprosesser."

    return "Følg opp i compliance-møte."


@dataclass
class Funn:
    type: str
    kilde: str
    kategori: str
    tittel: str
    url: str
    beskrivelse: str = ""
    endring_prosent: float = 0.0
    endringer: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    prioritet: int = 3  # 1=Kritisk, 2=Viktig, 3=Info
    deadline: str = ""

    def __post_init__(self):
        """Beregn prioritet basert på keywords og endringsprosent."""
        if self.type == "lov" and self.endring_prosent >= 5.0:
            self.prioritet = 1
        elif self.type == "lov" and self.endring_prosent >= 2.0:
            self.prioritet = 2
        elif self.type == "rss":
            # Høy prioritet hvis kritiske nøkkelord
            kritiske = {"frist", "høringsfrist", "ikrafttredelse", "pålegg", "krav"}
            if any(k in str(self.keywords).lower() for k in kritiske):
                self.prioritet = 2
            # Sjekk om det er en deadline
            if self.deadline:
                self.prioritet = min(self.prioritet, 2)


# --- HOVEDMOTOR ---

class LovRadar:
    def __init__(self):
        self.cache = self._last_cache()
        self.funn = []
        self.feil = []

    def _last_cache(self) -> dict:
        if os.path.exists(CONFIG["cache_file"]):
            try:
                with open(CONFIG["cache_file"], 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Kunne ikke laste cache: {e}")
        return {"lover": {}, "siste_kjoring": None}

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

    async def _skann_lover(self, session: aiohttp.ClientSession):
        logger.info(f"Skanner {len(ALLE_LOVER)} lovkilder...")
        if "lover" not in self.cache:
            self.cache["lover"] = {}
        for lov in ALLE_LOVER:
            await asyncio.sleep(CONFIG["rate_limit_delay"])
            html = await self._fetch_med_retry(session, lov.url)
            if not html:
                self.feil.append(f"Kunne ikke hente: {lov.navn}")
                continue
            tekst = ekstraher_lovtekst(html)
            if not tekst:
                continue
            ny_hash = hashlib.sha256(tekst.encode()).hexdigest()
            if lov.navn in self.cache["lover"]:
                gammel = self.cache["lover"][lov.navn]
                if ny_hash != gammel.get("hash"):
                    endring_prosent, endringer = beregn_endring(
                        gammel.get("tekst", ""),
                        tekst
                    )
                    if endring_prosent >= CONFIG["change_threshold_percent"]:
                        self.funn.append(Funn(
                            type="lov",
                            kilde=lov.navn,
                            kategori=lov.kategori,
                            tittel=lov.navn + " - " + lov.beskrivelse,
                            url=lov.url,
                            beskrivelse=lov.beskrivelse,
                            endring_prosent=endring_prosent,
                            endringer=endringer
                        ))
                        logger.info(f"Endring detektert: {lov.navn} ({endring_prosent}%)")
            else:
                logger.info(f"Ny baseline for: {lov.navn}")
            self.cache["lover"][lov.navn] = {
                "hash": ny_hash,
                "tekst": tekst[:10000],
                "sist_sjekket": datetime.now().isoformat(),
                "kategori": lov.kategori
            }

    async def _skann_rss(self, session: aiohttp.ClientSession):
        logger.info(f"Skanner {len(RSS_KILDER)} RSS-kilder...")
        for rss in RSS_KILDER:
            await asyncio.sleep(CONFIG["rate_limit_delay"])
            html = await self._fetch_med_retry(session, rss.url)
            if not html:
                continue
            try:
                feed = feedparser.parse(html)
                for entry in feed.entries[:CONFIG["max_rss_entries"]]:
                    tittel = getattr(entry, 'title', '')
                    sammendrag = getattr(entry, 'summary', '')
                    link = getattr(entry, 'link', '')
                    tekst = (tittel + " " + sammendrag).lower()
                    matchende_keywords = [kw for kw in ALLE_KEYWORDS if kw in tekst]
                    if matchende_keywords:
                        eksisterende_urls = [f.url for f in self.funn if f.type == "rss"]
                        if link not in eksisterende_urls:
                            deadline = ekstraher_deadline(tekst)
                            self.funn.append(Funn(
                                type="rss",
                                kilde=rss.navn,
                                kategori=rss.kategori,
                                tittel=tittel,
                                url=link,
                                keywords=matchende_keywords[:5],
                                deadline=deadline or ""
                            ))
            except Exception as e:
                logger.error(f"Feil ved parsing av {rss.navn}: {e}")

    async def kjor_skanning(self) -> dict:
        logger.info("=" * 60)
        logger.info("LovRadar v14.0 - Starter strategisk skanning")
        logger.info("=" * 60)
        headers = {"User-Agent": CONFIG["user_agent"]}
        connector = aiohttp.TCPConnector(limit=5)
        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            await self._skann_lover(session)
            await self._skann_rss(session)
        self._lagre_cache()

        lovendringer = [asdict(f) for f in self.funn if f.type == "lov"]
        nyheter = [asdict(f) for f in self.funn if f.type == "rss"]

        rapport = {
            "tidspunkt": datetime.now().isoformat(),
            "lovendringer": lovendringer,
            "nyheter": nyheter,
            "feil": self.feil,
            "statistikk": {
                "lover_sjekket": len(ALLE_LOVER),
                "rss_sjekket": len(RSS_KILDER),
                "lovendringer_funnet": len(lovendringer),
                "nyheter_funnet": len(nyheter)
            }
        }
        logger.info("-" * 60)
        logger.info(f"Skanning fullført: {len(lovendringer)} lovendringer, {len(nyheter)} relevante nyheter")
        return rapport


# --- RAPPORTER ---

def generer_markdown_rapport(rapport: dict) -> str:
    """Generer Markdown-rapport for enkel deling."""
    now = datetime.now()
    uke = now.isocalendar().week

    lovendringer = rapport["lovendringer"]
    nyheter = rapport["nyheter"]
    stats = rapport["statistikk"]

    # Sorter etter prioritet
    alle_funn = []
    for lov in lovendringer:
        alle_funn.append(lov)
    for nyhet in nyheter:
        alle_funn.append(nyhet)

    kritisk = [f for f in alle_funn if f.get("prioritet") == 1]
    viktig = [f for f in alle_funn if f.get("prioritet") == 2]
    info = [f for f in alle_funn if f.get("prioritet") == 3]

    # Statistikk
    kilder = Counter(f.get("kilde", "Ukjent") for f in alle_funn)
    kategorier = Counter(f.get("kategori", "Ukjent") for f in alle_funn)

    # Frister
    frister = []
    for f in alle_funn:
        if f.get("deadline"):
            dato = parse_norsk_dato(f["deadline"])
            frister.append((dato, f))
    frister.sort(key=lambda x: (x[0] is None, x[0] or date.max))

    lines = []
    lines.append(f"# LovRadar v14.1 - Ukesrapport")
    lines.append(f"**Uke {uke}, {now.year}** | Generert: {now.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("## Ledersammendrag")
    lines.append(f"- **Nye nyheter:** {stats['nyheter_funnet']}")
    lines.append(f"- **Lovendringer:** {stats['lovendringer_funnet']}")
    lines.append(f"- **Prioritering:** 🔴 {len(kritisk)} | 🟠 {len(viktig)} | 🟢 {len(info)}")
    lines.append("")

    # Topp handlinger
    lines.append("## Anbefalte handlinger (topp 5)")
    topp_funn = sorted(alle_funn, key=lambda x: (x.get("prioritet", 9), -x.get("endring_prosent", 0)))[:5]
    if topp_funn:
        for idx, f in enumerate(topp_funn, 1):
            pri = format_prioritet(f.get("prioritet", 3))
            # Lager Funn objekt midlertidig for å få handling
            temp_funn = Funn(
                type=f["type"],
                kilde=f["kilde"],
                kategori=f["kategori"],
                tittel=f["tittel"],
                url=f["url"],
                keywords=f.get("keywords", []),
                deadline=f.get("deadline", ""),
                prioritet=f.get("prioritet", 3)
            )
            handling = foreslå_handling(temp_funn)
            lines.append(f"{idx}. **{f['tittel'][:90]}**")
            lines.append(f"   - {pri} | Kilde: {f['kilde']}")
            lines.append(f"   - Handling: {handling}")
            lines.append(f"   - [Åpne kilde]({f['url']})")
            lines.append("")
    else:
        lines.append("- Ingen nye signaler denne uken.")
    lines.append("")

    # Frister
    if frister:
        lines.append("## Frister og tidshorisont")
        lines.append("| Dato | Tittel | Prioritet |")
        lines.append("|------|--------|-----------|")
        for dato, f in frister[:10]:
            dato_txt = dato.isoformat() if dato else f.get("deadline", "Ukjent")
            pri = format_prioritet(f.get("prioritet", 3))
            lines.append(f"| {dato_txt} | {f['tittel'][:60]} | {pri} |")
        lines.append("")

    # Lovendringer
    if lovendringer:
        lines.append("## Lovendringer")
        lines.append("| Lov/forskrift | Endring | Vurdering |")
        lines.append("|---------------|---------|-----------|")
        for lov in sorted(lovendringer, key=lambda x: -x.get("endring_prosent", 0)):
            navn = lov["kilde"]
            pst = lov.get("endring_prosent", 0)
            sev = "Høy" if pst >= 5 else "Moderat" if pst >= 2 else "Lav"
            lines.append(f"| [{navn}]({lov['url']}) | {pst:.1f}% | {sev} |")
        lines.append("")

    # Kildefordeling
    if kilder:
        lines.append("## Kildefordeling")
        for kilde, antall in kilder.most_common(10):
            lines.append(f"- **{kilde}**: {antall} funn")
        lines.append("")

    # Detaljliste
    lines.append("## Detaljliste")
    for seksjon, items in [("🔴 Kritisk", kritisk), ("🟠 Viktig", viktig), ("🟢 Info", info)]:
        if items:
            lines.append(f"### {seksjon}")
            for f in items[:15]:
                kws = ", ".join(f.get("keywords", [])[:5])
                dl = f" | Frist: {f.get('deadline')}" if f.get("deadline") else ""
                lines.append(f"- **{f['tittel']}**")
                lines.append(f"  - Kilde: {f['kilde']}{dl}")
                lines.append(f"  - Nøkkelord: {kws}")
                lines.append(f"  - [Les mer]({f['url']})")
                lines.append("")

    lines.append("---")
    lines.append("*LovRadar v14.1 | Proof of Concept*")

    return "\n".join(lines)


def generer_html_rapport(rapport: dict) -> str:
    dato = datetime.now().strftime('%d.%m.%Y')

    lov_miljo = [f for f in rapport["lovendringer"] if f["kategori"] == "miljø"]
    lov_bygg = [f for f in rapport["lovendringer"] if f["kategori"] == "bygg"]
    lov_handel = [f for f in rapport["lovendringer"] if f["kategori"] == "handel"]

    nyheter_miljo = [f for f in rapport["nyheter"] if f["kategori"] == "miljø"]
    nyheter_bygg = [f for f in rapport["nyheter"] if f["kategori"] == "bygg"]
    nyheter_handel = [f for f in rapport["nyheter"] if f["kategori"] == "handel"]
    nyheter_alle = [f for f in rapport["nyheter"] if f["kategori"] == "alle"]

    def render_lovendring(f):
        endringer_html = ""
        if f.get("endringer"):
            endringer_html = "<ul style='margin: 5px 0; padding-left: 20px; font-size: 12px; color: #666;'>"
            for e in f["endringer"][:3]:
                endringer_html += "<li>" + e + "</li>"
            endringer_html += "</ul>"
        return (
            "<div style='background: #fff3cd; padding: 10px; margin: 10px 0; "
            "border-left: 4px solid #ffc107; border-radius: 4px;'>"
            "<b>" + f['kilde'] + "</b> "
            "<span style='color: #dc3545;'>(" + str(f['endring_prosent']) + "% endring)</span><br>"
            "<span style='color: #666; font-size: 12px;'>" + f.get('beskrivelse', '') + "</span>"
            + endringer_html +
            "<a href='" + f['url'] + "' style='color: #007bff;'>Se kilde</a>"
            "</div>"
        )

    def render_nyhet(f):
        keywords = ", ".join(f.get("keywords", [])[:3])
        return (
            "<div style='padding: 8px 0; border-bottom: 1px solid #eee;'>"
            "<b>" + f['tittel'] + "</b><br>"
            "<span style='color: #666; font-size: 12px;'>"
            + f['kilde'] + " | Stikkord: " + keywords + "</span><br>"
            "<a href='" + f['url'] + "' style='color: #007bff; font-size: 12px;'>Les mer</a>"
            "</div>"
        )

    def render_seksjon(tittel, emoji, lovendringer, nyheter, farge):
        if not lovendringer and not nyheter:
            return ""
        innhold = ""
        if lovendringer:
            innhold += "<h4 style='margin: 10px 0 5px 0;'>Lovendringer:</h4>"
            for f in lovendringer:
                innhold += render_lovendring(f)
        if nyheter:
            innhold += "<h4 style='margin: 15px 0 5px 0;'>Relevante nyheter:</h4>"
            for f in nyheter:
                innhold += render_nyhet(f)
        return (
            "<div style='margin: 20px 0; padding: 15px; background: #f8f9fa; "
            "border-radius: 8px; border-left: 5px solid " + farge + ";'>"
            "<h3 style='margin: 0 0 10px 0; color: " + farge + ";'>"
            + emoji + " " + tittel + "</h3>" + innhold + "</div>"
        )

    seksjoner = ""
    seksjoner += render_seksjon("Miljo, Kjemikalier og Baerekraft", "[MILJO]", lov_miljo, nyheter_miljo, "#28a745")
    seksjoner += render_seksjon("Bygg og Produktkrav", "[BYGG]", lov_bygg, nyheter_bygg, "#17a2b8")
    seksjoner += render_seksjon("Handel og Forbruker", "[HANDEL]", lov_handel, nyheter_handel, "#6f42c1")
    if nyheter_alle:
        seksjoner += render_seksjon("Generelt (Stortinget)", "[GENERELT]", [], nyheter_alle, "#6c757d")

    if not seksjoner:
        seksjoner = (
            "<div style='padding: 20px; text-align: center; color: #666;'>"
            "<p>Ingen vesentlige endringer eller relevante nyheter denne perioden.</p>"
            "</div>"
        )

    feil_html = ""
    if rapport.get("feil"):
        feil_items = "".join(["<li>" + f + "</li>" for f in rapport["feil"][:5]])
        feil_html = (
            "<div style='margin: 20px 0; padding: 10px; background: #f8d7da; border-radius: 4px;'>"
            "<b>Tekniske merknader:</b><ul style='margin: 5px 0;'>" + feil_items + "</ul></div>"
        )

    stats = rapport['statistikk']

    html = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>LovRadar Rapport</title></head>
<body style="font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto; padding: 20px; background: #f5f5f5;">
<div style="background: linear-gradient(135deg, #1a5f7a 0%, #2d8e9f 100%); color: white; padding: 25px; border-radius: 12px; margin-bottom: 20px;">
<h1 style="margin: 0; font-size: 24px;">LovRadar v14.0</h1>
<p style="margin: 5px 0 0 0; opacity: 0.9;">Baerekraft og Handel - Byggevarebransjen</p>
<p style="margin: 10px 0 0 0; font-size: 14px; opacity: 0.8;">Strategisk rapport: """ + dato + """</p>
</div>
<div style="background: white; padding: 15px; border-radius: 8px; margin-bottom: 20px; display: flex; justify-content: space-around; text-align: center;">
<div><div style="font-size: 28px; font-weight: bold; color: #dc3545;">""" + str(stats['lovendringer_funnet']) + """</div><div style="font-size: 12px; color: #666;">Lovendringer</div></div>
<div><div style="font-size: 28px; font-weight: bold; color: #17a2b8;">""" + str(stats['nyheter_funnet']) + """</div><div style="font-size: 12px; color: #666;">Relevante nyheter</div></div>
<div><div style="font-size: 28px; font-weight: bold; color: #28a745;">""" + str(stats['lover_sjekket']) + """</div><div style="font-size: 12px; color: #666;">Kilder overvaket</div></div>
</div>
<div style="background: white; padding: 20px; border-radius: 8px;">""" + seksjoner + """</div>
""" + feil_html + """
<div style="text-align: center; padding: 20px; color: #999; font-size: 12px;">
<p>LovRadar v14.0 | Proof of Concept | Pilotfase</p>
<p>Basert pa offentlige rettskilder under NLOD 2.0</p>
</div>
</body>
</html>"""

    return html


def send_epost_rapport(rapport: dict, markdown: str = ""):
    bruker = os.environ.get("EMAIL_USER", "").strip()
    passord = os.environ.get("EMAIL_PASS", "").strip()
    mottaker = os.environ.get("EMAIL_RECIPIENT", "").strip() or bruker

    if not all([bruker, passord, mottaker]):
        logger.warning("E-postkonfigurasjon mangler. Hopper over sending.")
        return False

    if not rapport["lovendringer"] and not rapport["nyheter"]:
        logger.info("Ingen funn a rapportere. Hopper over e-post.")
        return False

    msg = MIMEMultipart("alternative")
    dato = datetime.now().strftime('%d.%m.%Y')
    uke = datetime.now().isocalendar().week
    n_lov = rapport['statistikk']['lovendringer_funnet']
    n_nyheter = rapport['statistikk']['nyheter_funnet']

    # Prioritetstelling
    alle_funn = rapport["lovendringer"] + rapport["nyheter"]
    kritisk = len([f for f in alle_funn if f.get("prioritet") == 1])
    viktig = len([f for f in alle_funn if f.get("prioritet") == 2])

    emne = f"LovRadar uke {uke}: "
    if kritisk > 0:
        emne += f"🔴 {kritisk} kritisk, "
    if viktig > 0:
        emne += f"🟠 {viktig} viktig, "
    emne += f"{n_lov} lovendring(er), {n_nyheter} nyhet(er)"

    msg["Subject"] = emne
    msg["From"] = bruker
    msg["To"] = mottaker

    # Legg til Markdown som preformatert tekst (lettere å lese i e-postklient)
    if markdown:
        tekst_versjon = markdown.replace("**", "").replace("##", "").replace("#", "")
        msg.attach(MIMEText(tekst_versjon, "plain", "utf-8"))

    # HTML-rapport
    html = generer_html_rapport(rapport)
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(bruker, passord)
            server.sendmail(bruker, [mottaker], msg.as_string())
        logger.info("Rapport sendt til " + mottaker)
        return True
    except Exception as e:
        logger.error("E-postfeil: " + str(e))
        return False


# --- HOVEDPROGRAM ---

async def main():
    radar = LovRadar()
    rapport = await radar.kjor_skanning()

    # Lagre JSON-rapport
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    rapport_fil_json = f"lovradar_rapport_{timestamp}.json"
    with open(rapport_fil_json, 'w', encoding='utf-8') as f:
        json.dump(rapport, f, indent=2, ensure_ascii=False)
    logger.info(f"JSON rapport lagret: {rapport_fil_json}")

    # Generer og lagre Markdown-rapport
    markdown = generer_markdown_rapport(rapport)
    rapport_fil_md = f"lovradar_rapport_{timestamp}.md"
    with open(rapport_fil_md, 'w', encoding='utf-8') as f:
        f.write(markdown)
    logger.info(f"Markdown rapport lagret: {rapport_fil_md}")

    # Send e-post med begge format
    send_epost_rapport(rapport, markdown)
    return rapport


if __name__ == "__main__":
    asyncio.run(main())