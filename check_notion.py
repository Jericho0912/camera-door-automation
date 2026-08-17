#!/usr/bin/env python3
"""Preflight for the Notion sink.

Run this after creating the database in Notion's UI and before setting a real
NOTION_TOKEN in a live run. It checks, in the order the failures actually bite:

  1. the token and database id are present and readable
  2. the integration can see the database at all (the share step everyone forgets)
  3. which API shape the workspace is on (database_id vs data_source_id)
  4. every property the reconciler writes exists, with the right type and exact name

It creates nothing and writes nothing. Read-only.
"""
from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv

API = "https://api.notion.com/v1"

# Mirrors notion_properties() in reconciler.py. Keep the two in step.
EXPECTED = {
    "Event ID": "title",
    "Person": "select",
    "Camera": "select",
    "Seen": "date",
    "Duration (s)": "number",
    "Segments": "number",
    "Manifest key": "rich_text",
    "Score": "number",
}

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


def main() -> int:
    load_dotenv()
    token = os.getenv("NOTION_TOKEN", "").strip()
    db_id = os.getenv("NOTION_DATABASE_ID", "").strip()
    version = os.getenv("NOTION_VERSION", "2026-03-11").strip()

    if not token or not db_id:
        print(f"[{BAD}] NOTION_TOKEN and NOTION_DATABASE_ID must both be set in .env")
        return 1
    if not token.startswith(("ntn_", "secret_")):
        print(f"[{WARN}] token does not start with ntn_ or secret_ — is it the integration token?")

    headers = {"Authorization": f"Bearer {token}", "Notion-Version": version}
    try:
        resp = requests.get(f"{API}/databases/{db_id}", headers=headers, timeout=30)
    except requests.RequestException as exc:
        print(f"[{BAD}] could not reach api.notion.com: {exc}")
        return 1

    if resp.status_code == 404:
        print(f"[{BAD}] 404 — the integration cannot see this database.")
        print("        Almost always the sharing step, not a wrong id:")
        print("        open the database page -> ... menu -> Connections -> + Add connection")
        return 1
    if resp.status_code == 401:
        print(f"[{BAD}] 401 — the token is wrong or revoked.")
        return 1
    if resp.status_code >= 400:
        print(f"[{BAD}] {resp.status_code}: {resp.text[:300]}")
        return 1

    db = resp.json()
    title = "".join(t.get("plain_text", "") for t in db.get("title", []))
    print(f"[{OK}] reachable: {title or '(untitled)'}")

    sources = db.get("data_sources") or []
    if sources:
        print(f"[{OK}] workspace uses data sources — parent will be data_source_id={sources[0]['id']}")
        if len(sources) > 1:
            print(f"[{WARN}] {len(sources)} data sources present; the code uses the first one")
        props = fetch_props(f"{API}/data_sources/{sources[0]['id']}", headers)
    else:
        print(f"[{OK}] classic database — parent will be database_id={db_id}")
        props = db.get("properties") or {}

    if props is None:
        print(f"[{BAD}] could not read the property schema")
        return 1

    print()
    failures = 0
    for name, want in EXPECTED.items():
        got = props.get(name)
        if got is None:
            near = [p for p in props if p.lower().replace(" ", "") == name.lower().replace(" ", "")]
            hint = f" — found {near[0]!r}, names must match exactly" if near else ""
            print(f"[{BAD}] {name!r} missing (expected type {want}){hint}")
            failures += 1
        elif got.get("type") != want:
            print(f"[{BAD}] {name!r} is type {got.get('type')!r}, expected {want!r}")
            failures += 1
        else:
            print(f"[{OK}] {name!r} -> {want}")

    extra = [p for p in props if p not in EXPECTED]
    if extra:
        print(f"\n[{WARN}] columns the reconciler never writes (harmless): {', '.join(sorted(extra))}")

    if EXPECTED.get("Seen") and (props.get("Seen") or {}).get("type") == "date":
        print(f"\n[{WARN}] confirm 'Seen' has 'Include time' switched on — the code sends a "
              "start and end timestamp, and the API cannot tell you whether the column shows them")

    print()
    if failures:
        print(f"{failures} problem(s). Fix the column names/types in Notion, then re-run.")
        return 1

    include = os.getenv("NOTION_INCLUDE_PERSON", "false").strip().lower() in {"1", "true", "yes", "on"}
    print("Schema matches. " + (
        "NOTION_INCLUDE_PERSON is ON — real names will be published to Notion."
        if include else
        "NOTION_INCLUDE_PERSON is off — every page will read 'Unrecognized'."))
    return 0


def fetch_props(url: str, headers: dict[str, str]) -> dict | None:
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code >= 400:
        print(f"[{WARN}] could not read data source schema ({r.status_code}); falling back")
        return None
    return r.json().get("properties") or {}


if __name__ == "__main__":
    raise SystemExit(main())
