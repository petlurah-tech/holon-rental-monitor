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

BRIGHTDATA_DATASET_ID = "gd_lz11l67o2cb3r0lkj3"
BRIGHTDATA_SCRAPE_URL = "https://api.brightdata.com/datasets/v3/scrape"
RAMAT_GAN_GROUP_URL = "https://www.facebook.com/share/g/191xvJWiwm/?mibextid=wwXIfr"

# Shenkar and Sokolov are intentionally excluded.
AGROBANK_STREETS: dict[str, list[str]] = {
    "אז״ר": ["אז״ר", 'אז"ר', "אזר"],
    "אליהו קראוזה": ["אליהו קראוזה", "קראוזה"],
    "אליעזר בן יהודה": ["אליעזר בן יהודה", "בן יהודה"],
    "חנה סנש": ["חנה סנש"],
    "קרל נטר": ["קרל נטר"],
    "ניל״י": ["ניל״י", 'ניל"י', "נילי"],
    "נורדאו": ["נורדאו"],
    "ספרינצק": ["ספרינצק", "שפרינצק"],
    "צבי שפירא": ["צבי שפירא"],
    "י״ל פרץ": ["י״ל פרץ", 'י"ל פרץ', "י.ל. פרץ", "יל פרץ"],
    "יוסף סרלין": ["יוסף סרלין", "סרלין"],
    "יהושע חנקין": ["יהושע חנקין", "חנקין"],
    "מקוה ישראל": ["מקוה ישראל", "מקווה ישראל"],
}


@dataclass(frozen=True)
class SearchRule:
    name: str
    city: str
    urls: list[str]
    require_agrobank: bool


@dataclass
class Listing:
    source: str
    city: str
    title: str
    text: str
    url: str
    published_at: datetime | None

    @property
    def stable_id(self) -> str:
        base = self.url.strip() or f"{self.source}|{self.city}|{self.title}|{self.text[:500]}"
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


