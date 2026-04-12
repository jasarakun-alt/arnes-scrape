#!/usr/bin/env python3
"""
Raziskava teme prek OpenAI Responses API (opcijsko s spletnim iskanjem) in izvoz v CSV.

API ključ: okoljska spremenljivka OPENAI_API_KEY ali datoteka (privzeto keys.txt).
Pričakovana oblika v keys.txt: ena vrstica z besedilom OPENAI API: <ključ> ali samo ključ.

Opomba: brez orodja web_search model nima svežih podatkov iz spleta; z web_search_preview
lahko pridobi citate (url_citation) v odgovoru.

Dovoljeni so samo modeli z brezplačnim dnevnim prometom (deljen promet z OpenAI; glej
FREE_TIER_MODELS). Drugi modeli niso podprti — skripta jih zavrne.

Opcija --guide naloži dodaten vgrajen vodič (npr. guides/patria.md).

Privzeto (--dynamic-guide, aktivno razen če podaš --no-dynamic-guide) se za vsako temo
najprej z API-jem generira dinamičen vodič (s spletnim iskanjem), nato glavni odgovor – več
konteksta in ciljnejši viri.

Privzeto se ustvari mapa seje (iskanja/<tema>_<čas>/) z datotekama nacrt_iskanja.md in
rezultati.csv; po iskanju se obiščejo predlagani URL-ji in dodajo vrstice scrape_dodatni_vir
(izklopi z --no-scrape). Ločen zagon: scrape_dodatni_viri.py.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from openai import OpenAI
from openai import APIError

DEFAULT_MODEL = "gpt-4o-mini"
KEYS_FILE = "keys.txt"

# Mappa z vodiči (guides/patria.md) – opcija --guide patria
_GUIDE_REMINDER = (
    "VODIČ JE PRILOŽEN: pri spletnem iskanju aktivno uporabi predlagane domene, portale in iskalne "
    "fraze iz vodiča. Cilj je najti globoke URL-je do člankov in PDF-jev na teh mestih, ne splošnih "
    "naslovnic. Odgovor naj vključi kratek odgovor na vprašanje »kje in kako« po korakih. "
    "Če vodič ali tema zajemata sodno prakso (npr. sodnapraksa.si), v odgovor vključi ločene "
    "povezave do dejanskih dokumentov (PDF ali stran s polnim besedilom odločbe), ne do strani z "
    "rezultati iskanja ali samo prvega površnega URL-ja."
)


def _guides_dir() -> Path:
    return Path(__file__).resolve().parent / "guides"


def list_guide_names() -> list[str]:
    d = _guides_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.md"))


def load_guide_text(guide_arg: str) -> str:
    """
    guide_arg: ime vgrajenega vodiča (npr. patria) ali pot do .md/.txt
    """
    ga = guide_arg.strip()
    path_candidate = Path(ga)
    if path_candidate.suffix.lower() in (".md", ".txt", ".markdown"):
        if path_candidate.is_file():
            return path_candidate.read_text(encoding="utf-8")
        raise FileNotFoundError(f"Vodič ne obstaja: {path_candidate}")
    # vgrajen: guides/<ime>.md
    p = _guides_dir() / f"{ga}.md"
    if p.is_file():
        return p.read_text(encoding="utf-8")
    names = ", ".join(list_guide_names()) or "(ni datotek)"
    raise FileNotFoundError(
        f"Neznan vodič {ga!r}. V mapi guides/ pričakujem {ga}.md. Razpoložljivo: {names}"
    )

# Modeli z brezplačnim dnevnim prometom (OpenAI: »free daily usage on traffic shared with OpenAI«).
# Preostali modeli se zaračunajo po standardnih cenah — v tej skripti niso dovoljeni.
#
# Skupina A — približno 250.000 žetonov/dan:
FREE_TIER_MODELS_250K: frozenset[str] = frozenset(
    {
        "gpt-5.4",
        "gpt-5.2",
        "gpt-5.1",
        "gpt-5.1-codex",
        "gpt-5",
        "gpt-5-codex",
        "gpt-5-chat-latest",
        "gpt-4.1",
        "gpt-4o",
        "o1",
        "o3",
    }
)
# Skupina B — približno 2.500.000 žetonov/dan (mini/nano; priporočeno za pogoste zagon):
FREE_TIER_MODELS_2_5M: frozenset[str] = frozenset(
    {
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.1-codex-mini",
        "gpt-5-mini",
        "gpt-5-nano",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "gpt-4o-mini",
        "o1-mini",
        "o3-mini",
        "o4-mini",
        "codex-mini-latest",
    }
)

if FREE_TIER_MODELS_250K & FREE_TIER_MODELS_2_5M:
    raise RuntimeError("FREE_TIER_MODELS_250K in FREE_TIER_MODELS_2_5M se ne smeta prekrivati.")

FREE_TIER_MODELS: frozenset[str] = frozenset().union(
    FREE_TIER_MODELS_250K, FREE_TIER_MODELS_2_5M
)


def normalize_model(name: str) -> str:
    return name.strip()


def assert_free_model(model: str) -> None:
    """Dovoli samo modele iz FREE_TIER_MODELS (brezplačen dnevni deljeni promet)."""
    m = normalize_model(model)
    if m not in FREE_TIER_MODELS:
        raise SystemExit(
            f"Model {m!r} ni na seznamu brezplačnih modelov (skripta podpira samo te).\n"
            f"Izpiši seznam: python3 {Path(__file__).name} --list-free-models\n"
            f"Privzeti model z višjo kvoto: {DEFAULT_MODEL}"
        )


def load_api_key(path: Path) -> str:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"Prazna datoteka: {path}")
    if "OPENAI API:" in raw:
        key = raw.split("OPENAI API:", 1)[1].strip()
    else:
        key = raw.splitlines()[0].strip()
    if not key.startswith("sk-"):
        raise ValueError("Ključ ne izgleda kot OpenAI API ključ (pričakovan prefix sk-).")
    return key


def strip_utm_params(url: str) -> str:
    """Odstrani utm_* iz poizvedbe (čistejši CSV)."""
    try:
        p = urlparse(url.strip())
        pairs = [
            (k, v)
            for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")
        ]
        new_q = urlencode(pairs)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, ""))
    except Exception:
        return url.strip()


# Generične navigacijske / upravne strani (ne članki, ne sodbe o zadevi).
_NAV_BOILERPLATE_SUBSTRINGS = (
    "izjava-o-dostopnosti",
    "osnovne_informacije_o_sodiscu",
    "sodnikov_informator",
    "/esodstvo/index.html",
    "esodstvo/index.html",
    "/esodstvo/index",
)


def is_nav_boilerplate_url(url: str) -> bool:
    """True za kataloge, indekse e-Sodstva, izjave o dostopnosti ipd. (ne specifičen vir)."""
    u = url.lower()
    if any(s in u for s in _NAV_BOILERPLATE_SUBSTRINGS):
        return True
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower().replace("www.", "")
        path = (p.path or "").rstrip("/")
        if host == "poslovanje-sodstva.sodisce.si" and path in ("", "/"):
            return True
        if host == "portal.sodisce.si" and "esodstvo" in path and path.endswith("index.html"):
            return True
    except ValueError:
        return True
    return False


def is_search_or_listing_url(url: str) -> bool:
    """
    True za očitne strani zadetkov iskanja / sezname brez neposrednega dokumenta.
    PDF in tipični dokumentni parametri (id, docid, …) ostanejo sprejemljivi.
    """
    u = strip_utm_params(url)
    low = u.lower()
    if low.endswith(".pdf") or ".pdf?" in low or "/attachment" in low or "/attachments/" in low:
        return False
    try:
        p = urlparse(low)
    except ValueError:
        return True
    path = (p.path or "").lower()
    if any(
        seg in path
        for seg in (
            "/search",
            "/iskanje",
            "/rezultat",
            "/rezultati",
            "search.aspx",
            "search.do",
            "search.jsp",
            "/browse",
        )
    ):
        return True
    if path.rstrip("/").endswith("/search"):
        return True
    q = (p.query or "").lower()
    if not q:
        return False
    # Dokumentni identifikatorji v poizvedbi (ne čisti iskalni niz)
    if re.search(
        r"(^|&)(id|docid|documentid|dokument|cid|item|pid|uuid|legacyid|ecli|doc_id|document_id)=[^&]+",
        q,
    ):
        return False
    if re.search(r"(^|&)(q|query|searchtext|iskalni|keywords|text|search)=", q):
        path_trim = (p.path or "").rstrip("/").lower()
        if path_trim in ("", "/", "/index.html", "/index.php", "/index"):
            return True
        if "/search" in path or "/iskanje" in path or "/rezultat" in path:
            return True
    return False


def is_specific_resource_url(url: str) -> bool:
    """
    Zavrne generične naslovnice (samo domena ali /), sprejme globoke povezave in PDF.
    """
    u = url.strip()
    if not u:
        return False
    low = u.lower()
    if low.endswith(".pdf") or ".pdf?" in low or "/pdf" in low.split("?", 1)[0]:
        return True
    try:
        p = urlparse(u)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    path = (p.path or "").strip("/")
    if path:
        return True
    # Samo poizvedba, npr. ?id=123
    if p.query and len(p.query) > 2:
        return True
    return False


def is_acceptable_source_url(url: str) -> bool:
    """Kombinacija: globok URL, ni navigacijske šume in ni strani zgolj iskanja/seznama."""
    u = strip_utm_params(url)
    if not is_specific_resource_url(u):
        return False
    if is_nav_boilerplate_url(u):
        return False
    if is_search_or_listing_url(u):
        return False
    return True


def extract_urls_from_text(text: str) -> list[tuple[str, str]]:
    """Rezervni seznam (url, '') iz besedila; brez generičnih naslovnic."""
    urls = re.findall(r"https?://[^\s\)\]\"\'<>]+", text)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for u in urls:
        u = strip_utm_params(u.rstrip(".,;:"))
        if u in seen or not is_acceptable_source_url(u):
            continue
        seen.add(u)
        out.append((u, ""))
    return out


def is_pure_homepage(url: str) -> bool:
    """Domena brez poti (lahko seme za scrape, ne nujno dokument)."""
    try:
        p = urlparse(strip_utm_params(url))
        if p.scheme not in ("http", "https") or not p.netloc:
            return False
        if (p.path or "").strip("/"):
            return False
        return not (p.query or "").strip()
    except ValueError:
        return False


def _acceptable_for_summary_url(url: str) -> bool:
    if is_nav_boilerplate_url(url) or is_search_or_listing_url(url):
        return False
    return is_acceptable_source_url(url) or is_pure_homepage(url)


def extract_urls_from_text_including_homepages(text: str) -> list[tuple[str, str]]:
    """Markdown + prosti URL-ji; vključuje domače strani (semena za scrape)."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r"\[([^\]]*)\]\((https?://[^)]+)\)", text):
        url = strip_utm_params(m.group(2).strip().rstrip(".,;:"))
        title = (m.group(1) or "").strip()
        if url not in seen and _acceptable_for_summary_url(url):
            seen.add(url)
            pairs.append((url, title))
    for u in re.findall(r"https?://[^\s\)\]\"\'<>]+", text):
        u = strip_utm_params(u.rstrip(".,;:"))
        if u in seen or not _acceptable_for_summary_url(u):
            continue
        seen.add(u)
        pairs.append((u, ""))
    return pairs


def merge_sources_from_summary(
    summary: str,
    sources: list[dict[str, str]],
    max_results: int,
) -> tuple[list[dict[str, str]], str | None]:
    """
    Dopolni seznam virov z URL-ji iz besedila povzetka (tudi domače strani kot semena).
    """
    seen = {s.get("url", "") for s in sources if s.get("url")}
    out: list[dict[str, str]] = [dict(s) for s in sources]
    added = 0
    for url, title in extract_urls_from_text_including_homepages(summary):
        if url in seen:
            continue
        if max_results > 0 and len(out) >= max_results:
            break
        seen.add(url)
        label = title or "(iz povzetka)"
        if is_pure_homepage(url):
            label = f"{label} [površen URL — seme za obisk]"
        out.append({"url": url, "naslov": label})
        added += 1
    note = None
    if added:
        note = f"Dodanih {added} URL-jev iz besedila povzetka."
    return out, note


def slugify_topic(topic: str, max_len: int = 48) -> str:
    s = topic.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[-\s]+", "-", s)
    return (s[:max_len].strip("-") or "iskanje")


def write_nacrt_iskanja(
    path: Path,
    topic: str,
    model: str,
    *,
    guide_text: str | None,
    guide_label: str | None,
    warning: str | None,
    use_web: bool,
    no_dynamic_guide: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    parts = [
        "# Načrt iskanja",
        "",
        f"- **Tema:** {topic}",
        f"- **Model:** {model}",
        f"- **Spletno iskanje (API):** {'da' if use_web else 'ne'}",
        f"- **Dinamični vodič:** {'ne' if no_dynamic_guide else 'da'}",
        f"- **Čas:** {ts}",
    ]
    if guide_label:
        parts.append(f"- **Statični vodič:** {guide_label}")
    parts.extend(["", "---", ""])
    if guide_text and guide_text.strip():
        parts.append(guide_text.strip())
    else:
        parts.append("*(Vodič ni bil generiran ali je prazen.)*")
    if warning and warning.strip():
        parts.extend(["", "---", "", "## Opozorila", "", warning.strip()])
    path.write_text("\n".join(parts), encoding="utf-8")


def parse_response_citations(response: Any) -> tuple[str, list[dict[str, str]]]:
    """Vrne (celotno besedilo, seznam {url, naslov}) iz Responses objekta."""
    summary_parts: list[str] = []
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    output_text = getattr(response, "output_text", None) or ""

    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for block in getattr(item, "content", None) or []:
            if getattr(block, "type", None) != "output_text":
                continue
            txt = getattr(block, "text", "") or ""
            summary_parts.append(txt)
            for ann in getattr(block, "annotations", None) or []:
                if getattr(ann, "type", None) != "url_citation":
                    continue
                url = strip_utm_params((getattr(ann, "url", "") or "").strip())
                title = getattr(ann, "title", "") or ""
                if url and url not in seen_urls and is_acceptable_source_url(url):
                    seen_urls.add(url)
                    sources.append({"url": url, "naslov": title})

    full_text = output_text or "\n\n".join(summary_parts).strip()

    # Dodatno iz besedila (npr. markdown povezave), če model v besedilo vtakne globoke URL-je.
    if full_text:
        for url, title in extract_urls_from_text(full_text):
            url = strip_utm_params(url)
            if url not in seen_urls and is_acceptable_source_url(url):
                seen_urls.add(url)
                sources.append({"url": url, "naslov": title})

    return full_text, sources


def filter_sources_documents_only(
    sources: list[dict[str, str]],
) -> tuple[list[dict[str, str]], str | None]:
    """Odstrani naslovnice in navigacijske strani; vrne (filtrirano, opomba)."""
    kept: list[dict[str, str]] = []
    dropped = 0
    for s in sources:
        u = strip_utm_params((s.get("url") or "").strip())
        s2 = {**s, "url": u}
        if is_acceptable_source_url(u):
            kept.append(s2)
        else:
            dropped += 1
    note = None
    if dropped:
        note = (
            f"Zavrženih {dropped} URL-jev (naslovnica, indeks, stran iskanja/zadetkov, izjava o dostopnosti ipd.). "
            "V CSV naj bodo samo povezave do dejanskih dokumentov (PDF, stran s polnim besedilom) — ne seznami zadetkov."
        )
    return kept, note


def dedupe_paragraphs(text: str) -> str:
    """Odstrani ponovljene odstavke (model včasih zanka isti blok)."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return "\n\n".join(out)


def sanitize_summary(text: str) -> tuple[str, str | None]:
    """Skrajša ponavljanja; opomba če je bilo veliko odstranjeno."""
    if not text.strip():
        return text, None
    before = len(text)
    cleaned = dedupe_paragraphs(text)
    after = len(cleaned)
    note = None
    if after < before * 0.6:
        note = (
            f"Besedilo je bilo očiščeno ponovljenih odstavkov ({before}→{after} znakov). "
            "Preveri dejstva pri viru — model lahko zmotno ponavlja vsebino."
        )
    return cleaned, note


GUIDE_GENERATION_INSTRUCTIONS = """Ti pripravljaš SAMO raziskovalni vodič, ne končno poročilo o dejstvih.

Za podano temo v slovenščini (markdown) pripravi:
1) Katere VRSTE virov preveriti (npr. sodstvo, državni organi, parlament, mediji, odprti podatki, mednarodno).
2) Predlagane DOMENE in portale – uporabi spletno iskanje, da predlogi ustrezajo tej temi (splošno znane institucije + zadetki).
3) Iskalne FRAZE v slovenščini in kjer smiselno angleščini.
4) Kratek korak-po-korak postopek zbiranja dokumentov.

