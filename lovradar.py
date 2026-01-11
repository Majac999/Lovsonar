Current runner version: '2.330.0'
Runner Image Provisioner
Operating System
Runner Image
GITHUB_TOKEN Permissions
Secret source: Actions
Prepare workflow directory
Prepare all required actions
Getting action download info
Download action repository 'actions/checkout@v4' (SHA:34e114876b0b11c390a56381ad16ebd13914f8d5)
Download action repository 'actions/setup-python@v5' (SHA:a26af69be951a213d495a4c3e4e4022e16d87065)
Complete job name: run_compliance_system
0s
Run actions/checkout@v4
Syncing repository: Majac999/Lovsonar
Getting Git version info
Temporarily overriding HOME='/home/runner/work/_temp/1d7cbf32-f78e-4234-8046-96695d1f5b18' before making global git config changes
Adding repository directory to the temporary git global config as a safe directory
/usr/bin/git config --global --add safe.directory /home/runner/work/Lovsonar/Lovsonar
Deleting the contents of '/home/runner/work/Lovsonar/Lovsonar'
Initializing the repository
Disabling automatic garbage collection
Setting up auth
Fetching the repository
Determining the checkout info
/usr/bin/git sparse-checkout disable
/usr/bin/git config --local --unset-all extensions.worktreeConfig
Checking out the ref
/usr/bin/git log -1 --format=%H
432e590b9047bff53eb3a4072eafba0d0bbbb434
1s
Run actions/setup-python@v5
Installed versions
2s
Run python -m pip install --upgrade pip
Requirement already satisfied: pip in /opt/hostedtoolcache/Python/3.11.14/x64/lib/python3.11/site-packages (25.3)
Collecting requests (from -r requirements.txt (line 1))
  Downloading requests-2.32.5-py3-none-any.whl.metadata (4.9 kB)
Collecting beautifulsoup4 (from -r requirements.txt (line 2))
  Downloading beautifulsoup4-4.14.3-py3-none-any.whl.metadata (3.8 kB)
Collecting lxml (from -r requirements.txt (line 3))
  Downloading lxml-6.0.2-cp311-cp311-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl.metadata (3.6 kB)
Collecting charset_normalizer<4,>=2 (from requests->-r requirements.txt (line 1))
  Downloading charset_normalizer-3.4.4-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (37 kB)
Collecting idna<4,>=2.5 (from requests->-r requirements.txt (line 1))
  Downloading idna-3.11-py3-none-any.whl.metadata (8.4 kB)
Collecting urllib3<3,>=1.21.1 (from requests->-r requirements.txt (line 1))
  Downloading urllib3-2.6.3-py3-none-any.whl.metadata (6.9 kB)
Collecting certifi>=2017.4.17 (from requests->-r requirements.txt (line 1))
  Downloading certifi-2026.1.4-py3-none-any.whl.metadata (2.5 kB)
Collecting soupsieve>=1.6.1 (from beautifulsoup4->-r requirements.txt (line 2))
  Downloading soupsieve-2.8.1-py3-none-any.whl.metadata (4.6 kB)
Collecting typing-extensions>=4.0.0 (from beautifulsoup4->-r requirements.txt (line 2))
  Downloading typing_extensions-4.15.0-py3-none-any.whl.metadata (3.3 kB)
