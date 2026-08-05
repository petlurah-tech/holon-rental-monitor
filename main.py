from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import sys
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


@dataclass
class Listing:
    source: str
    title: str
    text: str
    url: str
    published_at: datetime | None

    @property
    def stable_id(self) -> str:
        base = self.url.strip() or f"{self.source}|{self.title}|{self.text[:300]}"
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


def run_apify_actor(actor_id: str, actor_input: dict[str, Any]) -> list[dict[str, Any]]:
    token = env_required("APIFY_TOKEN")
    actor_id = actor_id.strip()
    if not actor_id:
        return []

    url = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"
    response = requests.post(
        url,
        params={"token": token, "clean": "true", "format": "json"},
        json=actor_input,
        timeout=300,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected Apify response from {actor_id}")
    return payload


def first_text(item: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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


def normalize_items(items: list[dict[str, Any]], source: str) -> list[Listing]:
    listings: list[Listing] = []
    for item in items:
        title = first_text(item, ["title", "name", "headline", "groupTitle"])
        text = first_text(item, ["text", "description", "postText", "content", "body", "ocrText"])
        url = first_text(item, ["url", "facebookUrl", "postUrl", "permalinkUrl", "link", "listingUrl"])
        published = None
        for key in ["time", "publishedAt", "date", "timestamp", "createdAt", "publicationDate"]:
            if item.get(key):
                published = parse_date(item.get(key))
                if published:
                    break
        if title or text or url:
            listings.append(Listing(source, title, text, url, published))
    return listings


def extract_number(patterns: list[str], text: str) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return float(match.group(1).replace(",", "."))
            except ValueError:
                continue
    return None


def matches(listing: Listing, cfg: dict[str, Any]) -> tuple[bool, list[str]]:
    text = f"{listing.title}\n{listing.text}".lower()
    reasons: list[str] = []

    if "חולון" not in text and "holon" not in text:
        return False, ["לא זוהתה חולון"]

    rooms = extract_number([
        r"(\d+(?:[\.,]\d+)?)\s*(?:חדרים|חד['׳\"]?|rooms?)",
        r"(?:דירת|דירה)\s*(\d+(?:[\.,]\d+)?)",
    ], text)
    if rooms is None or not (float(cfg["rooms_min"]) <= rooms <= float(cfg["rooms_max"])):
        return False, ["מספר חדרים לא מתאים או לא צוין"]

    price = extract_number([
        r"(?:₪|ש[\"״']?ח|מחיר[:\s]*)\s*([0-9]{4,5})",
        r"([0-9]{4,5})\s*(?:₪|ש[\"״']?ח)",
    ], text)
    if price is None:
        if not cfg.get("allow_missing_price", False):
            return False, ["מחיר חסר"]
        reasons.append("מחיר לא צוין")
    elif not (float(cfg["price_min"]) <= price <= float(cfg["price_max"])):
        return False, ["מחיר מחוץ לטווח"]

    no_parking_terms = [
        "ללא חניה", "אין חניה", "חניה ברחוב", "חניה באיזור", "חניה באזור",
        "חניה מסביב", "parking on street", "street parking", "nearby parking"
    ]
    if any(term in text for term in no_parking_terms):
        return False, ["אין חניה פרטית לדירה"]

    private_parking_terms = [
        "חניה פרטית", "חניה בטאבו", "חניה צמודה", "חנייה פרטית", "חנייה בטאבו",
        "חנייה צמודה", "חניה לדירה", "חנייה לדירה", "private parking", "assigned parking"
    ]
    if cfg.get("parking_required", True) and not any(term in text for term in private_parking_terms):
        return False, ["לא זוהתה חניה פרטית"]

    no_mamad_terms = ["אין ממד", "אין ממ\"ד", "ללא ממד", "ללא ממ\"ד", "no mamad", "no safe room"]
    if any(term in text for term in no_mamad_terms):
        return False, ["נכתב שאין ממ״ד"]

    if "מרפסת" in text or "balcony" in text:
        reasons.append("יש מרפסת")
    else:
        reasons.append("מרפסת לא צוינה")

    if "אגרובנק" in text or "agrobank" in text:
        reasons.append("אגרובנק")
    else:
        reasons.append("מחוץ/לא צוין אגרובנק")

    if listing.published_at:
        min_move_in = listing.published_at + timedelta(days=int(cfg["move_in_min_days_after_publication"]))
        date_matches = re.findall(r"(?:כניסה|פינוי|move[- ]?in)[^\d]{0,12}(\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)", text)
        if date_matches:
            parsed = parse_date(date_matches[0])
            if parsed and parsed < min_move_in:
                return False, ["תאריך הכניסה מוקדם מדי"]
        else:
            reasons.append("תאריך כניסה לא צוין")

    return True, reasons


def build_email(listings: list[tuple[Listing, list[str]]], access_issues: list[str]) -> tuple[str, str]:
    subject = f"{len(listings)} מודעות שכירות חדשות שמתאימות"
    parts = ["נמצאו מודעות חדשות:\n"]
    for idx, (listing, reasons) in enumerate(listings, 1):
        parts.append(
            f"{idx}. {listing.title or 'מודעה ללא כותרת'}\n"
            f"מקור: {listing.source}\n"
            f"סיבות: {', '.join(reasons)}\n"
            f"קישור: {listing.url or 'לא נמצא קישור'}\n"
            f"טקסט: {listing.text[:700]}\n"
            f"{'-' * 50}\n"
        )
    if access_issues:
        parts.append("\nבעיות גישה למקורות:\n" + "\n".join(f"- {x}" for x in access_issues))
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


def main() -> int:
    cfg = load_json(CONFIG_PATH, {})
    seen = set(load_json(SEEN_PATH, []))
    access_issues: list[str] = []
    raw_listings: list[Listing] = []

    group_urls = [
        str(url).strip()
        for url in cfg.get("facebook_group_urls", [])
        if str(url).strip()
    ]
    if not group_urls:
        raise RuntimeError("config.json does not contain facebook_group_urls")

    # Official Apify Store actor. In the API URL, owner/name is written with ~.
    fb_actor = "apify~facebook-groups-scraper"

    try:
        items = run_apify_actor(
            fb_actor,
            {
                "startUrls": [{"url": url} for url in group_urls],
                "resultsLimit": 5,
                "viewOption": "CHRONOLOGICAL",
                "onlyPostsNewerThan": "4 hours",
            },
        )
        raw_listings.extend(normalize_items(items, "Facebook"))
        print(f"Apify returned {len(items)} rows; normalized {len(raw_listings)} posts.")
    except Exception as exc:
        access_issues.append(f"Facebook: {exc}")

    fresh_matches: list[tuple[Listing, list[str]]] = []
    new_seen = set(seen)

    for listing in raw_listings:
        if listing.stable_id in seen:
            continue

        ok, reasons = matches(listing, cfg)
        new_seen.add(listing.stable_id)

        if ok:
            fresh_matches.append((listing, reasons))

    save_json(SEEN_PATH, sorted(new_seen))

    if fresh_matches or access_issues:
        subject, body = build_email(fresh_matches, access_issues)
        if not fresh_matches:
            subject = "דוח ניטור: בעיות גישה לפייסבוק"
        send_email(subject, body, cfg["recipient_email"])
        print(f"Email sent. Matches={len(fresh_matches)}, issues={len(access_issues)}")
    else:
        print("No new matching Facebook listings and no access issues.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        raise