Pravila: ne izmišljuj konkretnih letnic ali sodnih izidov; ne ponavljaj odstavkov; največ ~1000 besed.
Izhod naj bodo samo naslovi in seznami, brez dolgega narrativega povzetka zgodovine.
Če tema zajema sodno prakso ali sodbe: omeni sodnapraksa.si (in po potrebi ustavno-sodisce.si, EUR-Lex);
pri korakih predlagaj iskanje po izrazu, nato odpiranje posameznega zadetka in kopiranje URL-ja do PDF-ja ali
strani s polnim besedilom odločbe — ne URL-ja do tabele zadetkov."""


def generate_dynamic_guide(
    client: OpenAI,
    topic: str,
    model: str,
    use_web: bool,
) -> tuple[str, str | None]:
    """
    Za trenutno temo generira vodič (1. API klic). Vrne (besedilo, opozorilo).
    """
    warn: str | None = None
    user = (
        f"Tema / raziskovalna zadeva:\n{topic}\n\n"
        "Pripravi vodič za vire in iskanje dokumentov (glej sistemska navodila)."
    )
    if use_web:
        try:
            r = client.responses.create(
                model=model,
                instructions=GUIDE_GENERATION_INSTRUCTIONS,
                input=user,
                tools=[{"type": "web_search_preview"}],
                max_output_tokens=3200,
                temperature=0.2,
            )
            text = (getattr(r, "output_text", None) or "").strip()
            if not text:
                text, _ = parse_response_citations(r)
                text = (text or "").strip()
            text, s_note = sanitize_summary(text)
            if s_note:
                warn = s_note
            return text, warn
        except APIError as e:
            return "", f"Dinamični vodič (splet) ni uspel: {e}"

    try:
        c = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": GUIDE_GENERATION_INSTRUCTIONS},
                {"role": "user", "content": user},
            ],
            max_tokens=2200,
            temperature=0.2,
            frequency_penalty=0.25,
        )
        text = (c.choices[0].message.content or "").strip()
        text, s_note = sanitize_summary(text)
        if s_note:
            warn = s_note
        return text, warn
    except APIError as e:
        return "", f"Dinamični vodič: {e}"


def merge_guide_sections(dynamic: str | None, static: str | None) -> str | None:
    """Združi dinamičen in statičen vodič za kontekst glavnega klica."""
    parts: list[str] = []
    if dynamic and dynamic.strip():
        parts.append("## Dinamično generiran vodič (za to temo)\n\n" + dynamic.strip())
    if static and static.strip():
        parts.append("## Dodatni vgrajen vodič (datoteka)\n\n" + static.strip())
    if not parts:
        return None
    return "\n\n---\n\n".join(parts)


RESEARCH_INSTRUCTIONS = """Odgovarjaj v slovenščini.

