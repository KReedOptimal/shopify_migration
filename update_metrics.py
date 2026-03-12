#!/usr/bin/env python3
"""
Add a new dated snapshot to data.json, then commit and push to GitHub Pages.

Usage:
  python update_metrics.py              # interactive prompts (defaults to today)
  python update_metrics.py --no-push   # update data.json only, don't push
  python update_metrics.py --date 2026-03-15   # specify a date explicitly
"""

import copy
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

DATA_FILE = (Path(__file__).parent if "__file__" in dir() else Path.cwd()) / "data.json"


def load_data():
    with open(DATA_FILE) as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Saved {DATA_FILE}")


def prompt_float(label, current):
    val = input(f"    {label} [{current}]: ").strip()
    return float(val) if val else current


def prompt_int(label, current):
    val = input(f"    {label} [{current}]: ").strip()
    return int(val) if val else current


def prompt_str(label, current):
    val = input(f"    {label} [{current}]: ").strip()
    return val if val else current


def get_template(data):
    """Return the most recent snapshot as a template for new entries."""
    snapshots = data.get("snapshots", {})
    if not snapshots:
        return {
            "label": "",
            "pages_audited": 3,
            "pages": [
                {"name": "Homepage", "type": "Shopify", "lcp": 0.0, "cls": 0.0, "tbt": 0.0, "ttfb": 0.0, "score": 0},
                {"name": "PLP",      "type": "Shopify", "lcp": 0.0, "cls": 0.0, "tbt": 0.0, "ttfb": 0.0, "score": 0},
                {"name": "PDP",      "type": "Shopify", "lcp": 0.0, "cls": 0.0, "tbt": 0.0, "ttfb": 0.0, "score": 0},
            ]
        }
    latest_key = sorted(snapshots.keys())[-1]
    return copy.deepcopy(snapshots[latest_key])


def date_label(snapshot_date):
    """Generate 'Mar 4 – Mar 11, 2026' style label: 8 days ago through yesterday."""
    end = snapshot_date - timedelta(days=1)
    start = end - timedelta(days=7)
    start_str = f"{start.strftime('%b')} {start.day}"
    end_str   = f"{end.strftime('%b')} {end.day}, {end.year}"
    return f"{start_str} \u2013 {end_str}"


def collect_snapshot(snapshot_date, template):
    """Interactively collect metrics for a new snapshot."""
    label = date_label(snapshot_date)

    print("\n── Snapshot Metadata ─────────────────────────────────────")
    print("  (Press Enter to keep the shown default)\n")
    print(f"  Date range (auto): {label}")
    pages_audited = prompt_int("Pages audited", template["pages_audited"])

    print("\n── Page Metrics ──────────────────────────────────────────")
    pages = []
    for page in template["pages"]:
        print(f"\n  {page['name']} ({page['type']})")
        pages.append({
            "name":  page["name"],
            "type":  page["type"],
            "lcp":   prompt_float("LCP  (s)", page["lcp"]),
            "cls":   prompt_float("CLS     ", page["cls"]),
            "tbt":   prompt_float("TBT  (s)", page["tbt"]),
            "ttfb":  prompt_float("TTFB (s)", page["ttfb"]),
            "score": prompt_int  ("Lighthouse score", page["score"]),
        })

    return {
        "label": label,
        "pages_audited": pages_audited,
        "pages": pages,
    }

def parse_args():
    no_push = "--no-push" in sys.argv
    snapshot_date = date.today()
    if "--date" in sys.argv:
        idx = sys.argv.index("--date")
        try:
            snapshot_date = date.fromisoformat(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("  Error: --date requires a value in YYYY-MM-DD format.")
            sys.exit(1)
    return snapshot_date, no_push


def main():
    snapshot_date, no_push = parse_args()
    date_str = snapshot_date.isoformat()  # e.g. "2026-03-12"

    data = load_data()

    if date_str in data["snapshots"]:
        print(f"\n  Snapshot for {date_str} already exists.")
        overwrite = input("  Overwrite? [y/N]: ").strip().lower()
        if overwrite not in ("y", "yes"):
            print("  Aborted.")
            sys.exit(0)

    template = get_template(data)
    snapshot = collect_snapshot(snapshot_date, template)

    data["snapshots"][date_str] = snapshot
    save_data(data)

    print(f"\n  Added snapshot for {date_str} ({snapshot['label']})")

if __name__ == "__main__":
    main()
