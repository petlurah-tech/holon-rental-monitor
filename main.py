from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable

import requests
from dateutil import parser as date_parser

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SEEN_PATH = ROOT / "seen.json"

BRIGHTDATA_DATASET_ID = "gd_lz11l67o2cb3r0lkj3"
BRIGHTDATA_SCRAPE_URL = "https://api.brightdata.com/datasets/v3/scrape"

AGROBANK_STREETS: dict[str, list[str]] = {
    "אזר": ["אזר"],
    "אליהו קראוזה": ["אליהו קראוזה", "קראוזה"],
    "אליעזר בן יהודה": ["אליעזר בן יהודה", "בן יהודה"],
    "חנה סנש": ["חנה סנש", "סנש"],
    "קרל נטר": ["קרל נטר", "נטר"],
    "ניל\"י": ["ניל\"י", "נילי", "ניל״י"],
    "נורדאו": ["נורדאו"],
    "צבי שפירא": ["צבי שפירא", "שפירא"],
    "י\"ל פרץ": ["י\"ל פרץ", "יל פרץ", "י״ל פרץ", "פרץ"],
    "יוסף סרלין": ["יוסף סרלין", "סרלין"],
    "יהושע חנקין": ["יהושע חנקין", "חנקין"],
    "מקוה ישראל": ["מקוה ישראל", "מקווה ישראל"],
    "פרוג": ["פרוג"],
    "קלישר": ["קלישר"],
    "שלום עליכם": ["שלום עליכם"],
    "הנוטרים": ["הנוטרים"],
    "הצנחנים": ["הצנחנים"],
    "הגדוד העברי": ["הגדוד העברי"],
    "הפלמ\"ח": ["הפלמ\"ח", "הפלמח", "הפלמ״ח"],
    "אגרובנק": ["אגרובנק", "אגרו בנק"],
}

@dataclass
class SearchRule:
    city: str
    urls: list[str]
    cfg: dict[str, Any]

@dataclass
class Listing:
    source: str
    city_rule: str
    title: str
    text: str
    url: str
    published_at: datetime | None

    @property
    def stable_id(self) -> str:
        base = self.url.strip() or f"{self.source}|{self.city_rule}|{self.title}|{self.text[:300]}"
        return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()

def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default

def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

def parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = date_parser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def first_text(item: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""

def _parse_brightdata_rows(response: requests.Response) -> Any:
    """Parse Bright Data response as normal JSON or JSONL/NDJSON."""
    text = response.text.strip()
    if not text:
        return []

    try:
        return response.json()
    except ValueError:
        rows: list[Any] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Could not parse Bright Data response at line {line_no}: {exc}; "
                    f"sample={line[:300]!r}"
                ) from exc
        return rows


def _snapshot_id_from_payload(data: Any) -> str | None:
    if isinstance(data, dict):
        for key in ("snapshot_id", "snapshot", "id"):
            value = data.get(key)
            if isinstance(value, str) and (value.startswith("s_") or value.startswith("sd_")):
                return value

        # Sometimes the useful payload is nested.
        for key in ("data", "result", "results"):
            nested = data.get(key)
            found = _snapshot_id_from_payload(nested)
            if found:
                return found

        # Last resort: extract an s_* token from the message/error text.
        combined = " ".join(str(data.get(k, "")) for k in ("error", "message", "details"))
        match = re.search(r"\b((?:s|sd)_[A-Za-z0-9_-]+)\b", combined)
        if match:
            return match.group(1)

    if isinstance(data, list):
        for item in data:
            found = _snapshot_id_from_payload(item)
            if found:
                return found
    return None


