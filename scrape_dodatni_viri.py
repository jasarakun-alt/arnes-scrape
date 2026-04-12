#!/usr/bin/env python3
"""
Obisk predlaganih (seed) URL-jev in zbiranje neposrednih povezav do datotek (PDF, DOC, …),
podobno kot viri na OPSI — ne splošnih HTML povezav. Ujemanje z iskalnim nizom v URL-ju,
besedilu povezave ali naslovu strani (<title>).

Uporaba sama: python3 scrape_dodatni_viri.py -t TEMA --input viri.csv -o dodatni.csv
Integracija: openai_iskanje.py (privzeto scrape vključen).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from openai_iskanje import (
    is_acceptable_source_url,
    is_nav_boilerplate_url,
    is_search_or_listing_url,
    strip_utm_params,
)

DEFAULT_UA = (
    "Mozilla/5.0 (compatible; DodatniViri/1.0; +https://podatki.gov.si) "
    "Python-requests scrape-dodatni-viri"
)
MAX_HTML_BYTES = 6_000_000
_DOC_EXTENSIONS = (".pdf", ".doc", ".docx", ".odt", ".rtf", ".xml", ".zip")


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().replace("www.", "")
    except ValueError:
        return ""


def build_search_terms(topic: str, extra: str | None) -> list[str]:
    """Besede in kratke fraze za ujemanje v URL-ju in besedilu povezave."""
    raw = f"{topic or ''} {extra or ''}"
    parts = re.split(r"[\s,;|]+", raw)
    terms: list[str] = []
    seen: set[str] = set()
    for p in parts:
        t = p.strip().strip("\"'«»")
        if len(t) < 2:
            continue
        low = t.casefold()
        if low not in seen:
            seen.add(low)
            terms.append(t)
    if topic and topic.strip():
        phrase = topic.strip()
        if phrase.casefold() not in seen:
            terms.append(phrase)
    return terms


def _is_direct_file_url(url: str) -> bool:
    """Neposredna povezava do datoteke (kot vir na podatki.gov.si / CKAN)."""
    low = url.lower().split("#")[0]
    base = low.split("?", 1)[0]
    if any(base.endswith(ext) for ext in _DOC_EXTENSIONS):
        return True
    if "format=pdf" in low or "format%3Dpdf" in low:
        return True
    if "/download" in low and any(x in low for x in (".pdf", "pdf", "document", "dokument")):
        return True
    if "printpdf" in low or "exportpdf" in low or "getpdf" in low:
        return True
    return False


def _terms_match_blob(href: str, link_text: str, page_title: str, terms: list[str]) -> tuple[bool, str]:
    blob = unescape(f"{href} {link_text} {page_title}").casefold()
    for t in terms:
        if len(t) < 2:
            continue
        if t.casefold() in blob:
            return True, t
    return False, ""


def _page_title_from_soup(soup: BeautifulSoup) -> str:
    tag = soup.find("title")
    if not tag:
        return ""
    return tag.get_text(separator=" ", strip=True)[:600]


def collect_direct_file_links(
    page_url: str,
    html: str,
    terms: list[str],
    same_host_only: bool,
    seed_host: str,
    *,
    require_term_match: bool = True,
) -> list[tuple[str, str, str]]:
    """
    Vrne [(file_url, naslov, razlog_ujemanja), ...] samo za neposredne datoteke.
    Ujemanje: iskalni izraz v URL-ju, besedilu povezave ali naslovu strani (<title>).
    """
    soup = BeautifulSoup(html, "html.parser")
    title = _page_title_from_soup(soup)
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = (tag.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        abs_url = strip_utm_params(urljoin(page_url, href).split("#")[0])
        if abs_url in seen:
            continue
        if not _is_direct_file_url(abs_url):
            continue
        if not is_acceptable_source_url(abs_url):
            continue
        h2 = _host(abs_url)
        if same_host_only and h2 != seed_host:
            continue
        text = tag.get_text(separator=" ", strip=True)[:500]
        ok, reason = _terms_match_blob(abs_url, text, title, terms)
        if require_term_match and not ok:
            continue
        if not require_term_match and not ok:
            reason = "PDF/datoteka (stran zadetka iskanja)"
        seen.add(abs_url)
        label = text or abs_url.split("/")[-1][:200]
        out.append((abs_url, label, reason))
    return out


def internal_follow_urls(seed: str, page_url: str, html: str, max_n: int) -> list[str]:
    """Iste domene, HTML strani za sledenje (ne PDF)."""
    soup = BeautifulSoup(html, "html.parser")
    seed_host = _host(seed)
    out: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = (tag.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        abs_url = strip_utm_params(urljoin(page_url, href).split("#")[0])
        if abs_url in seen:
            continue
        if _is_direct_file_url(abs_url):
            continue
        if _host(abs_url) != seed_host:
            continue
        if is_nav_boilerplate_url(abs_url) or is_search_or_listing_url(abs_url):
            continue
        try:
            path = (urlparse(abs_url).path or "").lower()
        except ValueError:
            continue
        if path.endswith((".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2")):
            continue
        seen.add(abs_url)
        out.append(abs_url)
        if len(out) >= max_n:
            break
    return out


_PAGINATION_LABEL = re.compile(
    r"^(naslednja|prejšnja|zadnja|nazaj|stran|relevantnost|datum|rss|vrstični|pomoč|«|»|&lt;|&gt;)$",
    re.I,
)


def build_form_data_for_search(form: BeautifulSoup, topic: str) -> dict[str, str] | None:
    """Zgradi polja za oddajo iskalnika (GET/POST)."""
    data: dict[str, str] = {}
    text_names: list[str] = []
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "text").lower()
        if itype == "hidden":
            data[name] = inp.get("value") or ""
        elif itype == "checkbox":
            continue
        elif itype in ("text", "search", ""):
            text_names.append(name)
    for inp in form.find_all("input"):
        if not inp.get("type") and inp.get("name") and inp["name"] not in text_names:
            text_names.append(inp["name"])
    if not text_names:
        return None
    preferred = (
        "q",
        "query",
        "search",
        "searchword",
        "keywords",
        "text",
        "iskalni_niz",
        "p",
        "searchWord",
        "iskanje",
    )
    chosen = next((p for p in preferred if p in text_names), text_names[0])
    data[chosen] = topic
    n_cb = len(form.find_all("input", type="checkbox"))
    if n_cb > 1:
        for inp in form.find_all("input", type="checkbox"):
            name = inp.get("name")
            if name:
                data[name] = inp.get("value") or "on"
    else:
        for inp in form.find_all("input", type="checkbox"):
            name = inp.get("name")
            if name and inp.has_attr("checked"):
                data[name] = inp.get("value") or "on"
    for inp in form.find_all("input", type="submit"):
        if inp.get("name"):
            data[inp["name"]] = inp.get("value") or ""
            break
    for btn in form.find_all("button", type="submit"):
        if btn.get("name"):
            data[btn["name"]] = btn.get("value") or ""
            break
    return data


def _looks_like_search_results(html: str, baseline_len: int) -> bool:
    low = html.lower()
    if len(html) > baseline_len + 1200:
        return True
    if "zadetek" in low or "št. zadetkov" in low or "število zadetkov" in low:
        return True
    if "rezultat" in low and "id=" in html:
        return True
    return False


def try_site_search(
    seed: str,
    html: str,
    topic: str,
    timeout: int,
    session: requests.Session,
) -> tuple[str | None, str | None, str]:
    """
    Odda prvi smiseln iskalni obrazec na strani (GET/POST) in vrne HTML z zadetki.
    Vrne tudi končni URL odgovora (za relativne povezave v zadetkih).
    """
    soup = BeautifulSoup(html, "html.parser")
    baseline = len(html)
    for form in soup.find_all("form"):
        data = build_form_data_for_search(form, topic)
        if not data:
            continue
        action = (form.get("action") or "").strip()
        action_url = urljoin(seed, action) if action else seed
        method = (form.get("method") or "get").strip().lower()
        if method not in ("get", "post"):
            continue
        try:
            if method == "get":
                r = session.get(
                    action_url,
                    params=data,
                    timeout=timeout,
                    headers={"User-Agent": DEFAULT_UA, "Accept": "text/html,application/xhtml+xml"},
                    allow_redirects=True,
                )
            else:
                r = session.post(
                    action_url,
                    data=data,
                    timeout=timeout,
                    headers={"User-Agent": DEFAULT_UA, "Accept": "text/html,application/xhtml+xml"},
                    allow_redirects=True,
                )
            r.raise_for_status()
            ct = (r.headers.get("Content-Type") or "").lower()
            if "html" not in ct:
                continue
            body = r.text
            if _looks_like_search_results(body, baseline):
                return body, None, r.url
        except requests.RequestException:
            continue
    return None, None, seed


def extract_search_hit_urls(
    html: str,
    page_url: str,
    seed_host: str,
    max_n: int,
) -> list[tuple[str, str]]:
    """
    Povezave do posameznih zadetkov (npr. ?id=… na sodnapraksa.si).
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue
        abs_url = strip_utm_params(urljoin(page_url, href).split("#")[0])
        if abs_url in seen:
            continue
        if _host(abs_url) != seed_host:
            continue
        try:
            pq = parse_qs(urlparse(abs_url).query)
            if "id" not in pq or not pq["id"] or not str(pq["id"][0]).isdigit():
                continue
        except Exception:
            continue
        label = a.get_text(separator=" ", strip=True)[:500]
        first = (label.split() or [""])[0]
        if _PAGINATION_LABEL.match(first) or _PAGINATION_LABEL.match(label.strip()):
            continue
        if len(label) < 4 and len(label.split()) <= 1:
            continue
        seen.add(abs_url)
        out.append((abs_url, label))
        if len(out) >= max_n:
            break
    return out


