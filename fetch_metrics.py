#!/usr/bin/env python3
"""
Fetch page-level metrics from Splunk Synthetics via SignalFlow
and write them into the brand data files (same format as update_metrics.py).

Usage:
  python fetch_metrics.py                    # fetch and update for today's date
  python fetch_metrics.py --no-push          # fetch + write, skip git push
  python fetch_metrics.py --date 2026-03-25  # fetch for a specific snapshot date
  python fetch_metrics.py --dry-run          # print fetched data, write nothing
  python fetch_metrics.py --skip lab-series  # skip a brand

Credentials (set as environment variables or in a .env file beside this script):
  SPLUNK_TOKEN   Splunk Observability API access token (required)
  SPLUNK_REALM   Realm slug, e.g. us1, us2, eu0 (required)

Edit the CONFIG section below to match your Splunk Synthetics test names/IDs.
Run with --dry-run first to verify data is coming back before writing anything.
"""

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── Load .env if present ───────────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG — edit to match your Splunk Synthetics setup
# ══════════════════════════════════════════════════════════════════════════════

CONFIG = {
    # One entry per brand.  Each brand lists its Synthetics tests — one per page.
    # "test_name" must match the test name exactly as it appears in Splunk
    # Synthetics (it maps to the "test" dimension in SignalFlow).
    # Use "test_id" instead of "test_name" if you prefer to filter by ID.
    "brands": [
        {
            "key":   "tom-ford",
            "label": "Tom Ford",
            "file":  "data.json",
            "pages": [
                {"name": "Homepage", "test_name": "Tom Ford US Home (Mobile)"},
                {"name": "PLP",      "test_name": "Tom Ford US PLP (Mobile)"},
                {"name": "PDP",      "test_name": "Tom Ford US PDP (Mobile)"},
            ],
        },
        {
            "key":   "lab-series",
            "label": "Lab Series",
            "file":  "data-lab-series.json",
            "pages": [
                {"name": "Homepage", "test_name": "Lab Series US Home (Mobile)"},
                {"name": "PLP",      "test_name": "Lab Series US PLP (Mobile)"},
                {"name": "PDP",      "test_name": "Lab Series US PDP (Mobile)"},
            ],
        },
    ],

    # Splunk Synthetics metric names as they appear in SignalFlow.
    # Run --list-metrics to discover what's available for your tests.
    "metrics": {
        "lcp":   "synthetics.webvitals_lcp.time.ms",
        "cls":   "synthetics.webvitals_cls.score",
        "tbt":   "synthetics.webvitals_tbt.time.ms",
        "ttfb":  "synthetics.ttfb.time.ms",
        "score": "synthetics.lighthouse.score",
    },

    # Dimension name that identifies a test in SignalFlow.
    # Typically "test" or "test_name" — check your metric dimensions if unsure.
    "test_dimension": "test",

    # How to aggregate across the 7-day window.
    # "mean" gives the week's average; "percentile_75" gives P75.
    "aggregation": "mean",

    # Metrics whose values Splunk reports in milliseconds (will be converted to s).
    "millisecond_metrics": {"lcp", "tbt", "ttfb"},
}

# ══════════════════════════════════════════════════════════════════════════════
# End of CONFIG
# ══════════════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent

THRESHOLDS = {
    "lcp":   {"good": 2.5,  "warn": 4.0},
    "cls":   {"good": 0.1,  "warn": 0.25},
    "tbt":   {"good": 0.2,  "warn": 0.6},
    "ttfb":  {"good": 0.8,  "warn": 1.8},
    "score": {"good": 90,   "warn": 50},
}


# ── Utilities ──────────────────────────────────────────────────────────────────

def date_label(snapshot_date: date) -> str:
    end   = snapshot_date - timedelta(days=1)
    start = end - timedelta(days=7)
    return f"{start.strftime('%b')} {start.day} - {end.strftime('%b')} {end.day}, {end.year}"


def window_ms(snapshot_date: date):
    """Return (start_ms, stop_ms) covering 7 days ending yesterday (UTC)."""
    stop_dt  = datetime(snapshot_date.year, snapshot_date.month, snapshot_date.day,
                        tzinfo=timezone.utc) - timedelta(seconds=1)
    start_dt = stop_dt - timedelta(days=7)
    return int(start_dt.timestamp() * 1000), int(stop_dt.timestamp() * 1000)



def get_rating(key, val):
    t = THRESHOLDS[key]
    if key == "score":
        if val >= t["good"]: return "good"
        if val >= t["warn"]: return "warn"
        return "fail"
    if val <= t["good"]: return "good"
    if val <= t["warn"]: return "warn"
    return "fail"


# ── SignalFlow ─────────────────────────────────────────────────────────────────

def get_client(token: str, realm: str):
    try:
        import signalfx.signalflow as sf_mod
        return sf_mod.SignalFlowClient(
            token=token,
            endpoint=f"https://stream.{realm}.signalfx.com",
        )
    except ImportError:
        print("  ERROR: signalflow-client-python is not installed.")
        print("         Run: pip install signalflow-client-python")
        sys.exit(1)