def normalize_text(value: str) -> str:
    value = value.lower().replace("׳", "'").replace("״", '"')
    value = re.sub(r"[\-–—,/()\[\]:;]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


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
        dt = date_parser.parse(str(value), dayfirst=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def run_brightdata_group(group_url: str, start_date: str, end_date: str, num_of_posts: int) -> list[dict[str, Any]]:
    """Collect recent public posts from one Facebook group through Bright Data."""
    api_key = env_required("BRIGHTDATA_API_KEY")
    payload = {
        "input": [
            {
                "url": group_url,
                "num_of_posts": num_of_posts,
                "start_date": start_date,
                "end_date": end_date,
            }
        ]
    }
    response = requests.post(
        BRIGHTDATA_SCRAPE_URL,
        params={
            "dataset_id": BRIGHTDATA_DATASET_ID,
            "include_errors": "true",
        },
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=300,
    )
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        # Some API errors are returned as JSON objects even with HTTP 200.
        error = data.get("error") or data.get("message")
        if error:
            raise RuntimeError(str(error))
        data = data.get("results", data.get("data", []))
    if not isinstance(data, list):
        raise RuntimeError("Unexpected Bright Data response")
    errors = [row for row in data if isinstance(row, dict) and row.get("error")]
    if errors and len(errors) == len(data):
        raise RuntimeError(str(errors[0].get("error")))
    return [row for row in data if isinstance(row, dict) and not row.get("error")]


def normalize_items(items: list[dict[str, Any]], rule: SearchRule) -> list[Listing]:
    listings: list[Listing] = []
    for item in items:
        title = first_text(item, ["title", "name", "headline", "group_name"])
        text = first_text(item, ["content", "text", "description", "postText", "body", "ocrText"])
        url = first_text(item, ["url", "facebookUrl", "postUrl", "permalinkUrl", "link"])
        published_at = None
        for key in ["date_posted", "time", "publishedAt", "date", "timestamp", "createdAt", "publicationDate"]:
            if item.get(key):
                published_at = parse_date(item.get(key))
                if published_at:
                    break
        if title or text or url:
            listings.append(
                Listing(
                    source=rule.name,
                    city=rule.city,
                    title=title,
                    text=text,
                    url=url,
                    published_at=published_at,
                )
            )
    return listings


def build_rules(cfg: dict[str, Any]) -> list[SearchRule]:
    configured_groups = cfg.get("groups")
    if isinstance(configured_groups, list) and configured_groups:
        rules: list[SearchRule] = []
        for group in configured_groups:
            if not isinstance(group, dict):
                continue
            url = str(group.get("facebook_url", "")).strip()
            if not url:
                continue
            city = str(group.get("city", "")).strip() or "לא ידוע"
            rules.append(
                SearchRule(
                    name=str(group.get("name", city)).strip() or city,
                    city=city,
                    urls=[url],
                    require_agrobank=bool(group.get("require_agrobank", city == "חולון")),
                )
            )
        if rules:
            return rules

    old_urls = [
        str(url).strip()
        for url in cfg.get("facebook_group_urls", [])
        if str(url).strip()
    ]
    holon_urls = [url for url in old_urls if url != RAMAT_GAN_GROUP_URL]
    rules = []
    if holon_urls:
        rules.append(SearchRule("Facebook חולון", "חולון", holon_urls, True))
    rules.append(SearchRule("Facebook רמת גן", "רמת גן", [RAMAT_GAN_GROUP_URL], False))
    return rules


def extract_rooms(text: str) -> float | None:
    patterns = [
        r"(\d+(?:[.,]\d+)?)\s*(?:חדרים|חד['\"]?|rooms?)",
        r"(?:דירת|דירה)\s*(\d+(?:[.,]\d+)?)",
        r"(\d+)\s*(?:וחצי|ו\s*חצי)\s*(?:חדרים|חד['\"]?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        value = float(match.group(1).replace(",", "."))
        if "וחצי" in match.group(0) or "ו חצי" in match.group(0):
            value += 0.5
        return value
    return None


def extract_price(text: str) -> int | None:
    patterns = [
        r"(?:מחיר|שכ[\"']?ד|שכר דירה)?\s*[:\-]?\s*(\d{1,2}(?:[,.]|\s)\d{3})\s*(?:₪|ש[\"']?ח)?",
        r"(?:₪|ש[\"']?ח)\s*(\d{4,5})",
        r"(\d{4,5})\s*(?:₪|ש[\"']?ח)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(re.sub(r"[^0-9]", "", match.group(1)))
    return None


def extract_street(text: str, city: str) -> tuple[str | None, bool]:
    normalized = normalize_text(text)
    if city == "חולון":
        if "אגרובנק" in normalized or "agrobank" in normalized:
            for canonical, aliases in AGROBANK_STREETS.items():
                if any(normalize_text(alias) in normalized for alias in aliases):
                    return canonical, True
            return "לא צוין", True
        for canonical, aliases in AGROBANK_STREETS.items():
            if any(normalize_text(alias) in normalized for alias in aliases):
                return canonical, True
        return None, False

    street_match = re.search(
        r"(?:רחוב|רח['’]?|ברחוב)\s+([א-ת][א-ת\"' .\-]{1,35}?)(?=\s+\d{1,3}\b|[,\n]|$)",
        text,
        flags=re.IGNORECASE,
    )
    if street_match:
        return street_match.group(1).strip(" .,-"), True
    return "לא זוהה", True


def contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(normalize_text(term) in text for term in terms)


def evaluate_listing(listing: Listing, cfg: dict[str, Any], require_agrobank: bool) -> tuple[bool, dict[str, Any]]:
    raw_text = f"{listing.title}\n{listing.text}"
    text = normalize_text(raw_text)
    rooms_min = float(cfg.get("rooms_min", 3))
    rooms_max = float(cfg.get("rooms_max", 4.5))
    price_max = int(cfg.get("price_max", 7300))
    allow_missing_price = bool(cfg.get("allow_missing_price", True))

    rooms = extract_rooms(text)
    if rooms is None or not rooms_min <= rooms <= rooms_max:
        return False, {"reason": "מספר חדרים לא מתאים או לא צוין"}

    price = extract_price(text)
    if price is None and not allow_missing_price:
        return False, {"reason": "מחיר לא צוין"}
    if price is not None and price > price_max:
        return False, {"reason": "המחיר גבוה מ-7,300 ₪"}

    no_parking = [
        "ללא חניה", "אין חניה", "חניה ברחוב", "חנייה ברחוב", "חניה באזור",
        "חניה באיזור", "חניה מסביב", "street parking", "nearby parking",
    ]
    if contains_any(text, no_parking):
        return False, {"reason": "אין חניה השייכת לדירה"}

    parking_terms = [
        "חניה פרטית", "חנייה פרטית", "חניה בטאבו", "חנייה בטאבו", "חניה צמודה",
        "חנייה צמודה", "חניה לדירה", "חנייה לדירה", "חניה", "חנייה",
        "private parking", "assigned parking",
    ]
    has_parking = contains_any(text, parking_terms)
    if not has_parking:
        return False, {"reason": "חניה לא צוינה"}

    no_elevator = ["ללא מעלית", "אין מעלית", "בלי מעלית", "no elevator", "no lift"]
    if contains_any(text, no_elevator):
        return False, {"reason": "אין מעלית"}
    has_elevator = contains_any(text, ["מעלית", "elevator", "lift"])
    if not has_elevator:
        return False, {"reason": "מעלית לא צוינה"}

    street, location_ok = extract_street(raw_text, listing.city)
    if require_agrobank and not location_ok:
        return False, {"reason": "לא זוהו אגרובנק או רחוב מאושר באגרובנק"}

    has_balcony = contains_any(text, ["מרפסת", "balcony"])
    has_mamad = contains_any(text, ["ממ״ד", 'ממ"ד', "ממד", "safe room"])
    has_mamak = contains_any(
        text,
        ["ממ״ק", 'ממ"ק', "ממק", "ממ ק", "ממ״ק קומתי", "ממק קומתי", "ממ ק קומתי",
         "מרחב מוגן קומתי", "מקלט קומתי", "ממ״ק בכל קומה", "ממק בכל קומה"],
    )

    # Move-in date: reject only when a stated date is earlier than 21 days after publication.
    move_in_status = "לא צוין"
    if listing.published_at:
        date_match = re.search(
            r"(?:כניסה|פינוי|move in)[^0-9]{0,15}(\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)",
            text,
        )
        if date_match:
            move_in = parse_date(date_match.group(1))
            if move_in:
                minimum = listing.published_at + timedelta(days=int(cfg.get("move_in_min_days_after_publication", 21)))
                if move_in < minimum:
                    return False, {"reason": "תאריך הכניסה מוקדם מדי"}
                move_in_status = move_in.strftime("%d/%m/%Y")

    score = 80
    if has_balcony:
        score += 5
    if has_mamad or has_mamak:
        score += 15

    return True, {
        "rooms": rooms,
        "price": price,
        "street": street or "לא זוהה",
        "parking": has_parking,
        "elevator": has_elevator,
        "balcony": has_balcony,
        "mamad": has_mamad,
        "mamak": has_mamak,
        "move_in": move_in_status,
        "score": min(score, 100),
    }


def yes_no(value: bool) -> str:
    return "כן ✅" if value else "לא צוין ❌"


def build_email(matches: list[tuple[Listing, dict[str, Any]]], access_issues: list[str]) -> tuple[str, str]:
    subject = f"{len(matches)} מודעות שכירות חדשות שמתאימות"
    parts = ["נמצאו מודעות חדשות:\n"]
    for index, (listing, details) in enumerate(matches, 1):
        price_text = f"{details['price']:,} ₪" if details["price"] is not None else "לא צוין — יש לברר"
        protected = "ממ״ד" if details["mamad"] else ("ממ״ק/מרחב מוגן קומתי" if details["mamak"] else "לא צוין")
        parts.append(
            f"{index}. התאמה: {details['score']}%\n"
            f"עיר: {listing.city}\n"
            f"רחוב: {details['street']}\n"
            f"חדרים: {details['rooms']:g}\n"
            f"מחיר: {price_text}\n"
            f"חניה: {yes_no(details['parking'])}\n"
            f"מעלית: {yes_no(details['elevator'])}\n"
            f"מרפסת: {yes_no(details['balcony'])}\n"
            f"מיגון: {protected}\n"
            f"כניסה: {details['move_in']}\n"
            f"מקור: {listing.source}\n"
            f"קישור: {listing.url or 'לא נמצא קישור'}\n"
            f"טקסט: {listing.text[:900]}\n"
            f"{'-' * 60}\n"
        )
    if access_issues:
        parts.append("\nבעיות גישה למקורות:\n" + "\n".join(f"- {issue}" for issue in access_issues))
    return subject, "\n".join(parts)


def send_email(subject: str, body: str, recipient: str) -> None:
    host = env_required("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = env_required("SMTP_USERNAME")
    password = env_required("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM", username)

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(message)


def main() -> int:
    cfg = load_json(CONFIG_PATH, {})
    seen = set(load_json(SEEN_PATH, []))
    rules = build_rules(cfg)
    if not rules:
        raise RuntimeError("No Facebook groups configured")

    access_issues: list[str] = []
    raw: list[tuple[Listing, bool]] = []

    lookback_hours = int(cfg.get("lookback_hours", 4))
    max_posts_per_group = int(cfg.get("max_posts_per_group", 10))
    now_utc = datetime.now(timezone.utc)
    start_date = (now_utc - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_date = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    for rule in rules:
        for group_url in rule.urls:
            try:
                items = run_brightdata_group(
                    group_url=group_url,
                    start_date=start_date,
                    end_date=end_date,
                    num_of_posts=max_posts_per_group,
                )
                normalized = normalize_items(items, rule)
                raw.extend((listing, rule.require_agrobank) for listing in normalized)
                print(
                    f"{rule.name}: Bright Data returned {len(items)} rows; "
                    f"normalized {len(normalized)} posts."
                )
            except Exception as exc:
                access_issues.append(f"{rule.name}: {exc}")

    fresh_matches: list[tuple[Listing, dict[str, Any]]] = []
    new_seen = set(seen)
    for listing, require_agrobank in raw:
        if listing.stable_id in seen:
            continue
        accepted, details = evaluate_listing(listing, cfg, require_agrobank)
        new_seen.add(listing.stable_id)
        if accepted:
            fresh_matches.append((listing, details))

    save_json(SEEN_PATH, sorted(new_seen))

    if fresh_matches or access_issues:
        subject, body = build_email(fresh_matches, access_issues)
        if not fresh_matches:
            subject = "דוח ניטור: בעיות גישה לפייסבוק"
        send_email(subject, body, str(cfg.get("recipient_email", "Petlurav@yahoo.com")))
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
