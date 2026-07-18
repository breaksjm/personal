#!/usr/bin/env python3
"""Collect official USSA/USSS race pages for offline overlap analysis."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://my-dev.usskiandsnowboard.org"
MEMBER_ID = "4943163"
FIS_ID = "534266"
DISCIPLINES = ("SL", "GS", "SG", "DH")
EXPECTED = {"SL": 72, "GS": 50, "SG": 7, "DH": 5}
RACE_RE = re.compile(r"/ussa-tools/events/results/([^/?#]+)/([0-9]{4})(?:[/?#]|$)", re.I)
MEMBER_RE = re.compile(r"/ussa-tools/history/(\d+)(?:$|[/?#])", re.I)
HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/126.0 Safari/537.36"}
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
RAW = OUT / "raw_html"


def text(node: Tag | BeautifulSoup) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def fetch(session: requests.Session, url: str) -> requests.Response:
    last = None
    for attempt in range(4):
        try:
            response = session.get(url, headers=HEADERS, timeout=90, allow_redirects=True)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last = exc
            time.sleep(attempt + 1)
    raise RuntimeError(f"fetch failed for {url}: {last}")


def archive(name: str, response: requests.Response) -> dict:
    path = RAW / f"{name}.html.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as handle:
        handle.write(response.content)
    return {
        "url": response.url,
        "status": response.status_code,
        "sha256": hashlib.sha256(response.content).hexdigest(),
        "path": str(path.relative_to(ROOT)),
        "bytes": len(response.content),
    }


def closest_row(anchor: Tag) -> Tag:
    for tag in ("tr", "li"):
        parent = anchor.find_parent(tag)
        if parent is not None:
            return parent
    for parent in anchor.parents:
        if isinstance(parent, Tag):
            classes = " ".join(parent.get("class", [])).lower()
            if any(token in classes for token in ("view-row", "views-row", "table-row")):
                return parent
    return anchor.parent if isinstance(anchor.parent, Tag) else anchor


def cells(row: Tag) -> list[str]:
    found = row.find_all(["td", "th"], recursive=True)
    if found:
        return [text(cell) for cell in found]
    found = row.find_all(["div", "span"], recursive=False)
    values = [text(cell) for cell in found if text(cell)]
    return values or [text(row)]


def normalize_date(value: str) -> str:
    for pattern in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            pass
    return ""


def history_rows(content: bytes, page_url: str, discipline: str) -> list[dict]:
    soup = BeautifulSoup(content, "html.parser")
    title = text(soup.select_one("h1")) if soup.select_one("h1") else ""
    if "BREAKSTONE" not in title.upper() or "JASON" not in title.upper():
        raise RuntimeError(f"identity missing from history title: {title}")
    rows = []
    seen = set()
    for anchor in soup.select('a[href*="/ussa-tools/events/results/"]'):
        href = urljoin(page_url, str(anchor.get("href", "")))
        match = RACE_RE.search(urlparse(href).path + "/")
        if not match:
            continue
        code, year = match.group(1).upper(), match.group(2)
        key = (code, year, discipline)
        if key in seen:
            continue
        seen.add(key)
        row = closest_row(anchor)
        values = cells(row)
        padded = values + [""] * max(0, 8 - len(values))
        prefix_match = re.match(r"[A-Z]+", code)
        prefix = prefix_match.group(0) if prefix_match else ""
        rows.append({
            "discipline": discipline,
            "race_code": code,
            "season_year": year,
            "race_key": f"{code}/{year}",
            "date_raw": padded[1],
            "date": normalize_date(padded[1]),
            "event_name": padded[2],
            "location": padded[3],
            "jay_result": padded[4],
            "jay_time": padded[5],
            "race_points": padded[6],
            "ussa_points": padded[7],
            "prefix": prefix,
            "system": "FIS" if prefix == "F" else "USSA_NON_FIS",
            "url": href,
            "source_cells": values,
            "source_text": text(row),
        })
    return rows


def header_values(table: Tag) -> list[str]:
    values = [text(cell) for cell in table.select("thead th, thead td")]
    if values:
        return values
    for row in table.select("tr"):
        direct = row.find_all(["th", "td"], recursive=False)
        if direct and any(cell.name == "th" for cell in direct):
            return [text(cell) for cell in direct]
    return []


def table_payload(table: Tag, page_url: str, index: int) -> dict:
    rows = []
    for row_index, row in enumerate(table.select("tr"), start=1):
        row_cells = row.find_all(["th", "td"], recursive=False)
        if not row_cells:
            row_cells = row.find_all(["th", "td"], recursive=True)
        links = []
        for anchor in row.select("a[href]"):
            href = urljoin(page_url, str(anchor.get("href", "")))
            match = MEMBER_RE.search(href)
            links.append({
                "text": text(anchor),
                "href": href,
                "member_id": match.group(1) if match else "",
            })
        rows.append({
            "index": row_index,
            "text": text(row),
            "cells": [text(cell) for cell in row_cells],
            "cell_tags": [cell.name for cell in row_cells],
            "links": links,
        })
    return {
        "index": index,
        "id": str(table.get("id", "")),
        "classes": list(table.get("class", [])),
        "headers": header_values(table),
        "rows": rows,
    }


def page_payload(content: bytes, page_url: str) -> dict:
    soup = BeautifulSoup(content, "html.parser")
    return {
        "title": text(soup.select_one("h1")) if soup.select_one("h1") else "",
        "headings": [text(tag) for tag in soup.select("h1, h2, h3, h4")],
        "tables": [table_payload(table, page_url, index) for index, table in enumerate(soup.select("table"), 1)],
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).isoformat()
    with requests.Session() as session:
        member_url = f"{BASE}/ussa-tools/history/{MEMBER_ID}"
        member_response = fetch(session, member_url)
        member_text = text(BeautifulSoup(member_response.content, "html.parser"))
        if "Jason M Breakstone" not in member_text or MEMBER_ID not in member_text or FIS_ID not in member_text:
            raise RuntimeError("official member identity did not verify")
        member_evidence = archive(f"member_{MEMBER_ID}", member_response)

        all_history = []
        history_evidence = []
        for discipline in DISCIPLINES:
            url = f"{BASE}/ussa-tools/portal/history/races/{MEMBER_ID}/ALP/{discipline}"
            response = fetch(session, url)
            evidence = archive(f"history_{discipline}", response)
            rows = history_rows(response.content, response.url, discipline)
            if len(rows) != EXPECTED[discipline]:
                raise RuntimeError(f"expected {EXPECTED[discipline]} {discipline} rows; found {len(rows)}")
            all_history.extend(rows)
            history_evidence.append({"discipline": discipline, "rows": len(rows), **evidence})

        non_fis = [row for row in all_history if row["system"] == "USSA_NON_FIS"]
        if len(all_history) != 134 or len(non_fis) != 94:
            raise RuntimeError(f"unexpected history counts: total={len(all_history)} non_fis={len(non_fis)}")

        pages = []
        for number, race in enumerate(non_fis, start=1):
            response = fetch(session, race["url"])
            evidence = archive(f"race_{race['race_code']}_{race['season_year']}", response)
            pages.append({"race": race, "evidence": evidence, "page": page_payload(response.content, response.url)})
            print(f"[{number:02d}/94] {race['race_key']}")
            time.sleep(0.05)

    output = {
        "generated_at_utc": generated,
        "source_host": BASE,
        "member_id": MEMBER_ID,
        "member_evidence": member_evidence,
        "history_evidence": history_evidence,
        "history_rows": all_history,
        "non_fis_race_count": len(non_fis),
        "pages": pages,
    }
    (OUT / "usss_raw_collection.json").write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Collected {len(pages)} official non-FIS USSS result pages.")


if __name__ == "__main__":
    main()