Downloading requests-2.32.5-py3-none-any.whl (64 kB)
Downloading charset_normalizer-3.4.4-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (151 kB)
Downloading idna-3.11-py3-none-any.whl (71 kB)
Downloading urllib3-2.6.3-py3-none-any.whl (131 kB)
Downloading beautifulsoup4-4.14.3-py3-none-any.whl (107 kB)
Downloading lxml-6.0.2-cp311-cp311-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl (5.2 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 5.2/5.2 MB 133.4 MB/s  0:00:00
Downloading certifi-2026.1.4-py3-none-any.whl (152 kB)
Downloading soupsieve-2.8.1-py3-none-any.whl (36 kB)
Downloading typing_extensions-4.15.0-py3-none-any.whl (44 kB)
Installing collected packages: urllib3, typing-extensions, soupsieve, lxml, idna, charset_normalizer, certifi, requests, beautifulsoup4

Successfully installed beautifulsoup4-4.14.3 certifi-2026.1.4 charset_normalizer-3.4.4 idna-3.11 lxml-6.0.2 requests-2.32.5 soupsieve-2.8.1 typing-extensions-4.15.0 urllib3-2.6.3
14s
Run python lovradar.py
2026-01-11 11:00:52,227 [INFO] ============================================================
2026-01-11 11:00:52,227 [INFO] LovSonar v3.0 - Strategisk Fremtidsovervåking
2026-01-11 11:00:52,228 [INFO] ============================================================
2026-01-11 11:00:52,234 [INFO] 🏛️ Henter data fra Stortinget...
2026-01-11 11:00:53,636 [INFO]   [LOW] Representantforslag fra stortingsrepresentantene Sunniva Hol...
2026-01-11 11:00:53,645 [INFO]   Prosessert 261 fra saker
2026-01-11 11:00:55,124 [INFO]   Prosessert 116 fra horinger
2026-01-11 11:00:55,126 [INFO] 📡 Sjekker RSS-feeds...
2026-01-11 11:00:56,533 [INFO]   [CRITICAL] Oppdatert veiledning om aktsomhetsvurderinger...
2026-01-11 11:00:56,533 [INFO]   [MEDIUM] Ulovlig praksis ved salg av strømavtaler ved overtakelse av ...
2026-01-11 11:00:56,534 [INFO]   [HIGH] Forbrukertilsynet avslutter sak mot Equinor...
2026-01-11 11:00:56,534 [INFO]   ✓ ⚖️ Forbrukertilsynet: 10 innlegg
Error: -11 11:00:57,064 [ERROR]   ✗ 🏗️ DiBK Nyheter: 404 Client Error: Not Found for url: https://www.dibk.no/nyheter/rss/
2026-01-11 11:00:57,066 [INFO] 📜 Sjekker lover for endringer...
Error: -11 11:01:01,623 [ERROR]   ✗ Byggevareforskriften (DOK): 404 Client Error: Not Found for url: https://lovdata.no/dokument/SF/forskrift/2014-12-17-1714
2026-01-11 11:01:03,587 [INFO]   Sjekket 7 dokumenter
2026-01-11 11:01:05,222 [INFO] 📧 Rapport sendt til ***
2026-01-11 11:01:05,224 [INFO] ============================================================
2026-01-11 11:01:05,224 [INFO] Ferdig. 4 signaler, 0 lovendringer.
2026-01-11 11:01:05,224 [INFO] ============================================================

# LOVSONAR STRATEGISK RAPPORT
Generert: 11.01.2026 11:01

Fokus: EU Green Deal, bærekraft, compliance, grønnvasking.
======================================================================

## 🚨 KRITISKE SIGNALER

### Oppdatert veiledning om aktsomhetsvurderinger
- Kilde: ⚖️ Forbrukertilsynet
- Score: 9.7 | Compliance/Markedsføring
- Nøkkelord: forbruker, åpenhetsloven, aktsomhet, forbrukertilsynet, ikrafttredelse
- Lenke: https://www.forbrukertilsynet.no/oppdatert-veiledning-om-aktsomhetsvurderinger

## ⚡ HØY PRIORITET

### Forbrukertilsynet avslutter sak mot Equinor
- Kilde: ⚖️ Forbrukertilsynet | Score: 7.2
- Nøkkelord: forbruker, åpenhetsloven, aktsomhet, forbrukertilsynet
- Lenke: https://www.forbrukertilsynet.no/forbrukertilsynet-avslutter-sak-mot-equinor

## 📋 MEDIUM PRIORITET

- Ulovlig praksis ved salg av strømavtaler ved overtakelse av bolig... (Score: 5.7)

0s
Run git config --global user.name "LovSonar Bot"
[main 128a68d] Oppdaterte ukentlig database [skip ci]
 2 files changed, 32 insertions(+)
 create mode 100644 lovsonar_cache.json
 create mode 100644 lovsonar_v3.db
To https://github.com/Majac999/Lovsonar
   432e590..128a68d  main -> main
1s
Post job cleanup.
0s
Post job cleanup.
/usr/bin/git version
git version 2.52.0
Copying '/home/runner/.gitconfig' to '/home/runner/work/_temp/1b688d23-e2f3-426c-8203-0c6bdb23ad16/.gitconfig'
Temporarily overriding HOME='/home/runner/work/_temp/1b688d23-e2f3-426c-8203-0c6bdb23ad16' before making global git config changes
Adding repository directory to the temporary git global config as a safe directory
/usr/bin/git config --global --add safe.directory /home/runner/work/Lovsonar/Lovsonar
/usr/bin/git config --local --name-only --get-regexp core\.sshCommand
/usr/bin/git submodule foreach --recursive sh -c "git config --local --name-only --get-regexp 'core\.sshCommand' && git config --local --unset-all 'core.sshCommand' || :"
/usr/bin/git config --local --name-only --get-regexp http\.https\:\/\/github\.com\/\.extraheader
http.https://github.com/.extraheader
/usr/bin/git config --local --unset-all http.https://github.com/.extraheader
/usr/bin/git submodule foreach --recursive sh -c "git config --local --name-only --get-regexp 'http\.https\:\/\/github\.com\/\.extraheader' && git config --local --unset-all 'http.https://github.com/.extraheader' || :"
/usr/bin/git config --local --name-only --get-regexp ^includeIf\.gitdir:
/usr/bin/git submodule foreach --recursive git config --local --show-origin --name-only --get-regexp remote.origin.url
0s
Cleaning up orphan processes

if __name__ == "__main__":
    main()