def _wait_for_snapshot(api_key: str, snapshot_id: str, timeout_seconds: int = 600) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {api_key}"}
    progress_url = f"https://api.brightdata.com/datasets/v3/progress/{snapshot_id}"
    data_url = f"https://api.brightdata.com/datasets/v3/log/{snapshot_id}"
    deadline = time.monotonic() + timeout_seconds

    print(f"[BrightData] snapshot={snapshot_id} waiting for completion")

    while time.monotonic() < deadline:
        progress = requests.get(progress_url, headers=headers, timeout=60)
        if not progress.ok:
            raise RuntimeError(
                f"Bright Data progress HTTP {progress.status_code}: {progress.text[:1000]}"
            )

        progress_data = _parse_brightdata_rows(progress)
        if not isinstance(progress_data, dict):
            raise RuntimeError(f"Unexpected Bright Data progress response: {progress_data!r}")

        status = str(progress_data.get("status", "")).lower()
        print(f"[BrightData] snapshot={snapshot_id} status={status or 'unknown'}")

        if status == "ready":
            result = requests.get(data_url, headers=headers, timeout=120)
            if not result.ok:
                raise RuntimeError(
                    f"Bright Data snapshot HTTP {result.status_code}: {result.text[:1000]}"
                )
            parsed = _parse_brightdata_rows(result)
            if isinstance(parsed, dict):
                error = parsed.get("error") or parsed.get("message")
                if error:
                    raise RuntimeError(str(error))
                parsed = parsed.get("results", parsed.get("data", [parsed]))
            if not isinstance(parsed, list):
                raise RuntimeError(f"Unexpected Bright Data snapshot response: {type(parsed).__name__}")
            return [row for row in parsed if isinstance(row, dict) and not row.get("error")]

        if status == "failed":
            raise RuntimeError(f"Bright Data snapshot failed: {progress_data}")

        time.sleep(10)

    raise RuntimeError(f"Bright Data snapshot {snapshot_id} timed out after {timeout_seconds}s")


def run_brightdata_group(group_url: str, start_date: str, end_date: str, num_of_posts: int) -> list[dict[str, Any]]:
    api_key = env_required("BRIGHTDATA_API_KEY")

    # Match Bright Data's generated Python example for "Facebook - Posts by group URL".
    payload = {
        "input": [{
            "url": group_url,
            "user_to_not_include": "",
            "start_date": start_date,
            "end_date": end_date,
        }],
        "limit_per_input": num_of_posts,
    }

    response = requests.post(
        BRIGHTDATA_SCRAPE_URL,
        params={
            "dataset_id": BRIGHTDATA_DATASET_ID,
            "notify": "false",
            "include_errors": "true",
        },
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload),
        timeout=300,
    )

    if not response.ok:
        try:
            error_data = _parse_brightdata_rows(response)
        except Exception:
            error_data = response.text
        print(
            f"[BrightData][HTTP {response.status_code}] url={group_url} error={error_data}",
            file=sys.stderr,
        )
        raise RuntimeError(f"Bright Data HTTP {response.status_code}: {error_data}")

    data = _parse_brightdata_rows(response)

    # A synchronous request may be converted to a background snapshot if it runs too long.
    snapshot_id = _snapshot_id_from_payload(data)
    if snapshot_id:
        return _wait_for_snapshot(api_key, snapshot_id)

    if isinstance(data, dict):
        error = data.get("error") or data.get("message")
        if error:
            # If Bright Data says the request is still running but omitted snapshot_id from the
            # structured fields, surface the full payload so the log makes the problem obvious.
            print(f"[BrightData][API error] url={group_url} error={data}", file=sys.stderr)
            raise RuntimeError(str(error))
        data = data.get("results", data.get("data", [data]))

    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Bright Data response: {type(data).__name__}")

    errors = [row for row in data if isinstance(row, dict) and row.get("error")]
    if errors and len(errors) == len(data):
        err = errors[0].get("error")
        print(f"[BrightData][row error] url={group_url} error={err}", file=sys.stderr)
        raise RuntimeError(str(err))

    return [row for row in data if isinstance(row, dict) and not row.get("error")]

def normalize_items(items: list[dict[str, Any]], rule: SearchRule) -> list[Listing]:
    out: list[Listing] = []
    for item in items:
        title = first_text(item, ["title", "name", "headline", "group_name"])
        text = first_text(item, ["content", "text", "description", "postText", "body", "ocrText"])
        url = first_text(item, ["url", "facebookUrl", "postUrl", "permalinkUrl", "link"])
        published = None
        for key in ["date_posted", "time", "publishedAt", "date", "timestamp", "createdAt"]:
            if item.get(key):
                published = parse_date(item.get(key))
                if published:
                    break
        if title or text or url:
            out.append(Listing("Facebook", rule.city, title, text, url, published))
    return out

def normalize_text(text: str) -> str:
    return text.lower().replace("״", '"').replace("׳", "'").replace("\u200f", "").replace("\u200e", "")

def contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(term.lower() in text for term in terms)

def extract_number(patterns: list[str], text: str) -> float | None:
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                pass
    return None

def extract_rooms(text: str) -> float | None:
    return extract_number([
        r"(\d+(?:[.,]\d+)?)\s*(?:חדרים|חדר|חד['\"]?|rooms?)",
        r"(?:דירת|דירה)\s*(\d+(?:[.,]\d+)?)",
    ], normalize_text(text))