Natančnost in zanka:
- NE izmišljuj letnic, imen sodnikov, izrečenih kazni ali prihodnjih dogodkov. Če podatka ni v viru, reci, da ni preverljivo.
- NE ponavljaj istega odstavka ali bloka besedila. Največ 10 kratkih odstavkov + kratek seznam virov.
- Če ne najdeš neposredne povezave do sodbe, navedi članek, ki opisuje zadevo, z globokim URL-jem.

Viri (spletno iskanje):
- Ne navajaj splošnih portalov sodišč (e-Sodstvo index, izjava o dostopnosti, splošna stran občine).
- Za vsak vir navedi URL do DEJANSKEGA DOKUMENTA: PDF, uradna objava v HTML z polnim besedilom, uradno sporočilo z vsebino — ne strani z zadetki iskanja, ne praznega indeksa, ne same kategorije brez besedila.
- Za vsak vir uporabi globok URL do članka, sporočila za javnost ali PDF (sodba, sklep), ki eksplicitno obravnava iskano temo.
- Uporabi url_citation iz iskanja; ne izmišljuj naslovov strani.

Sodna praksa in baze odločb (npr. sodnapraksa.si, podobni iskalniki):
- Če tema zahteva pregled sodnih odločb ali »sodne prakse«, z iskanjem poišči ustrezno bazo, nato za vsak relevanten zadetek sledi do strani z besedilom odločbe ali do PDF-ja (pogosto povezava »pogled dokumenta« / »PDF«).
- V odgovor vključi ločene URL-je do posameznih dokumentov (polno besedilo ali PDF). Ne navajaj kot vira URL-ja do tabele zadetkov, iskalnika ali strani »rezultati iskanja«.
- Vsak dokument naj ima lasten url_citation ali ekspliciten URL v besedilu.
- Ne zadosti ena sama povezava na naslovnico baze; navedi toliko dokumentov, kolikor smiselno podpira temo (do omejitve v uporabniškem navodilu).
- Če do PDF-ja ne moreš priti, navedi najgloblji URL do strani z branjem celotne odločbe in to izrecno označi."""


def run_research(
    client: OpenAI,
    topic: str,
    model: str,
    use_web: bool,
    max_results: int,
    allow_homepage_sources: bool,
    guide_text: str | None = None,
) -> tuple[str, list[dict[str, str]], str | None]:
    """
    Vrne (povzetek, viri, opozorilo).

    max_results: največ število vrstic virov (0 = brez omejitve). Po obdelavi seznam obrežemo.
    """
    warn: str | None = None
    instructions = RESEARCH_INSTRUCTIONS
    if guide_text:
        instructions = instructions + "\n\n" + _GUIDE_REMINDER
    limit_hint = ""
    if max_results > 0:
        limit_hint = (
            f"\n\nOmeji se na največ {max_results} različnih dokumentov (vsak z lastnim URL-jem do dejanskega "
            "dokumenta: PDF ali stran s polnim besedilom — ne strani z zadetki iskanja)."
        )
    user_input = (
        f"Raziskovalna zadeva: {topic}\n\n"
        "Napiši kratek, preverljiv povzetek samo s podatki, ki jih podpirajo navedeni viri. "
        "Brez izmišljenih letnic ali ponavljanja istega besedila. "
        "Če je vprašanje »kje najdem dokumente«, strukturiraj odgovor po vrsti virov (sodstvo, DZ, mediji, tujina) "
        "in ob vsakem navedi konkretne portale z globokimi povezavami iz iskanja. "
        "Če tema zajema sodno prakso, sodbe ali zbirke odločb (npr. sodnapraksa.si), v besedilo vključi URL-je do "
        "posameznih dokumentov (PDF ali polno besedilo odločbe), ne do strani z rezultati iskanja."
        f"{limit_hint}"
    )
    if guide_text:
        user_input += (
            "\n\n--- VODIČ ZA VIRE IN ISKANJE (UPORABI PRI POIZVEDOVANJU IN STRUKTURI ODGOVORA) ---\n\n"
            + guide_text.strip()
        )

    if use_web:
        try:
            response = client.responses.create(
                model=model,
                instructions=instructions,
                input=user_input,
                tools=[{"type": "web_search_preview"}],
                max_output_tokens=3500,
                temperature=0.15,
            )
            text, sources = parse_response_citations(response)
            text, s_note = sanitize_summary(text)
            if s_note:
                warn = f"{warn} {s_note}" if warn else s_note
            if not allow_homepage_sources:
                sources, fnote = filter_sources_documents_only(sources)
                if fnote:
                    warn = f"{warn} {fnote}" if warn else fnote
            return _apply_source_limit(text, sources, max_results, warn)
        except APIError as e:
            warn = f"Spletno iskanje ni uspelo ({e}); uporabljen je način brez spleta."
            use_web = False

    if not use_web:
        comp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": instructions},
                {
                    "role": "user",
                    "content": user_input
                    + "\n\nNa koncu naštej samo POLNE URL-je do dejanskih dokumentov (PDF ali stran s polnim "
                    "besedilom odločbe/članka), ne do strani z iskanjem ali seznamom zadetkov.",
                },
            ],
            max_tokens=2800,
            temperature=0.15,
            frequency_penalty=0.35,
        )
        msg = comp.choices[0].message.content or ""
        text = msg.strip()
        text, s_note = sanitize_summary(text)
        if s_note:
            warn = f"{warn} {s_note}" if warn else s_note
        _, sources = parse_response_citations(
            SimpleNamespace(output_text=text, output=[])
        )
        if not allow_homepage_sources:
            sources, fnote = filter_sources_documents_only(sources)
            if fnote:
                warn = f"{warn} {fnote}" if warn else fnote
        return _apply_source_limit(text, sources, max_results, warn)


def _apply_source_limit(
    text: str,
    sources: list[dict[str, str]],
    max_results: int,
    warn: str | None,
) -> tuple[str, list[dict[str, str]], str | None]:
    if max_results <= 0 or len(sources) <= max_results:
        return text, sources, warn
    n = len(sources)
    trimmed = sources[:max_results]
    extra = f"Omejeno na {max_results} virov (najdenih {n})."
    if warn:
        warn = f"{warn} {extra}"
    else:
        warn = extra
    return text, trimmed, warn


def write_csv(
    path: Path,
    topic: str,
    model: str,
    summary: str,
    sources: list[dict[str, str]],
    warning: str | None,
    guide_csv_text: str | None = None,
    scraped_extra: list[dict[str, str]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "zadeva",
                "model",
                "vrsta",
                "besedilo",
                "vir_url",
                "vir_naslov",
                "opomba",
            ],
        )
        w.writeheader()
        w.writerow(
            {
                "zadeva": topic,
                "model": model,
                "vrsta": "povzetek",
                "besedilo": summary,
                "vir_url": "",
                "vir_naslov": "",
                "opomba": warning or "",
            }
        )
        if guide_csv_text and guide_csv_text.strip():
            w.writerow(
                {
                    "zadeva": topic,
                    "model": model,
                    "vrsta": "generiran_vodic",
                    "besedilo": guide_csv_text.strip(),
                    "vir_url": "",
                    "vir_naslov": "",
                    "opomba": "",
                }
            )
        for s in sources:
            w.writerow(
                {
                    "zadeva": topic,
                    "model": model,
                    "vrsta": "vir",
                    "besedilo": "",
                    "vir_url": s.get("url", ""),
                    "vir_naslov": s.get("naslov", ""),
                    "opomba": "",
                }
            )
        for row in scraped_extra or []:
            w.writerow(
                {
                    "zadeva": topic,
                    "model": model,
                    "vrsta": row.get("vrsta") or "scrape_dokument",
                    "besedilo": row.get("besedilo", ""),
                    "vir_url": row.get("url", ""),
                    "vir_naslov": row.get("naslov", ""),
                    "opomba": row.get("opomba", ""),
                }
            )


def main() -> int:
    p = argparse.ArgumentParser(
        description="OpenAI raziskava teme + shranjevanje povzetka in virov v CSV (projekt Sinji_delfini / odprti podatki)."
    )
    p.add_argument(
        "-t",
        "--topic",
        default=None,
        help="Iskana zadeva (npr. 'odprti podatki o javnih financah v Sloveniji').",
    )
    p.add_argument(
        "-o",
        "--output",
        default="rezultati.csv",
        help="Ime izhodne CSV v mapi seje (privzeto: rezultati.csv); z --no-run-dir polna pot.",
    )
    p.add_argument(
        "--no-run-dir",
        action="store_true",
        help="Ne ustvari mape seje; zapiši samo eno CSV na pot -o (npr. patria_viri.csv v trenutni mapi).",
    )
    p.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        metavar="MAPA",
        help="Eksplicitna mapa seje (nacrt_iskanja.md + CSV). Če ni podano: iskanja/<tema>_<čas>/.",
    )
    p.add_argument(
        "--iskanja-base",
        type=Path,
        default=None,
        metavar="MAPA",
        help="Korenska mapa za avtomatsko sejo (privzeto: mapa iskanja ob skripti).",
    )
    p.add_argument(
        "--keys",
        type=Path,
        default=Path(KEYS_FILE),
        help=f"Pot do datoteke s ključem (privzeto: {KEYS_FILE}).",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model (privzeto {DEFAULT_MODEL}; samo brezplačni, glej --list-free-models).",
    )
    p.add_argument(
        "--max-results",
        type=int,
        default=100,
        metavar="N",
        help="Največ število virov (URL vrstic) v CSV; 0 = brez omejitve (privzeto: 100).",
    )
    p.add_argument(
        "--list-free-models",
        action="store_true",
        help="Izpiši seznam dovoljenih brezplačnih modelov (z oznako kvote) in končaj.",
    )
    p.add_argument(
        "--no-web",
        action="store_true",
        help="Ne uporabi spletnega iskanja (samo model; brez svežih URL-jev iz iskanja).",
    )
    p.add_argument(
        "--allow-homepage-sources",
        action="store_true",
        help="Ne filtriraj generičnih naslovnic (privzeto globoki URL/PDF). Strani iskanja in seznami zadetkov ostanejo zavrnjeni.",
    )
    p.add_argument(
        "--guide",
        default=None,
        metavar="IME|DATOTEKA.md",
        help="Vgrajen vodič (npr. patria = guides/patria.md) ali pot do lastnega .md/.txt z navodili za vire.",
    )
    p.add_argument(
        "--list-guides",
        action="store_true",
        help="Izpiši imena vgrajenih vodičev (guides/*.md) in končaj.",
    )
    p.add_argument(
        "--no-dynamic-guide",
        action="store_true",
        help="Ne generiraj vodiča za temo (privzeto: da – najprej klic za dinamičen vodič, nato glavni odgovor).",
    )
    p.add_argument(
        "--no-scrape",
        action="store_true",
        help="Ne obišči predlaganih strani (privzeto: scrape dodatnih povezav je vključen).",
    )
    p.add_argument(
        "--scrape-extra-terms",
        default=None,
        metavar="BESEDILO",
        help="Dodatni iskalni izrazi za scrape (ločeno s presledkom/vejico), poleg -t.",
    )
    p.add_argument(
        "--scrape-delay",
        type=float,
        default=1.0,
        metavar="S",
        help="Premor med obiski strani v sekundah (privzeto: 1.0).",
    )
    p.add_argument(
        "--scrape-max-pages",
        type=int,
        default=40,
        metavar="N",
        help="Največ seed strani za scrape (privzeto: 40).",
    )
    p.add_argument(
        "--scrape-max-links",
        type=int,
        default=250,
        metavar="N",
        help="Največ najdenih PDF/datotek v CSV (privzeto: 250).",
    )
    p.add_argument(
        "--scrape-follow",
        type=int,
        default=8,
        metavar="N",
        help="Največ notranjih strani na seed za iskanje PDF/datotek (privzeto: 8; 0 = samo prva stran).",
    )
    p.add_argument(
        "--scrape-search-hits",
        type=int,
        default=25,
        metavar="N",
        help="Največ zadetkov iskanja na strani za obisk (PDF na strani odločbe; privzeto: 25).",
    )
    p.add_argument(
        "--scrape-any-host",
        action="store_true",
        help="Pri scrape dovolj tudi tuje domene (privzeto predvsem ista domena kot seed).",
    )
    args = p.parse_args()

    if args.list_guides:
        for name in list_guide_names():
            print(name)
        if not list_guide_names():
            print("(prazno: dodaj guides/*.md ob strani skripte)", file=sys.stderr)
        return 0

    if args.list_free_models:
        print(
            "Brezplačen dnevni promet (deljen z OpenAI). "
            "Poraba nad limitom in drugi modeli se zaračunajo po ceniku — ta skripta jih ne uporablja.\n"
        )
        print("~250.000 žetonov/dan:")
        for m in sorted(FREE_TIER_MODELS_250K):
            print(f"  {m}")
        print("\n~2.500.000 žetonov/dan (mini/nano):")
        for m in sorted(FREE_TIER_MODELS_2_5M):
            print(f"  {m}")
        return 0

    if not args.topic or not str(args.topic).strip():
        p.error("Zahtevan je -t / --topic (razen pri --list-free-models).")

    assert_free_model(args.model)
    model_used = normalize_model(args.model)

    if args.max_results < 0:
        print("Napaka: --max-results mora biti >= 0.", file=sys.stderr)
        return 1

    static_guide_text: str | None = None
    guide_arg_label: str | None = None
    if args.guide:
        guide_arg_label = args.guide.strip()
        try:
            static_guide_text = load_guide_text(args.guide)
        except FileNotFoundError as e:
            print(f"Napaka: {e}", file=sys.stderr)
            return 1

    script_dir = Path(__file__).resolve().parent
    iskanja_base = args.iskanja_base if args.iskanja_base is not None else script_dir / "iskanja"
    run_dir: Path | None = None
    if not args.no_run_dir:
        run_dir = args.run_dir
        if run_dir is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_dir = iskanja_base / f"{slugify_topic(args.topic)}_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        if not args.keys.is_file():
            print(
                f"Napaka: ni OPENAI_API_KEY in datoteka {args.keys} ne obstaja.",
                file=sys.stderr,
            )
            return 1
        key = load_api_key(args.keys)

    client = OpenAI(api_key=key)

    use_web = not args.no_web
    warn_pre: str | None = None
    merged_guide: str | None = None

    if not args.no_dynamic_guide:
        dynamic_guide_raw, gwarn = generate_dynamic_guide(
            client, args.topic, model_used, use_web=use_web
        )
        if gwarn:
            warn_pre = gwarn
        merged_guide = merge_guide_sections(
            dynamic_guide_raw.strip() if dynamic_guide_raw else None,
            static_guide_text,
        )
    else:
        merged_guide = merge_guide_sections(None, static_guide_text)

    guide_text = merged_guide
    guide_for_csv = merged_guide.strip() if merged_guide else None

    summary, sources, warn = run_research(
        client,
        args.topic,
        model_used,
        use_web=use_web,
        max_results=args.max_results,
        allow_homepage_sources=args.allow_homepage_sources,
        guide_text=guide_text,
    )
    if warn_pre:
        warn = f"{warn_pre} | {warn}" if warn else warn_pre

    sources, merge_note = merge_sources_from_summary(summary, sources, max_results=args.max_results)
    if merge_note:
        warn = f"{warn} | {merge_note}" if warn else merge_note

    # V CSV ne zapisujemo površnih domačih strani (ostanejo samo semena za scrape PDF).
    sources_for_csv = [
        s for s in sources if not is_pure_homepage((s.get("url") or "").strip())
    ]

    scraped_extra: list[dict[str, str]] = []
    if not args.no_scrape:
        from scrape_dodatni_viri import scrape_sources_for_extra_documents

        scraped_extra, scrape_warn = scrape_sources_for_extra_documents(
            sources,
            args.topic,
            extra_terms=args.scrape_extra_terms,
            delay_sec=args.scrape_delay,
            same_host_only=not args.scrape_any_host,
            max_seed_pages=args.scrape_max_pages,
            max_output_links=args.scrape_max_links,
            max_follow_per_seed=args.scrape_follow,
            max_search_hits=args.scrape_search_hits,
        )
        if scrape_warn:
            warn = f"{warn} | {scrape_warn}" if warn else scrape_warn

    if args.no_run_dir:
        out = Path(args.output)
        if not out.is_absolute():
            out = Path.cwd() / out
    else:
        assert run_dir is not None
        out = run_dir / Path(args.output).name
        write_nacrt_iskanja(
            run_dir / "nacrt_iskanja.md",
            args.topic,
            model_used,
            guide_text=guide_for_csv,
            guide_label=guide_arg_label,
            warning=warn,
            use_web=use_web,
            no_dynamic_guide=args.no_dynamic_guide,
        )

    write_csv(
        out,
        args.topic,
        model_used,
        summary,
        sources_for_csv,
        warn,
        guide_csv_text=guide_for_csv,
        scraped_extra=scraped_extra,
    )

    n_rows = (
        1
        + (1 if guide_for_csv and guide_for_csv.strip() else 0)
        + len(sources_for_csv)
        + len(scraped_extra)
    )
    print(f"Zapisano CSV: {out.resolve()}")
    if run_dir is not None:
        print(f"Mapa seje: {run_dir.resolve()}")
        print(f"Načrt: {(run_dir / 'nacrt_iskanja.md').resolve()}")
    print(
        f"Vrstic (povzetek + opcijski vodič + viri"
        f"{' + scrape' if scraped_extra else ''}): {n_rows}"
    )
    if warn:
        print(f"Opozorilo: {warn}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
