🔭 LovSonar – Strategisk Fremtidsovervåking & Risikoanalyse
Et Open Source-verktøy for tidlig varsling av politiske forslag, EU-direktiver og bransjetrender.

🔮 Om prosjektet
Mens tradisjonelle verktøy (som LovRadar) passer på lovene som gjelder i dag, er LovSonar designet for å se inn i fremtiden. Dette er et strategisk verktøy.

Målet er å fange opp politiske signaler og kommende EU-krav før de blir vedtatt, slik at virksomheter kan omstille seg kostnadseffektivt og unngå panikktiltak.

Status: 🟢 Live (Pilotfase)

🎯 Hva speider verktøyet etter?
Systemet skanner løpende etter signaler som kan påvirke byggevarehandelens og varehandelens rammevilkår 1–5 år frem i tid. Det overvåker spesifikke nøkkelord (f.eks. torvuttak, engangsplast, ombruk, digitale produktpass) i tre hovedkanaler:

1. Norsk Politikk & Lovarbeid 🇳🇴
Stortingsforslag: Hva foreslår partiene (f.eks. forbud, avgifter)?

Høringer & NOU-er: Offentlige utredninger som ofte blir lov 1-2 år senere.

Regjeringsplattformer: Signaler om satsingsområder (sirkulærøkonomi, energi).

2. EU & EØS-signaler 🇪🇺
Green Deal-pakker: Kommende forordninger (ESPR, PPWR).

EØS-notater: Hvilke EU-lover er på vei inn i norsk rett?

Standardisering: Nye ISO/NS-krav til byggevarer.

3. Bransje & Marked 🏗️
Bransjeorganisasjoner: Rapporter/utspill fra aktører som Virke og NHO.

Konkurranselandskap: Trender innen bærekraft, digitalisering og AI i varehandelen.

🤖 Hvordan det virker (Workflow)
LovSonar er bygget på Python og kjører automatisk via GitHub Actions. Prosessen er todelt:

Fangst & Filtrering (Python):

Roboten henter inn nye RSS-strømmer fra Regjeringen og Stortinget.

Sorterer bort støy ved hjelp av en definert søkeliste ("Keywords").

Lagrer relevante treff i en database for å unngå duplikater.

Analyse & Strategi (AI-støttet):

Systemet genererer en ukentlig rapport.

Rapporten er klargjort for behandling med LLM (Large Language Model), som vurderer:

Sannsynlighet: Blir dette faktisk lov?

Konsekvens: Treffer dette bunnlinjen eller driften?

Tidshorisont: Når må vi være klare?

🛠 Teknisk Stack
Språk: Python 3.10

Database: SQLite

Automasjon: GitHub Actions (Cron jobs)

Varsling: E-post (SMTP) med AI-ready prompts.