def extract_price(text: str) -> int | None:
    t = normalize_text(text)
    pats = [
        r"(?:₪|שח|ש\"ח|מחיר[:\s]*)\s*([0-9]{1,2}(?:[.,][0-9]{3})|[0-9]{4,5})",
        r"([0-9]{1,2}(?:[.,][0-9]{3})|[0-9]{4,5})\s*(?:₪|שח|ש\"ח)",
        r"(\d+(?:[.,]\d+)?)\s*[kK]\b",
    ]
    for i, p in enumerate(pats):
        m = re.search(p, t)
        if not m:
            continue
        raw = m.group(1)
        try:
            if i == 2:
                return int(float(raw.replace(",", ".")) * 1000)
            return int(raw.replace(",", "").replace(".", ""))
        except ValueError:
            pass
    return None

def extract_street(text: str) -> tuple[str | None, bool]:
    t = normalize_text(text)
    for canonical, variants in AGROBANK_STREETS.items():
        for v in variants:
            if normalize_text(v) in t:
                return canonical, True
    return None, False

def evaluate_listing(listing: Listing, cfg: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    raw = f"{listing.title}\n{listing.text}"
    text = normalize_text(raw)

    rooms = extract_rooms(text)
    rmin = float(cfg.get("rooms_min", 3))
    rmax = float(cfg.get("rooms_max", 4.5))
    if rooms is None or not (rmin <= rooms <= rmax):
        return False, {"reason": f"חדרים לא בטווח {rmin}-{rmax}"}

    price = extract_price(text)
    pmax = int(cfg.get("price_max", 7300))
    if price is None and not bool(cfg.get("allow_missing_price", True)):
        return False, {"reason": "לא נמצא מחיר"}
    if price is not None and price > pmax:
        return False, {"reason": f"מחיר מעל {pmax}"}

    if contains_any(text, ["ללא חניה", "אין חניה", "חניה ברחוב", "חנייה ברחוב", "street parking", "nearby parking"]):
        return False, {"reason": "אין חניה פרטית"}

    has_parking = contains_any(text, [
        "חניה פרטית", "חנייה פרטית", "חניה בטאבו", "חנייה בטאבו",
        "חניה צמודה", "חנייה צמודה", "חניה לדירה", "חנייה לדירה",
        "חניה", "חנייה", "חניות", "חניות פרטיות", "2 חניות", "שתי חניות",
        "private parking", "assigned parking"
    ])
    if cfg.get("parking_required", True) and not has_parking:
        return False, {"reason": "חניה לא זוהתה"}

    if contains_any(text, ["אין מעלית", "ללא מעלית", "בלי מעלית", "no elevator", "no lift"]):
        return False, {"reason": "אין מעלית"}

    has_elevator = contains_any(text, ["מעלית", "elevator", "lift"])
    if cfg.get("elevator_required", True) and not has_elevator:
        return False, {"reason": "מעלית לא זוהתה"}

    street, street_ok = extract_street(raw)
    if cfg.get("require_agrobank", False) and not street_ok:
        return False, {"reason": "לא זוהה רחוב מאגרובנק"}

    has_balcony = contains_any(text, ["מרפסת", "balcony", "בלקון"])
    has_mamad = contains_any(text, ['ממ"ד', "ממד", "ממ״ד", "חדר ממד", "חדר ממ\"ד", "safe room", "protected room"])
    has_mamak = contains_any(text, ['ממ"ק', "ממק", "ממ״ק", "מרחב מוגן קומתי", "ממ\"ק קומתי", "ממק קומתי"])

    score = (5 if has_balcony else 0) + (15 if (has_mamad or has_mamak) else 0)
    return True, {
        "rooms": rooms, "price": price, "street": street or "לא זוהה",
        "parking": has_parking, "elevator": has_elevator,
        "balcony": has_balcony, "mamad": has_mamad, "mamak": has_mamak,
        "score": score,
    }

def yes_no(v: bool) -> str:
    return "כן ✅" if v else "לא / לא צוין ❌"

def build_email(matches: list[tuple[Listing, dict[str, Any]]], issues: list[str]) -> tuple[str, str]:
    subject = f"{len(matches)} מודעות שכירות חדשות שמתאימות"
    parts = ["נמצאו מודעות חדשות:\n"]
    for i, (listing, info) in enumerate(matches, 1):
        parts.append(
            f"{i}. {listing.title or 'מודעה ללא כותרת'}\n"
            f"עיר: {listing.city_rule}\n"
            f"חדרים: {info.get('rooms')}\n"
            f"מחיר: {info.get('price') if info.get('price') is not None else 'לא צוין'}\n"
            f"רחוב: {info.get('street')}\n"
            f"חניה: {yes_no(info.get('parking', False))}\n"
            f"מעלית: {yes_no(info.get('elevator', False))}\n"
            f"מרפסת: {yes_no(info.get('balcony', False))}\n"
            f"ממ״ד: {yes_no(info.get('mamad', False))}\n"
            f"ממ״ק קומתי: {yes_no(info.get('mamak', False))}\n"
            f"ציון עדיפות: {info.get('score', 0)}\n"
            f"קישור: {listing.url or 'לא נמצא קישור'}\n"
            f"טקסט: {listing.text[:900]}\n"
            f"{'-' * 60}\n"
        )
    if issues:
        parts.append("\nבעיות גישה למקורות:\n" + "\n".join(f"- {x}" for x in issues))
    return subject, "\n".join(parts)

def send_email(subject: str, body: str, recipient: str) -> None:
    host = env_required("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = env_required("SMTP_USERNAME")
    password = env_required("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM", username)

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(msg)

def build_rules(cfg: dict[str, Any]) -> list[SearchRule]:
    rules: list[SearchRule] = []
    for city, city_cfg in cfg.get("cities", {}).items():
        urls = [str(u).strip() for u in city_cfg.get("facebook_group_urls", []) if str(u).strip()]
        if urls:
            rules.append(SearchRule(city=city, urls=urls, cfg=city_cfg))
    return rules

def main() -> int:
    cfg = load_json(CONFIG_PATH, {})

    # One-time initial backfill: on the first run with this version, ignore old dedupe
    # entries so listings from the previous 4 hours can be evaluated and emailed.
    # After that run, seen.json is saved with initial_backfill_done=true, so future
    # runs deduplicate normally and do NOT reset again.
    seen_payload = load_json(SEEN_PATH, [])
    if isinstance(seen_payload, dict):
        initial_backfill_done = bool(seen_payload.get("initial_backfill_done", False))
        seen = set(seen_payload.get("ids", []))
    else:
        initial_backfill_done = False
        seen = set(seen_payload if isinstance(seen_payload, list) else [])

    if not initial_backfill_done:
        print("[Monitor] initial 4-hour backfill: resetting deduplication once")
        seen = set()

    issues: list[str] = []
    raw: list[tuple[Listing, dict[str, Any]]] = []

    now = datetime.now(timezone.utc)
    lookback = int(cfg.get("lookback_hours", 4))
    start_date = (now - timedelta(hours=lookback)).isoformat().replace("+00:00", "Z")
    end_date = now.isoformat().replace("+00:00", "Z")
    max_posts = int(cfg.get("max_posts_per_group", 10))

    rules = build_rules(cfg)
    print(f"[Monitor] rules={len(rules)} lookback_hours={lookback} max_posts={max_posts}")

    for rule in rules:
        for url in rule.urls:
            print(f"[Monitor] fetching city={rule.city} url={url}")
            try:
                items = run_brightdata_group(url, start_date, end_date, max_posts)
                print(f"[Monitor] received city={rule.city} url={url} items={len(items)}")
                for listing in normalize_items(items, rule):
                    raw.append((listing, rule.cfg))
            except Exception as exc:
                issue = f"Facebook {rule.city}: {exc}"
                print(f"[Monitor][issue] {issue}", file=sys.stderr)
                issues.append(issue)

    matches: list[tuple[Listing, dict[str, Any]]] = []
    new_seen = set(seen)

    for listing, city_cfg in raw:
        if listing.stable_id in seen:
            continue
        ok, info = evaluate_listing(listing, city_cfg)
        if ok:
            print(f"[Filter][MATCH] city={listing.city_rule} url={listing.url}")
            matches.append((listing, info))
        else:
            print(
                f"[Filter][REJECTED] city={listing.city_rule} "
                f"reason={info.get('reason', 'unknown')} url={listing.url}"
            )
        new_seen.add(listing.stable_id)

    save_json(SEEN_PATH, {
        "initial_backfill_done": True,
        "ids": sorted(new_seen),
    })

    if matches or issues:
        subject, body = build_email(matches, issues)
        if not matches:
            subject = "דוח ניטור: בעיות גישה לפייסבוק"
        send_email(subject, body, cfg["recipient_email"])
        print(f"Email sent. Matches={len(matches)}, issues={len(issues)}")
    else:
        print("No new matching listings and no access issues.")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        raise