def fetch_html(
    url: str,
    timeout: int,
    session: requests.Session | None = None,
) -> tuple[str | None, str | None]:
    try:
        req = session if session is not None else requests
        r = req.get(
            url,
            timeout=timeout,
            headers={"User-Agent": DEFAULT_UA, "Accept": "text/html,application/xhtml+xml"},
            allow_redirects=True,
            stream=True,
        )
        r.raise_for_status()
        ct = (r.headers.get("Content-Type") or "").lower()
        if "html" not in ct and "xml" not in ct:
            return None, f"ni HTML ({ct[:40]})"
        buf = bytearray()
        for chunk in r.iter_content(65536):
            buf.extend(chunk)
            if len(buf) >= MAX_HTML_BYTES:
                break
        enc = r.encoding or "utf-8"
        return buf.decode(enc, errors="replace"), None
    except requests.RequestException as e:
        return None, str(e)


def scrape_sources_for_extra_documents(
    sources: list[dict[str, str]],
    topic: str,
    *,
    extra_terms: str | None = None,
    delay_sec: float = 1.0,
    timeout: int = 22,
    same_host_only: bool = True,
    max_seed_pages: int = 40,
    max_output_links: int = 250,
    max_follow_per_seed: int = 8,
    max_search_hits: int = 25,
) -> tuple[list[dict[str, str]], str | None]:
    """
    Za vsak seed: če stran vsebuje iskalnik, odda poizvedbo z iskalno frazo (-t), iz zadetkov
    obišče strani odločb in izvleče PDF/printPdf ipd. Dodatno: neposredne datoteke na strani
    in opcijsko notranje povezave (max_follow_per_seed).
    """
    terms = build_search_terms(topic, extra_terms)
    if not terms:
        return [], "Ni iskalnih izrazov (podaj -t / --scrape-extra-terms)."

    seeds: list[str] = []
    seen_seed: set[str] = set()
    for s in sources:
        u = (s.get("url") or "").strip()
        if not u or u in seen_seed:
            continue
        seen_seed.add(u)
        seeds.append(u)
        if len(seeds) >= max_seed_pages:
            break

    if not seeds:
        return [], "Ni seed URL-jev za obisk (najprej zberi vire z openai_iskanje)."

    discovered: list[dict[str, str]] = []
    seen_out: set[str] = set()
    notes: list[str] = []
    session = requests.Session()

    def add_files(
        rows: list[tuple[str, str, str]],
        seed: str,
        page_label: str,
        *,
        prefix: str = "",
        hit_title: str = "",
    ) -> None:
        for file_url, naslov, reason in rows:
            if file_url in seen_out:
                continue
            seen_out.add(file_url)
            besedilo = f"{prefix}Datoteka; ujemanje: {reason}; stran: {page_label}"
            if hit_title:
                besedilo = f"{besedilo} | zadetek: {hit_title[:400]}"
            discovered.append(
                {
                    "url": file_url,
                    "naslov": naslov,
                    "besedilo": besedilo,
                    "opomba": f"seed: {seed}",
                    "vrsta": "scrape_dokument",
                }
            )
            if len(discovered) >= max_output_links:
                return

    for i, seed in enumerate(seeds):
        if i > 0 and delay_sec > 0:
            time.sleep(delay_sec)
        seed_host = _host(seed)
        html, err = fetch_html(seed, timeout=timeout, session=session)
        if err or not html:
            notes.append(f"{seed[:60]}…: {err or 'prazno'}")
            continue

        search_html, _, search_base = try_site_search(seed, html, topic, timeout, session)
        if search_html and max_search_hits > 0:
            hits = extract_search_hit_urls(search_html, search_base, seed_host, max_search_hits)
            qprefix = f"Iskanje «{topic.strip()[:100]}» | "
            for hit_url, hit_title in hits:
                if len(discovered) >= max_output_links:
                    break
                if delay_sec > 0:
                    time.sleep(delay_sec)
                hdoc, err_h = fetch_html(hit_url, timeout=timeout, session=session)
                if err_h or not hdoc:
                    notes.append(f"{hit_url[:50]}…: {err_h or 'prazno'}")
                    continue
                rows_h = collect_direct_file_links(
                    hit_url,
                    hdoc,
                    terms,
                    same_host_only,
                    seed_host,
                    require_term_match=False,
                )
                add_files(rows_h, seed, hit_url, prefix=qprefix, hit_title=hit_title)
                if len(discovered) >= max_output_links:
                    break

        if len(discovered) >= max_output_links:
            break

        rows = collect_direct_file_links(seed, html, terms, same_host_only, seed_host)
        add_files(rows, seed, seed)
        if len(discovered) >= max_output_links:
            break

        if max_follow_per_seed <= 0:
            continue
        follows = internal_follow_urls(seed, seed, html, max_follow_per_seed)
        for sub in follows:
            if len(discovered) >= max_output_links:
                break
            if delay_sec > 0:
                time.sleep(delay_sec)
            html2, err2 = fetch_html(sub, timeout=timeout, session=session)
            if err2 or not html2:
                notes.append(f"{sub[:50]}…: {err2 or 'prazno'}")
                continue
            rows2 = collect_direct_file_links(sub, html2, terms, same_host_only, seed_host)
            add_files(rows2, seed, sub)
            if len(discovered) >= max_output_links:
                break

    warn = None
    if notes:
        warn = "Scrape opozorila: " + "; ".join(notes[:8])
        if len(notes) > 8:
            warn += f" … (+{len(notes) - 8})"
    if not discovered:
        extra = (
            " Ni najdenih PDF/datotek (iskanje na strani, zadetki, notranje strani). "
            "Poskusi --scrape-extra-terms, večji --scrape-search-hits ali --scrape-follow."
        )
        warn = (warn + extra) if warn else extra.strip()
    return discovered, warn


