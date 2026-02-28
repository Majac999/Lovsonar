+   1 #!/usr/bin/env python3
+   2 """
+   3 LovSonar v1.0 - Strategisk Fremtidsovervåkning
+   4 Byggevarebransjen
+   5 
+   6 Fokus: Overvåker FREMTIDIGE reguleringer (ikke gjeldende lover)
+   7 - Norske forslag: NOU-er, Stortingsforslag, høringer
+   8 - EU-direktiver: Green Deal, ESPR, PPWR, DPP
+   9 - Regulatoriske trender i bærekraft
+  10 """
+  11 
+  12 import os
+  13 import json
+  14 import hashlib
+  15 import smtplib
+  16 import re
+  17 import asyncio
+  18 import aiohttp
+  19 import logging
+  20 from datetime import datetime, date
+  21 from email.mime.text import MIMEText
+  22 from email.mime.multipart import MIMEMultipart
+  23 from dataclasses import dataclass, field, asdict
+  24 from typing import Optional
+  25 from collections import Counter
+  26 from bs4 import BeautifulSoup
+  27 import feedparser
+  28 
+  29 # --- KONFIGURASJON ---
+  30 
+  31 @dataclass
+  32 class Signal:
+  33     """Et fremtidssignal (NOU, forslag, høring, EU-direktiv)."""
+  34     type: str  # "nou", "stortingsforslag", "horing", "eu_direktiv", "nyhet"
+  35     kilde: str
+  36     kategori: str
+  37     tittel: str
+  38     url: str
+  39     sammendrag: str = ""
+  40     keywords: list = field(default_factory=list)
+  41     prioritet: int = 3  # 1=Kritisk, 2=Viktig, 3=Info
+  42     sannsynlighet: str = "Ukjent"  # Høy/Medium/Lav
+  43     konsekvens: str = "Ukjent"  # Høy/Medium/Lav
+  44     tidshorisont: str = "Ukjent"  # <1år, 1-3år, >3år
+  45     deadline: str = ""
+  46     publisert: Optional[date] = None
+  47 
+  48     def __post_init__(self):
+  49         """Beregn prioritet."""
+  50         # Høy prioritet hvis høring med frist
+  51         if self.type == "horing" and self.deadline:
+  52             self.prioritet = 1
+  53         # Høy prioritet hvis kritiske nøkkelord
+  54         kritiske = {"frist", "høringsfrist", "ikrafttredelse", "krav", "forbud", "pålegg"}
+  55         if any(k in str(self.keywords).lower() for k in kritiske):
+  56             self.prioritet = min(self.prioritet, 2)
+  57         # EU-direktiver er ofte viktige
+  58         if self.type == "eu_direktiv":
+  59             self.prioritet = 2
+  60 
+  61 
+  62 # Strategiske kilder - FREMTIDSRETTEDE
+  63 SONAR_KILDER = {
+  64     "stortinget_saker": {
+  65         "url": "https://data.stortinget.no/eksport/saker?format=json&sesjonid=2023-2024",
+  66         "type": "api",
+  67         "kategori": "norsk_politikk"
+  68     },
+  69     "regjeringen_horinger": {
+  70         "url": "https://www.regjeringen.no/no/dokument/horinger/id438325/?type=rss",
+  71         "type": "rss",
+  72         "kategori": "horinger"
+  73     },
+  74     "regjeringen_nou": {
+  75         "url": "https://www.regjeringen.no/no/dokumenter/nou-er/id438249/?type=rss",
+  76         "type": "rss",
+  77         "kategori": "nou"
+  78     },
+  79     "regjeringen_proposisjoner": {
+  80         "url": "https://www.regjeringen.no/no/dokumenter/proposisjoner/id438246/?type=rss",
+  81         "type": "rss",
+  82         "kategori": "proposisjon"
+  83     },
+  84     "eur_lex_miljopakken": {
+  85         "url": "https://eur-lex.europa.eu/search.html?qid=1234567890&DTS_DOM=EU_LAW&type=advanced&lang=en&SUBDOM_INIT=ALL_ALL&DTS_SUBDOM=ALL_ALL",
+  86         "type": "web",  # Må scrapes
+  87         "kategori": "eu"
+  88     }
+  89 }
+  90 
+  91 # Nøkkelord tilpasset FREMTIDIGE reguleringer
+  92 FREMTID_KEYWORDS = {
+  93     "sirkulær_økonomi": [
+  94         "sirkulær", "produktpass", "dpp", "digital produktpass", "reparerbarhet",
+  95         "levetid", "modularitet", "resirkulering", "gjenvinning", "gjenbruk",
+  96         "ecodesign", "espr", "økodesign"
+  97     ],
+  98     "emballasje": [
+  99         "emballasje", "ppwr", "packaging", "plastemballasje", "gjenbruksemballasje",
+ 100         "emballasjeforordningen", "produsentansvar", "pant"
+ 101     ],
+ 102     "klima_energi": [
+ 103         "klimagass", "co2", "karbonavtrykk", "klimanøytral", "nullutslipp",
+ 104         "grønn", "fornybar", "energimerking", "energikrav"
+ 105     ],
+ 106     "kjemikalier": [
+ 107         "reach", "svhc", "farlige stoffer", "kjemikalier", "biocid", "clp",
+ 108         "mikroplast", "pfas", "evige kjemikalier"
+ 109     ],
+ 110     "sporbarhet": [
+ 111         "sporbarhet", "dokumentasjon", "leverandørkjede", "due diligence",
+ 112         "åpenhet", "menneskerettigheter", "tømmer", "eutr", "konfliktmineraler"
+ 113     ],
+ 114     "grønnvasking": [
+ 115         "grønnvasking", "greenwashing", "miljøpåstand", "bærekraftspåstand",
+ 116         "markedsføring", "villedende", "dokumenterbar"
+ 117     ],
+ 118     "bygg_produkter": [
+ 119         "byggevare", "byggprodukt", "ce-merking", "dok", "produktdokumentasjon",
+ 120         "tek", "byggteknisk", "energieffektiv"
+ 121     ]
+ 122 }
+ 123 
+ 124 ALLE_FREMTID_KEYWORDS = []
+ 125 for kategori_keywords in FREMTID_KEYWORDS.values():
+ 126     ALLE_FREMTID_KEYWORDS.extend(kategori_keywords)
+ 127 
+ 128 CONFIG = {
+ 129     "cache_file": "lovsonar_cache.json",
+ 130     "request_timeout": 30,
+ 131     "retry_attempts": 3,
+ 132     "retry_delay": 2,
+ 133     "rate_limit_delay": 0.5,
+ 134     "max_entries": 20,
+ 135     "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
+ 136 }
+ 137 
+ 138 logging.basicConfig(
+ 139     level=logging.INFO,
+ 140     format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
+ 141     datefmt="%Y-%m-%d %H:%M:%S"
+ 142 )
+ 143 logger = logging.getLogger("LovSonar")
+ 144 
+ 145 # Norske måneder
+ 146 NORWEGIAN_MONTHS = {
+ 147     "januar": 1, "februar": 2, "mars": 3, "april": 4, "mai": 5, "juni": 6,
+ 148     "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12
+ 149 }
+ 150 
+ 151 
+ 152 # --- HJELPEFUNKSJONER ---
+ 153 
+ 154 def parse_norsk_dato(text: str) -> Optional[date]:
+ 155     """Parser norske datoformater."""
+ 156     if not text:
+ 157         return None
+ 158     text_lower = text.lower()
+ 159     
+ 160     # dd.mm.yyyy
+ 161     m1 = re.search(r'\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b', text_lower)
+ 162     if m1:
+ 163         try:
+ 164             d, m, y = map(int, m1.groups())
+ 165             return date(y, m, d)
+ 166         except ValueError:
+ 167             pass
+ 168     
+ 169     # d. måned yyyy
+ 170     m2 = re.search(r'\b(\d{1,2})\.\s*([a-zæøå]+)\s+(\d{4})\b', text_lower)
+ 171     if m2:
+ 172         try:
+ 173             d = int(m2.group(1))
+ 174             month_word = m2.group(2)
+ 175             y = int(m2.group(3))
+ 176             month_num = NORWEGIAN_MONTHS.get(month_word)
+ 177             if month_num:
+ 178                 return date(y, month_num, d)
+ 179         except ValueError:
+ 180             pass
+ 181     
+ 182     return None
+ 183 
+ 184 
+ 185 def ekstraher_deadline(text: str) -> Optional[str]:
+ 186     """Finn frister/deadlines."""
+ 187     if not text:
+ 188         return None
+ 189     
+ 190     patterns = [
+ 191         r'(høringsfrist|frist)\s*[:\-]?\s*\d{1,2}\.\d{1,2}\.\d{4}',
+ 192         r'(trer i kraft|ikrafttredelse)\s*[:\-]?\s*\d{1,2}\.\d{1,2}\.\d{4}',
+ 193         r'(høringsfrist|frist)\s*[:\-]?\s*\d{1,2}\.\s*[a-zæøå]+\s+\d{4}',
+ 194         r'(senest|innen)\s+\d{1,2}\.\s*[a-zæøå]+\s+\d{4}',
+ 195     ]
+ 196     
+ 197     for pattern in patterns:
+ 198         m = re.search(pattern, text, re.IGNORECASE)
+ 199         if m:
+ 200             return m.group(0)
+ 201     
+ 202     return None
+ 203 
+ 204 
+ 205 def estimat_tidshorisont(text: str, publisert: Optional[date]) -> str:
+ 206     """Estimat når reguleringen kan tre i kraft."""
+ 207     if not text:
+ 208         return "Ukjent"
+ 209     
+ 210     text_lower = text.lower()
+ 211     
+ 212     # Sjekk eksplisitte tidspunkter
+ 213     if re.search(r'(2026|umiddelbar|straks|med virkning fra)', text_lower):
+ 214         return "<1 år"
+ 215     if re.search(r'(2027|2028)', text_lower):
+ 216         return "1-3 år"
+ 217     if re.search(r'(2029|2030|langsiktig)', text_lower):
+ 218         return ">3 år"
+ 219     
+ 220     # Basert på type dokument
+ 221     if "høring" in text_lower or "forslag" in text_lower:
+ 222         return "1-3 år"
+ 223     if "nou" in text_lower or "utredning" in text_lower:
+ 224         return ">3 år"
+ 225     
+ 226     return "Ukjent"
+ 227 
+ 228 
+ 229 def vurder_sannsynlighet(text: str, type: str) -> str:
+ 230     """Vurder sannsynlighet for at forslaget blir vedtatt."""
+ 231     if not text:
+ 232         return "Ukjent"
+ 233     
+ 234     text_lower = text.lower()
+ 235     
+ 236     # Høy sannsynlighet
+ 237     if type == "proposisjon" or "regjeringen foreslår" in text_lower:
+ 238         return "Høy"
+ 239     if "eu-direktiv" in text_lower or "eu-forordning" in text_lower:
+ 240         return "Høy"
+ 241     
+ 242     # Medium sannsynlighet
+ 243     if type == "horing" or "høring" in text_lower:
+ 244         return "Medium"
+ 245     
+ 246     # Lav sannsynlighet
+ 247     if type == "stortingsforslag" or "representantforslag" in text_lower:
+ 248         return "Lav"
+ 249     
+ 250     return "Ukjent"
+ 251 
+ 252 
+ 253 def vurder_konsekvens(keywords: list, kategori: str) -> str:
+ 254     """Vurder konsekvens for byggevarebransjen."""
+ 255     kw_str = " ".join(keywords).lower()
+ 256     
+ 257     # Høy konsekvens
+ 258     hoye_impact = ["forbud", "krav", "pålegg", "dokumentasjon", "produktpass", "sporbarhet"]
+ 259     if any(k in kw_str for k in hoye_impact):
+ 260         return "Høy"
+ 261     
+ 262     # Emballasje og kjemikalier er ofte høy konsekvens
+ 263     if kategori in ["emballasje", "kjemikalier", "sporbarhet"]:
+ 264         return "Høy"
+ 265     
+ 266     # Medium konsekvens
+ 267     if kategori in ["sirkulær_økonomi", "grønnvasking"]:
+ 268         return "Medium"
+ 269     
+ 270     return "Lav"
+ 271 
+ 272 
+ 273 def format_prioritet(prioritet: int) -> str:
+ 274     return {
+ 275         1: "🔴 Kritisk",
+ 276         2: "🟠 Viktig",
+ 277         3: "🟢 Info"
+ 278     }.get(prioritet, "🟢 Info")
+ 279 
+ 280 
+ 281 def foreslå_handling(signal: Signal) -> str:
+ 282     """Foreslå handling for signal."""
+ 283     if signal.deadline:
+ 284         return "Forbered høringssvar - sett ansvarlig denne uken."
+ 285     
+ 286     if signal.sannsynlighet == "Høy" and signal.konsekvens == "Høy":
+ 287         return "Kritisk - start umiddelbar scenarioanalyse og kostnadsestimering."
+ 288     
+ 289     if signal.konsekvens == "Høy":
+ 290         return "Analyser påvirkning på produktportefølje og leverandører."
+ 291     
+ 292     if signal.type == "eu_direktiv":
+ 293         return "Følg implementering i EU/EØS - typisk 1-2 års varsel."
+ 294     
+ 295     if "emballasje" in signal.kategori:
+ 296         return "Vurder emballasjedesign og produsentansvar-løsninger."
+ 297     
+ 298     if "kjemikalier" in signal.kategori:
+ 299         return "Innhent dokumentasjon fra leverandører (SDS, SVHC-lister)."
+ 300     
+ 301     return "Bevar i fremtidsoversikt - følg med på utvikling."
+ 302 
+ 303 
+ 304 # --- HOVEDMOTOR ---
+ 305 
+ 306 class LovSonar:
+ 307     def __init__(self):
+ 308         self.cache = self._last_cache()
+ 309         self.signaler = []
+ 310         self.feil = []
+ 311     
+ 312     def _last_cache(self) -> dict:
+ 313         if os.path.exists(CONFIG["cache_file"]):
+ 314             try:
+ 315                 with open(CONFIG["cache_file"], 'r', encoding='utf-8') as f:
+ 316                     return json.load(f)
+ 317             except Exception as e:
+ 318                 logger.warning(f"Kunne ikke laste cache: {e}")
+ 319         return {"sett_urls": [], "siste_kjoring": None}
+ 320     
+ 321     def _lagre_cache(self):
+ 322         self.cache["siste_kjoring"] = datetime.now().isoformat()
+ 323         try:
+ 324             with open(CONFIG["cache_file"], 'w', encoding='utf-8') as f:
+ 325                 json.dump(self.cache, f, indent=2, ensure_ascii=False)
+ 326         except Exception as e:
+ 327             logger.error(f"Kunne ikke lagre cache: {e}")
+ 328     
+ 329     async def _fetch_med_retry(self, session: aiohttp.ClientSession, url: str) -> Optional[str]:
+ 330         for attempt in range(CONFIG["retry_attempts"]):
+ 331             try:
+ 332                 async with session.get(url, timeout=CONFIG["request_timeout"]) as response:
+ 333                     if response.status == 200:
+ 334                         return await response.text()
+ 335                     elif response.status == 429:
+ 336                         await asyncio.sleep(CONFIG["retry_delay"] * (attempt + 1))
+ 337                     else:
+ 338                         logger.warning(f"HTTP {response.status} for {url}")
+ 339                         return None
+ 340             except asyncio.TimeoutError:
+ 341                 logger.warning(f"Timeout for {url} (forsøk {attempt + 1})")
+ 342             except Exception as e:
+ 343                 logger.error(f"Feil ved {url}: {e}")
+ 344             
+ 345             if attempt < CONFIG["retry_attempts"] - 1:
+ 346                 await asyncio.sleep(CONFIG["retry_delay"])
+ 347         
+ 348         return None
+ 349     
+ 350     async def _skann_rss_kilder(self, session: aiohttp.ClientSession):
+ 351         """Skann RSS-feeds fra Regjeringen."""
+ 352         logger.info("Skanner RSS-kilder (høringer, NOU-er, proposisjoner)...")
+ 353         
+ 354         rss_kilder = {k: v for k, v in SONAR_KILDER.items() if v["type"] == "rss"}
+ 355         
+ 356         for navn, config in rss_kilder.items():
+ 357             await asyncio.sleep(CONFIG["rate_limit_delay"])
+ 358             
+ 359             html = await self._fetch_med_retry(session, config["url"])
+ 360             if not html:
+ 361                 continue
+ 362             
+ 363             try:
+ 364                 feed = feedparser.parse(html)
+ 365                 
+ 366                 for entry in feed.entries[:CONFIG["max_entries"]]:
+ 367                     tittel = getattr(entry, 'title', '')
+ 368                     sammendrag = getattr(entry, 'summary', '')
+ 369                     link = getattr(entry, 'link', '')
+ 370                     
+ 371                     # Sjekk om allerede sett
+ 372                     if link in self.cache.get("sett_urls", []):
+ 373                         continue
+ 374                     
+ 375                     tekst = f"{tittel} {sammendrag}".lower()
+ 376                     
+ 377                     # Finn matchende keywords
+ 378                     matchende_keywords = [kw for kw in ALLE_FREMTID_KEYWORDS if kw in tekst]
+ 379                     
+ 380                     if matchende_keywords:
+ 381                         # Finn kategori
+ 382                         kategorier = []
+ 383                         for kat, kws in FREMTID_KEYWORDS.items():
+ 384                             if any(k in tekst for k in kws):
+ 385                                 kategorier.append(kat)
+ 386                         
+ 387                         hovedkategori = kategorier[0] if kategorier else "generelt"
+ 388                         
+ 389                         deadline = ekstraher_deadline(tekst)
+ 390                         sannsynlighet = vurder_sannsynlighet(tekst, config["kategori"])
+ 391                         konsekvens = vurder_konsekvens(matchende_keywords, hovedkategori)
+ 392                         tidshorisont = estimat_tidshorisont(tekst, None)
+ 393                         
+ 394                         self.signaler.append(Signal(
+ 395                             type=config["kategori"],
+ 396                             kilde=navn.replace("_", " ").title(),
+ 397                             kategori=hovedkategori,
+ 398                             tittel=tittel,
+ 399                             url=link,
+ 400                             sammendrag=sammendrag[:300],
+ 401                             keywords=matchende_keywords[:8],
+ 402                             deadline=deadline or "",
+ 403                             sannsynlighet=sannsynlighet,
+ 404                             konsekvens=konsekvens,
+ 405                             tidshorisont=tidshorisont
+ 406                         ))
+ 407                         
+ 408                         # Legg til i cache
+ 409                         if "sett_urls" not in self.cache:
+ 410                             self.cache["sett_urls"] = []
+ 411                         self.cache["sett_urls"].append(link)
+ 412                         
+ 413                         logger.info(f"Nytt signal: {tittel[:60]}...")
+ 414             
+ 415             except Exception as e:
+ 416                 logger.error(f"Feil ved parsing av {navn}: {e}")
+ 417     
+ 418     async def kjor_skanning(self) -> dict:
+ 419         logger.info("=" * 70)
+ 420         logger.info("LovSonar v1.0 - Starter fremtidsovervåkning")
+ 421         logger.info("=" * 70)
+ 422         
+ 423         headers = {"User-Agent": CONFIG["user_agent"]}
+ 424         connector = aiohttp.TCPConnector(limit=5)
+ 425         
+ 426         async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
+ 427             await self._skann_rss_kilder(session)
+ 428         
+ 429         self._lagre_cache()
+ 430         
+ 431         rapport = {
+ 432             "tidspunkt": datetime.now().isoformat(),
+ 433             "signaler": [asdict(s) for s in self.signaler],
+ 434             "feil": self.feil,
+ 435             "statistikk": {
+ 436                 "signaler_funnet": len(self.signaler),
+ 437                 "kilder_sjekket": len([k for k, v in SONAR_KILDER.items() if v["type"] == "rss"])
+ 438             }
+ 439         }
+ 440         
+ 441         logger.info("-" * 70)
+ 442         logger.info(f"Skanning fullført: {len(self.signaler)} nye signaler")
+ 443         
+ 444         return rapport
+ 445 
+ 446 
+ 447 # --- RAPPORTER ---
+ 448 
+ 449 def generer_markdown_rapport(rapport: dict) -> str:
+ 450     """Generer fremtidsrettet Markdown-rapport."""
+ 451     now = datetime.now()
+ 452     uke = now.isocalendar().week
+ 453     
+ 454     signaler = [Signal(**s) for s in rapport["signaler"]]
+ 455     stats = rapport["statistikk"]
+ 456     
+ 457     # Sorter etter prioritet
+ 458     kritisk = [s for s in signaler if s.prioritet == 1]
+ 459     viktig = [s for s in signaler if s.prioritet == 2]
+ 460     info = [s for s in signaler if s.prioritet == 3]
+ 461     
+ 462     # Frister
+ 463     frister = []
+ 464     for s in signaler:
+ 465         if s.deadline:
+ 466             dato = parse_norsk_dato(s.deadline)
+ 467             frister.append((dato, s))
+ 468     frister.sort(key=lambda x: (x[0] is None, x[0] or date.max))
+ 469     
+ 470     # Statistikk
+ 471     per_kategori = Counter(s.kategori for s in signaler)
+ 472     per_type = Counter(s.type for s in signaler)
+ 473     per_konsekvens = Counter(s.konsekvens for s in signaler)
+ 474     
+ 475     lines = []
+ 476     lines.append(f"# 🔮 LovSonar v1.0 - Fremtidsrapport")
+ 477     lines.append(f"**Uke {uke}, {now.year}** | Generert: {now.strftime('%Y-%m-%d %H:%M')}")
+ 478     lines.append("")
+ 479     lines.append("## 📊 Strategisk Sammendrag")
+ 480     lines.append(f"- **Nye signaler:** {stats['signaler_funnet']}")
+ 481     lines.append(f"- **Prioritering:** 🔴 {len(kritisk)} | 🟠 {len(viktig)} | 🟢 {len(info)}")
+ 482     lines.append(f"- **Høringer med frist:** {len(frister)}")
+ 483     lines.append("")
+ 484     
+ 485     # Strategisk matrise
+ 486     lines.append("## 🎯 Strategisk Vurdering")
+ 487     lines.append("| Signal | Sannsynlighet | Konsekvens | Tidshorisont |")
+ 488     lines.append("|--------|---------------|-----------|--------------|")
+ 489     for s in sorted(signaler, key=lambda x: (x.prioritet, x.konsekvens))[:10]:
+ 490         lines.append(f"| {s.tittel[:40]} | {s.sannsynlighet} | {s.konsekvens} | {s.tidshorisont} |")
+ 491     lines.append("")
+ 492     
+ 493     # Topp handlinger
+ 494     lines.append("## 💡 Anbefalte Handlinger (Topp 5)")
+ 495     topp_signaler = sorted(signaler, key=lambda x: (x.prioritet, -ord(x.konsekvens[0])))[:5]
+ 496     if topp_signaler:
+ 497         for idx, s in enumerate(topp_signaler, 1):
+ 498             pri = format_prioritet(s.prioritet)
+ 499             handling = foreslå_handling(s)
+ 500             lines.append(f"{idx}. **{s.tittel[:80]}**")
+ 501             lines.append(f"   - {pri} | Konsekvens: {s.konsekvens} | Sannsynlighet: {s.sannsynlighet}")
+ 502             lines.append(f"   - Handling: {handling}")
+ 503             lines.append(f"   - [Les mer]({s.url})")
+ 504             lines.append("")
+ 505     else:
+ 506         lines.append("- Ingen nye signaler denne uken.")
+ 507     lines.append("")
+ 508     
+ 509     # Frister
+ 510     if frister:
+ 511         lines.append("## ⏰ Høringer med Frist")
+ 512         lines.append("| Frist | Tittel | Type |")
+ 513         lines.append("|-------|--------|------|")
+ 514         for dato, s in frister[:10]:
+ 515             dato_txt = dato.isoformat() if dato else s.deadline
+ 516             lines.append(f"| {dato_txt} | {s.tittel[:50]} | {s.type} |")
+ 517         lines.append("")
+ 518     
+ 519     # Tematisk fordeling
+ 520     if per_kategori:
+ 521         lines.append("## 📈 Tematisk Fordeling")
+ 522         for kat, antall in per_kategori.most_common():
+ 523             lines.append(f"- **{kat.replace('_', ' ').title()}**: {antall} signaler")
+ 524         lines.append("")
+ 525     
+ 526     # Detaljliste
+ 527     lines.append("## 📋 Detaljert Signalliste")
+ 528     for seksjon, items in [("🔴 Kritiske", kritisk), ("🟠 Viktige", viktig), ("🟢 Info", info)]:
+ 529         if items:
+ 530             lines.append(f"### {seksjon}")
+ 531             for s in items[:15]:
+ 532                 kws = ", ".join(s.keywords[:5])
+ 533                 dl = f" | Frist: {s.deadline}" if s.deadline else ""
+ 534                 lines.append(f"- **[{s.type.upper()}] {s.tittel}**")
+ 535                 lines.append(f"  - Kilde: {s.kilde}{dl}")
+ 536                 lines.append(f"  - Vurdering: {s.sannsynlighet} sannsynlighet, {s.konsekvens} konsekvens, {s.tidshorisont}")
+ 537                 lines.append(f"  - Nøkkelord: {kws}")
+ 538                 lines.append(f"  - Handling: {foreslå_handling(s)}")
+ 539                 lines.append(f"  - [Les dokumentet]({s.url})")
+ 540                 lines.append("")
+ 541     
+ 542     lines.append("---")
+ 543     lines.append("*LovSonar v1.0 | Pilot - Strategisk Fremtidsovervåkning for Byggevarebransjen*")
+ 544     
+ 545     return "\n".join(lines)
+ 546 
+ 547 
+ 548 def send_epost_rapport(rapport: dict, markdown: str):
+ 549     """Send e-post med rapport."""
+ 550     bruker = os.environ.get("EMAIL_USER", "").strip()
+ 551     passord = os.environ.get("EMAIL_PASS", "").strip()
+ 552     mottaker = os.environ.get("EMAIL_RECIPIENT", "").strip() or bruker
+ 553     
+ 554     if not all([bruker, passord, mottaker]):
+ 555         logger.warning("E-postkonfigurasjon mangler.")
+ 556         return False
+ 557     
+ 558     if not rapport["signaler"]:
+ 559         logger.info("Ingen nye signaler - hopper over e-post.")
+ 560         return False
+ 561     
+ 562     msg = MIMEMultipart("alternative")
+ 563     uke = datetime.now().isocalendar().week
+ 564     n_signaler = rapport['statistikk']['signaler_funnet']
+ 565     
+ 566     signaler_obj = [Signal(**s) for s in rapport["signaler"]]
+ 567     kritisk = len([s for s in signaler_obj if s.prioritet == 1])
+ 568     viktig = len([s for s in signaler_obj if s.prioritet == 2])
+ 569     
+ 570     emne = f"🔮 LovSonar uke {uke}: "
+ 571     if kritisk > 0:
+ 572         emne += f"🔴 {kritisk} kritisk, "
+ 573     if viktig > 0:
+ 574         emne += f"🟠 {viktig} viktig, "
+ 575     emne += f"{n_signaler} nye signaler"
+ 576     
+ 577     msg["Subject"] = emne
+ 578     msg["From"] = bruker
+ 579     msg["To"] = mottaker
+ 580     
+ 581     # Tekst-versjon
+ 582     tekst_versjon = markdown.replace("**", "").replace("##", "").replace("#", "")
+ 583     msg.attach(MIMEText(tekst_versjon, "plain", "utf-8"))
+ 584     
+ 585     # HTML-versjon (enkel)
+ 586     html = f"<html><body><pre style='font-family:Arial,sans-serif;white-space:pre-wrap'>{markdown}</pre></body></html>"
+ 587     msg.attach(MIMEText(html, "html", "utf-8"))
+ 588     
+ 589     try:
+ 590         with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
+ 591             server.login(bruker, passord)
+ 592             server.sendmail(bruker, [mottaker], msg.as_string())
+ 593         logger.info(f"Rapport sendt til {mottaker}")
+ 594         return True
+ 595     except Exception as e:
+ 596         logger.error(f"E-postfeil: {e}")
+ 597         return False
+ 598 
+ 599 
+ 600 # --- HOVEDPROGRAM ---
+ 601 
+ 602 async def main():
+ 603     sonar = LovSonar()
+ 604     rapport = await sonar.kjor_skanning()
+ 605     
+ 606     # Lagre JSON
+ 607     timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
+ 608     rapport_fil_json = f"lovsonar_rapport_{timestamp}.json"
+ 609     with open(rapport_fil_json, 'w', encoding='utf-8') as f:
+ 610         json.dump(rapport, f, indent=2, ensure_ascii=False)
+ 611     logger.info(f"JSON rapport lagret: {rapport_fil_json}")
+ 612     
+ 613     # Generer og lagre Markdown
+ 614     markdown = generer_markdown_rapport(rapport)
+ 615     rapport_fil_md = f"lovsonar_rapport_{timestamp}.md"
+ 616     with open(rapport_fil_md, 'w', encoding='utf-8') as f:
+ 617         f.write(markdown)
+ 618     logger.info(f"Markdown rapport lagret: {rapport_fil_md}")
+ 619     
+ 620     # Send e-post
+ 621     send_epost_rapport(rapport, markdown)
+ 622     
+ 623     return rapport
+ 624 
+ 625 
+ 626 if __name__ == "__main__":
+ 627     asyncio.run(main())
