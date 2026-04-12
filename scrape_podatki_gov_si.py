#!/usr/bin/env python3
"""
Zbira povezave za prenos (PDF, CSV, XML, HTML) za vse zadetke iskanja na podatki.gov.si.

Uporablja uradni CKAN API (https://podatki.gov.si/api/3/), ne HTML strani — enak nabor
kot pri spletnem iskanju (npr. ?s=pdf ali filtri področje + odprti podatki), brez prenašanja datotek.

Za kombinacijo open_data=True & all_podrocje=… API uporabi poizvedbo oblike:
podrocje:\"…\" AND open_data:true (preverjeno število zadetkov z vmesnikom).

Za vsak vir preveri polje 'format' (primer: PDF, csv, HTML) in ujemanje je
neobčutljivo na velikost črk.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import requests

API_SEARCH = "https://podatki.gov.si/api/3/action/package_search"
DATASET_URL = "https://podatki.gov.si/data/dataset/{name}"

WANTED = frozenset({"pdf", "csv", "xml", "html"})


def norm_format(fmt: str | None) -> str:
    if not fmt:
        return ""
    return str(fmt).strip().lower()


def collect_links(resources: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in WANTED}
    for res in resources:
        fmt = norm_format(res.get("format"))
        if fmt not in WANTED:
            continue
        url = (res.get("url") or "").strip()
        if not url:
            continue
        out[fmt].append(url)
    return {k: v for k, v in out.items() if v}


def fetch_all(
    session: requests.Session,
    query: str,
    rows_per_page: int,
    delay_s: float,
) -> tuple[list[dict[str, Any]], int]:
    """Vrne (seznam paketov, skupno število)."""
    start = 0
    all_results: list[dict[str, Any]] = []
    total = None

    while True:
        r = session.get(
            API_SEARCH,
            params={"q": query, "start": start, "rows": rows_per_page},
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"API napaka: {data}")

        result = data["result"]
        if total is None:
            total = int(result["count"])
        batch = result["results"]
        if not batch:
            break
        all_results.extend(batch)
        start += len(batch)
        if start >= total:
            break
        if delay_s > 0:
            time.sleep(delay_s)

    return all_results, total or 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Zbere povezave PDF/CSV/XML/HTML za zadetke CKAN iskanja (podatki.gov.si)."
    )
    p.add_argument(
        "--query",
        default="pdf",
        help='Celoten Solr q niz (privzeto "pdf", kot /data/search?s=pdf). Če podaš --podrocje, se --query ignorira.',
    )
    p.add_argument(
        "--podrocje",
        default=None,
        metavar="NAZIV",
        help='Filtriraj po polju podrocje, npr. "Prebivalstvo in družba" (kot all_podrocje= na iskanju).',
    )
    p.add_argument(
        "--open-data",
        action="store_true",
        help="Skupaj s --podrocje: dodaj AND open_data:true (kot open_data=True na iskanju).",
    )
    p.add_argument(
        "--rows",
        type=int,
        default=100,
        help="Število paketov na en API klic (1–1000).",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Premor v sekundah med stranmi (privzeto 0).",
    )
    p.add_argument(
        "-o",
        "--output",
        default="podatki_povezave.json",
        help="Izhodna JSON datoteka.",
    )
    args = p.parse_args()

    if args.open_data and args.podrocje is None:
        print("Napaka: --open-data zahteva tudi --podrocje.", file=sys.stderr)
        return 2

    if args.podrocje is not None:
        q = f'podrocje:"{args.podrocje}"'
        if args.open_data:
            q += " AND open_data:true"
    else:
        q = args.query

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "podatki-gov-si-link-scraper/1.0 (+local script)",
            "Accept": "application/json",
        }
    )

    packages, total = fetch_all(session, q, args.rows, args.delay)

    if len(packages) != total:
        print(
            f"Opozorilo: pričakovano {total} paketov, prejeto {len(packages)}.",
            file=sys.stderr,
        )

    items: list[dict[str, Any]] = []
    for pkg in packages:
        name = pkg.get("name") or ""
        title = pkg.get("title") or name
        links = collect_links(pkg.get("resources") or [])
        items.append(
            {
                "title": title,
                "name": name,
                "dataset_url": DATASET_URL.format(name=name) if name else "",
                "links": links,
            }
        )

    out: dict[str, Any] = {
        "source": "https://podatki.gov.si",
        "api": API_SEARCH,
        "query": q,
        "total_datasets": total,
        "datasets": items,
    }
    if args.podrocje is not None:
        out["filters"] = {
            "podrocje": args.podrocje,
            "open_data": bool(args.open_data),
        }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"Zapisano: {args.output} ({len(items)} zbirk, query={q!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
