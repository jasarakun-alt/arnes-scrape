# arnes-scrape

Zbiranje virov in odprtih podatkov: **OpenAI** (iskanje z vodičem + CSV), **CKAN API** za [podatki.gov.si](https://podatki.gov.si), ter **scrape** strani z iskalniki in izvleček povezav do dokumentov (PDF ipd.).

## Zahteve

- Python 3.9+ (priporočeno 3.10+)
- Odvisnosti:

```bash
pip install -r requirements.txt
```

Paketi: `requests`, `openai`, `beautifulsoup4`.

## Konfiguracija API ključa

- Okoljska spremenljivka **`OPENAI_API_KEY`**, ali
- Datoteka **`keys.txt`** v korenu projekta (ena vrstica: `OPENAI API: sk-...` ali samo ključ).

> **`keys.txt` je v `.gitignore`** — ne commitaj ključev v git.

## `openai_iskanje.py`

Raziskava teme prek **OpenAI Responses API** (opcijsko **`web_search_preview`**), združen **dinamični + statični vodič**, izvoz v **CSV**.

### Privzeti tok

1. Ustvari mapo seje: **`iskanja/<tema-slug>_<YYYYMMDD_HHMMSS>/`**
2. Zapiše **`nacrt_iskanja.md`** (načrt / vodič)
3. Zapiše **`rezultati.csv`** (povzetek, vodič, viri, rezultati scrapa)

### Vrstice v CSV (`vrsta`)

| Vrednost | Pomen |
|----------|--------|
| `povzetek` | Besedilo odgovora modela |
| `generiran_vodic` | Združen vodič (dinamičen + opcijski `--guide`) |
| `vir` | URL-ji iz citatov API + globoki URL-ji iz povzetka (brez »golih« domačih strani v CSV) |
| `scrape_dokument` | PDF / printPdf / podobne datoteke (iskanje na strani, zadetki, notranje strani) |

### Modeli

Dovoljeni so **samo modeli z brezplačnim dnevnim prometom** (deljen promet z OpenAI). Seznam:

```bash
python3 openai_iskanje.py --list-free-models
```

Privzeti model: **`gpt-4o-mini`**.

### Pomembnejši argumenti

| Argument | Opis |
|----------|------|
| `-t`, `--topic` | Iskalna tema / zadeva (obvezen) |
| `-o`, `--output` | Ime CSV v mapi seje (privzeto `rezultati.csv`) ali s `--no-run-dir` polna/relativna pot |
| `--no-run-dir` | Brez mape `iskanja/…` — samo ena datoteka (npr. `-o patria_viri.csv`) |
| `--run-dir` | Fiksna mapa seje |
| `--iskanja-base` | Koren za avtomatske mape (privzeto `iskanja/` ob skripti) |
| `--keys` | Pot do datoteke s ključem |
| `--model` | Ime modela (mora biti na seznamu brezplačnih) |
| `--max-results` | Največ vrstic `vir` v CSV (`0` = brez omejitve) |
| `--no-web` | Brez spletnega iskanja v API-ju |
| `--guide` | Vgrajen vodič (`patria` → `guides/patria.md`) ali pot do `.md`/`.txt` |
| `--no-dynamic-guide` | Brez prvega klica za dinamični vodič |
| `--no-scrape` | Brez obiska strani in `scrape_dokument` |
| `--scrape-extra-terms` | Dodatni izrazi za scrape |
| `--scrape-delay` | Premor med HTTP zahtevami (s) |
| `--scrape-max-pages` | Največ seed URL-jev |
| `--scrape-max-links` | Največ najdenih dokumentov v CSV |
| `--scrape-follow` | Notranje strani na seed (privzeto `8`; `0` = samo prva stran) |
| `--scrape-search-hits` | Največ zadetkov iskanja na strani (obišče stran odločbe → PDF) |
| `--scrape-any-host` | Pri scrape tudi tuje domene (previdno) |
| `--allow-homepage-sources` | Omejeno ublažitev filtrov URL pri citatih |
| `--list-guides` / `--list-free-models` | Seznam in izhod |

### Primeri

```bash
cd "/Users/jasa/Desktop/arnes scrape"

python3 openai_iskanje.py -t "Patria oklepniki korupcija" --max-results 30 --model gpt-4o-mini

python3 openai_iskanje.py -t "odprti podatki javne finance" --guide patria --no-scrape

python3 openai_iskanje.py -t "tema" -o rezultati.csv --no-run-dir
```

## `scrape_dodatni_viri.py`

Samostojen zagon na obstoječem CSV-ju (vrstice **`vir`** ali URL-ji iz **`povzetek`**). Isti scrape kot v `openai_iskanje` (iskalnik, zadetki, PDF).

```bash
python3 scrape_dodatni_viri.py -t "patria" -i rezultati.csv -o dodatni.csv
```

Argumenti `--scrape-*` so enakovredni kot pri glavni skripti (glej `--help`).

## `scrape_podatki_gov_si.py`

Branje **CKAN API** `package_search` na **podatki.gov.si** — JSON z zbirkami in povezavami do virov (PDF, CSV, XML, HTML).

```bash
python3 scrape_podatki_gov_si.py --query "pdf" -o podatki_povezave.json

python3 scrape_podatki_gov_si.py --podrocje "Prebivalstvo in družba" --open-data -o podatki.json
```

## `json_to_csv.py`

Pretvorba izhoda `scrape_podatki_gov_si.py` v CSV:

```bash
python3 json_to_csv.py -i podatki_povezave.json -o podatki_povezave.csv
```

## `guides/`

Vgrajeni vodiči za `--guide <ime>` (datoteka `guides/<ime>.md`). Seznam:

```bash
python3 openai_iskanje.py --list-guides
```

## Git in GitHub

- V repozitorij **ne** gredo: `keys.txt`, `.env`, mapa **`iskanja/`** (generirane seje), `venv/`.
- Ustvarjanje repozitorija in prvi push (zahteva prijavljen **`gh`**):

```bash
./scripts/ustvari_github_repo.sh
# ali z drugim imenom:
./scripts/ustvari_github_repo.sh moje-ime-repozitorija
```

Nadaljnji pushi: `git add`, `git commit`, `git push`.

## Omejitve in opombe

- **Spletno iskanje** v OpenAI je odvisno od orodja in kvot; brez `--no-web` so odgovori bolj sveži.
- **Scrape** ne more nadomestiti brskalnika za strani, kjer je vse v **JavaScriptu** brez `<form>` v HTML.
- **sodnapraksa.si**: skripta odda iskalni obrazec, prebere zadetke (`id=…`), obišče stran odločbe in išče **PDF / printPdf**.
- Za **podatki.gov.si** so neposredni URL virov najzanesljiveje prek **`scrape_podatki_gov_si.py`** (CKAN), ne HTML iskanja.

## Licenca / raba

Uporaba skript je na lastno odgovornost; spoštuj **robote**, **pogoje strani** in **zakon o osebnih podatkih** pri obdelavi vsebin.
