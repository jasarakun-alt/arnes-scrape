#!/usr/bin/env python3
"""
Pretvori podatki_povezave.json (iz scrape_podatki_gov_si.py) v CSV.
Ena vrstica na zbirko; več URL-jev v istem stolpcu združi z ločilom (privzeto |).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from typing import Any

LINK_KEYS = ("pdf", "csv", "xml", "html")


def join_urls(links: dict[str, Any], key: str, sep: str) -> str:
    urls = links.get(key) if isinstance(links, dict) else None
    if not urls:
        return ""
    if isinstance(urls, list):
        return sep.join(str(u).strip() for u in urls if str(u).strip())
    return str(urls)


def main() -> int:
    p = argparse.ArgumentParser(description="JSON → CSV za podatki_povezave.json")
    p.add_argument(
        "-i",
        "--input",
        default="podatki_povezave.json",
        help="Vhodna JSON datoteka.",
    )
    p.add_argument(
        "-o",
        "--output",
        default="podatki_povezave.csv",
        help="Izhodna CSV datoteka.",
    )
    p.add_argument(
        "--sep",
        default="|",
        help="Ločilo med več URL-ji v istem stolpcu (privzeto |).",
    )
    p.add_argument(
        "--delimiter",
        default=",",
        help="CSV ločilo polj (privzeto zarez). Uporabi '\\t' za TSV.",
    )
    args = p.parse_args()

    delim = args.delimiter
    if delim == "\\t":
        delim = "\t"

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    datasets = data.get("datasets")
    if not isinstance(datasets, list):
        print("Napaka: v JSON pričakujem ključ 'datasets' (seznam).", file=sys.stderr)
        return 1

    fieldnames = [
        "title",
        "name",
        "dataset_url",
        "pdf",
        "csv",
        "xml",
        "html",
    ]

    with open(args.output, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            delimiter=delim,
            quoting=csv.QUOTE_MINIMAL,
        )
        w.writeheader()
        for ds in datasets:
            if not isinstance(ds, dict):
                continue
            links = ds.get("links") or {}
            row = {
                "title": ds.get("title", ""),
                "name": ds.get("name", ""),
                "dataset_url": ds.get("dataset_url", ""),
            }
            for k in LINK_KEYS:
                row[k] = join_urls(links, k, args.sep)
            w.writerow(row)

    print(f"Zapisano: {args.output} ({len(datasets)} vrstic)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