def query_metric(client, metric: str, test_name: str, start_ms: int, stop_ms: int) -> float | None:
    """
    Fetch a single aggregated metric value for one Synthetics test over the window.
    Returns None if no data comes back.
    """
    test_dim = CONFIG["test_dimension"]
    agg      = CONFIG["aggregation"]

    program = (
        f"data('{metric}', filter=filter('{test_dim}', '{test_name}'))"
        f".{agg}()"
        f".publish()"
    )

    try:
        import signalfx.signalflow as sf_mod
        computation = client.execute(
            program,
            start=start_ms,
            stop=stop_ms,
            resolution=3600000,  # 1-hour buckets
            immediate=True,
        )
    except Exception as e:
        print(f"      SignalFlow error: {e}")
        return None

    values = []
    for msg in computation.stream():
        msg_type = type(msg).__name__
        if msg_type == "DataMessage":
            for tsid, value in msg.data.items():
                if value is not None:
                    values.append(float(value))

    if not values:
        return None

    return sum(values) / len(values)


def fetch_page_metrics(client, page: dict, start_ms: int, stop_ms: int) -> dict:
    """Fetch all metrics for a single page's Synthetics test."""
    test_name = page["test_name"]
    result    = {}

    for metric_key, metric_name in CONFIG["metrics"].items():
        print(f"      {metric_key:<6} ({metric_name})", end=" ... ", flush=True)
        value = query_metric(client, metric_name, test_name, start_ms, stop_ms)

        if value is None:
            print("no data")
        else:
            if metric_key in CONFIG["millisecond_metrics"]:
                value = round(value / 1000, 3)
            elif metric_key == "score":
                value = int(round(value))
            else:
                value = round(value, 3)
            result[metric_key] = value
            print(value)

    return result


# ── Snapshot helpers ───────────────────────────────────────────────────────────

def build_snapshot(brand: dict, fetched: dict[str, dict], label: str, existing_pages: list) -> dict:
    """
    Construct a snapshot dict.  Falls back to previous values (then 0) for
    any metric that returned no data.  Lighthouse score comes from Synthetics.
    """
    pages = []
    for page_cfg in brand["pages"]:
        name    = page_cfg["name"]
        data    = fetched.get(name, {})
        prev    = next((p for p in existing_pages if p["name"] == name), {})

        def val(key, default=0.0):
            return data.get(key, prev.get(key, default))

        pages.append({
            "name":  name,
            "type":  "Shopify",
            "lcp":   val("lcp"),
            "cls":   val("cls"),
            "tbt":   val("tbt"),
            "ttfb":  val("ttfb"),
            "score": val("score", 0),
        })

    return {"label": label, "pages_audited": len(pages), "pages": pages}


def print_snapshot(snapshot: dict):
    pages = snapshot["pages"]
    MARKS = {"good": "+", "warn": "~", "fail": "x"}
    header = f"  {'Page':<12}  {'LCP':>7}  {'CLS':>6}  {'TBT':>7}  {'TTFB':>7}  {'Score':>5}"
    print(f"\n  {snapshot['label']}")
    print(header)
    print("  " + "-" * (len(header) - 2))
    for p in pages:
        print(
            f"  {p['name']:<12}  "
            f"{p['lcp']:>6.3f}{MARKS[get_rating('lcp',   p['lcp'])]}  "
            f"{p['cls']:>5.3f}{MARKS[get_rating('cls',   p['cls'])]}  "
            f"{p['tbt']:>6.3f}{MARKS[get_rating('tbt',   p['tbt'])]}  "
            f"{p['ttfb']:>6.3f}{MARKS[get_rating('ttfb', p['ttfb'])]}  "
            f"{p['score']:>5}{MARKS[get_rating('score', p['score'])]}"
        )


# ── Metric discovery helper ────────────────────────────────────────────────────

def list_metrics(client, test_name: str, start_ms: int, stop_ms: int):
    """Print all metric names that have data for a given test name."""
    test_dim = CONFIG["test_dimension"]
    program  = f"data('synthetics.*', filter=filter('{test_dim}', '{test_name}')).publish()"
    print(f"\n  Querying all synthetics.* metrics for test: {test_name}")
    try:
        computation = client.execute(program, start=start_ms, stop=stop_ms,
                                     resolution=3600000, immediate=True)
        seen: set[str] = set()
        for msg in computation.stream():
            if type(msg).__name__ == "MetadataMessage":
                metric = (msg.properties or {}).get("sf_metric") or (msg.properties or {}).get("metric")
                if metric and metric not in seen:
                    print(f"    {metric}")
                    seen.add(metric)
    except Exception as e:
        print(f"  Error: {e}")


# ── Data I/O ───────────────────────────────────────────────────────────────────

