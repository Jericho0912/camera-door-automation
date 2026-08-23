import os, json, requests
from dotenv import load_dotenv
load_dotenv()
h = {"Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
     "Notion-Version": os.getenv("NOTION_VERSION", "2026-03-11")}
db = requests.get(f"https://api.notion.com/v1/databases/{os.environ['NOTION_DATABASE_ID']}",
                  headers=h, timeout=30).json()
props = db.get("properties")
if not props and db.get("data_sources"):          # 2025-09-03+ splits schema onto the data source
    ds = db["data_sources"][0]["id"]
    props = requests.get(f"https://api.notion.com/v1/data_sources/{ds}", headers=h, timeout=30).json()["properties"]

want = {"Event ID":"title","Person":"select","Camera":"select","Seen":"date",
        "Duration (s)":"number","Score":"number","Segments":"number","Manifest key":"rich_text"}
if os.getenv("PUBLIC_BASE_URL"):                  # Clip is only written (and backfilled) when set
    want["Clip"] = "url"
print(f"{'PROPERTY':<16} {'ACTUAL':<12} {'WANTED':<12}")
for name, wtype in want.items():
    actual = props.get(name, {}).get("type", "-- MISSING --")
    print(f"{name:<16} {actual:<12} {wtype:<12} {'ok' if actual == wtype else '  <-- FIX'}")
extra = set(props) - set(want)
if extra: print("\nextra properties (harmless):", ", ".join(sorted(extra)))