def read_urls_from_csv(path: str, column: str = "vir_url") -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    povzetek: str | None = None
    with open(path, encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if (row.get("vrsta") or "") == "povzetek":
                povzetek = row.get("besedilo") or ""
            if (row.get("vrsta") or "") != "vir":
                continue
            u = (row.get(column) or "").strip()
            if u:
                out.append({"url": u, "naslov": (row.get("vir_naslov") or "").strip()})
    if not out and povzetek:
        from openai_iskanje import extract_urls_from_text_including_homepages

        seen: set[str] = set()
        for url, title in extract_urls_from_text_including_homepages(povzetek):
            if url in seen:
                continue
            seen.add(url)
            out.append({"url": url, "naslov": (title or "(iz povzetka)")[:500]})
            if len(out) >= 80:
                break
    return out


def write_scrape_csv(
    path: str,
    topic: str,
    rows: list[dict[str, str]],
    model: str = "scrape",
) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as f:
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
        for row in rows:
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
    ap = argparse.ArgumentParser(
        description="Obišči seed URL-je in zberi dodatne dokumentne povezave (ujemanje z iskalnim nizom)."
    )
    ap.add_argument("-t", "--topic", required=True, help="Iskalna tema / izrazi.")
    ap.add_argument(
        "--input",
        "-i",
        required=True,
        help="CSV iz openai_iskanje (vrstice vrsta=vir).",
    )
    ap.add_argument("-o", "--output", default="dodatni_viri_scrape.csv")
    ap.add_argument(
        "--scrape-extra-terms",
        default=None,
        help="Dodatni izrazi, ločeni s presledkom/vejico.",
    )
    ap.add_argument("--scrape-delay", type=float, default=1.0)
    ap.add_argument("--scrape-timeout", type=int, default=22)
    ap.add_argument("--scrape-max-pages", type=int, default=40)
    ap.add_argument("--scrape-max-links", type=int, default=250)
    ap.add_argument(
        "--scrape-follow",
        type=int,
        default=8,
        metavar="N",
        help="Notranje strani na seed za iskanje PDF (privzeto: 8; 0 = samo prva stran).",
    )
    ap.add_argument(
        "--scrape-search-hits",
        type=int,
        default=25,
        metavar="N",
        help="Največ zadetkov iskanja za obisk strani odločbe (privzeto: 25).",
    )
    ap.add_argument(
        "--scrape-any-host",
        action="store_true",
        help="Vključi tudi tuje domene (privzeto samo ista domena kot seed + dokumenti z ujemanjem).",
    )
    args = ap.parse_args()

    sources = read_urls_from_csv(args.input)
    if not sources:
        print(
            "Napaka: ni seed URL-jev (vrstice vrsta=vir) in v povzetku ni izvlečljivih povezav.",
            file=sys.stderr,
        )
        return 1

    rows, warn = scrape_sources_for_extra_documents(
        sources,
        args.topic,
        extra_terms=args.scrape_extra_terms,
        delay_sec=args.scrape_delay,
        timeout=args.scrape_timeout,
        same_host_only=not args.scrape_any_host,
        max_seed_pages=args.scrape_max_pages,
        max_output_links=args.scrape_max_links,
        max_follow_per_seed=args.scrape_follow,
        max_search_hits=args.scrape_search_hits,
    )
    write_scrape_csv(args.output, args.topic, rows)
    print(f"Zapisano {len(rows)} dodatnih povezav: {args.output}")
    if warn:
        print(warn, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