def load_data(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def save_data(data: dict, path: Path):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Saved {path.name}")


def git_push(label: str, files: list[str]):
    try:
        subprocess.run(["git", "add"] + files, cwd=BASE_DIR, check=True)
        subprocess.run(["git", "commit", "-m", f"metrics: update scorecard {label}"],
                       cwd=BASE_DIR, check=True)
        subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)
        print("\n  Pushed to GitHub — Pages will update in ~1 minute.")
    except subprocess.CalledProcessError as e:
        print(f"\n  Git error: {e}")
        sys.exit(1)


# ── Arg parsing ────────────────────────────────────────────────────────────────

def parse_args():
    no_push      = "--no-push"      in sys.argv
    dry_run      = "--dry-run"      in sys.argv
    yes          = "--yes"          in sys.argv
    list_metrics_ = "--list-metrics" in sys.argv
    skip_keys: set[str] = set()

    if "--skip" in sys.argv:
        idx   = sys.argv.index("--skip")
        valid = {b["key"] for b in CONFIG["brands"]}
        try:
            raw = sys.argv[idx + 1]
            if raw not in valid:
                print(f"  Error: unknown brand '{raw}'. Valid: {', '.join(sorted(valid))}")
                sys.exit(1)
            skip_keys.add(raw)
        except IndexError:
            print("  Error: --skip requires a brand key.")
            sys.exit(1)

    snapshot_date = date.today()
    if "--date" in sys.argv:
        idx = sys.argv.index("--date")
        try:
            snapshot_date = date.fromisoformat(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("  Error: --date requires YYYY-MM-DD.")
            sys.exit(1)

    return snapshot_date, no_push, dry_run, yes, list_metrics_, skip_keys


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    snapshot_date, no_push, dry_run, yes, do_list_metrics, skip_keys = parse_args()
    interactive = sys.stdin.isatty() and not yes
    date_str  = snapshot_date.isoformat()
    label     = date_label(snapshot_date)
    start_ms, stop_ms = window_ms(snapshot_date)

    token = os.environ.get("SPLUNK_TOKEN")
    realm = os.environ.get("SPLUNK_REALM")
    if not token or not realm:
        print("\n  Missing credentials — set environment variables (or add to a .env file):")
        if not token: print("    SPLUNK_TOKEN=<your-api-token>")
        if not realm: print("    SPLUNK_REALM=<realm e.g. us1>")
        sys.exit(1)

    client = get_client(token, realm)

    # ── --list-metrics mode ────────────────────────────────────────────────────
    if do_list_metrics:
        active = [b for b in CONFIG["brands"] if b["key"] not in skip_keys]
        for brand in active:
            for page in brand["pages"]:
                list_metrics(client, page["test_name"], start_ms, stop_ms)
        client.close()
        return

    # ── Normal fetch mode ──────────────────────────────────────────────────────
    print(f"\n  Window : {label}")
    print(f"  Range  : {datetime.fromtimestamp(start_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d')} to "
          f"{datetime.fromtimestamp(stop_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d')} UTC")

    active_brands = [b for b in CONFIG["brands"] if b["key"] not in skip_keys]
    if not active_brands:
        print("  Error: all brands skipped.")
        sys.exit(1)

    updated_files: list[str] = []

    for brand in active_brands:
        print(f"\n{'=' * 58}")
        print(f"  Brand: {brand['label']}")
        print(f"{'=' * 58}")

        file_path = BASE_DIR / brand["file"]
        data      = load_data(file_path)

        if date_str in data["snapshots"] and not dry_run:
            print(f"\n  Snapshot for {date_str} already exists.")
            if not interactive or input("  Overwrite? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("  Skipping.")
                continue

        existing_pages: list = []
        if data["snapshots"]:
            latest_key    = sorted(data["snapshots"].keys())[-1]
            existing_pages = data["snapshots"][latest_key].get("pages", [])

        fetched: dict[str, dict] = {}
        for page_cfg in brand["pages"]:
            print(f"\n    {page_cfg['name']} - test: \"{page_cfg['test_name']}\"")
            fetched[page_cfg["name"]] = fetch_page_metrics(client, page_cfg, start_ms, stop_ms)

        snapshot = build_snapshot(brand, fetched, label, existing_pages)
        print_snapshot(snapshot)

        if dry_run:
            print("\n  [dry-run] Not writing to file.")
            continue

        data["snapshots"][date_str] = snapshot
        save_data(data, file_path)
        updated_files.append(brand["file"])

    try:
        client.close()
    except Exception:
        pass

    if dry_run or not updated_files:
        if not dry_run:
            print("\n  No files updated.")
        return

    if no_push:
        print("\n  Skipping git push (--no-push).")
    elif not interactive or input("\n  Push to GitHub now? [Y/n]: ").strip().lower() in ("", "y", "yes"):
        git_push(label, updated_files)
    else:
        files_str = " ".join(updated_files)
        print(f"  Not pushed. Run: git add {files_str} && git commit && git push")


if __name__ == "__main__":
    main()
