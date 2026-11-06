#!/usr/bin/env python3
"""Maryad editorial publisher. Python stdlib only; designed for scheduled GitHub Actions.

Each run samples Ganjoor classical poems, rejects malformed/duplicate/over-long
texts, and embeds accepted poems into the *existing* single-file site.
No user account, API key, AI-generated commentary, or external database required.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "index.html"
SETTINGS = ROOT / "config.json"
CONTENT_RE = re.compile(
    r'(<script id="content-data" type="application/json">)(.*?)(</script>)',
    re.DOTALL,
)
BASE = "https://api.ganjoor.net"
ALLOWED_SOURCE = "ganjoor.net"
POETS = [
    (2, "حافظ", "hafez"),
    (7, "سعدی", "saadi"),
    (5, "مولانا", "moulavi"),
    (3, "خیام", "khayyam"),
    (28, "باباطاهر", "babataher"),
    (22, "صائب تبریزی", "saeb"),
    (21, "فخرالدین عراقی", "eraghi"),
    (26, "ابوسعید ابوالخیر", "abusaeed"),
]


def fetch_json(url: str) -> object:
    """Fetch JSON with conservative retries; do not silently guess a page structure."""
    if not url.startswith((BASE + "/", "https://cdn.jsdelivr.net/gh/ganjoor/")):
        raise ValueError("Unsupported API host")
    req = urllib.request.Request(
        url, headers={"User-Agent": "MaryadEditorialBot/1.0 (low-volume; attribution shown)", "Accept": "application/json"}
    )
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                if response.status != 200:
                    raise RuntimeError(f"Unexpected HTTP code {response.status}")
                data = response.read(550_000)
            return json.loads(data.decode("utf-8-sig"))
        except (OSError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed fetching {url}: {last_error}")


def field(obj: dict, *names: str, default=None):
    for name in names:
        if name in obj and obj[name] is not None:
            return obj[name]
    return default


def clean_text(value: str) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("\u200c", "\u200c").replace("\r", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", value)
    return value.strip()


def extract_lines(poem: dict) -> str:
    plain = field(poem, "plainText", "PlainText", "plaintext", "text", "Text")
    if isinstance(plain, str) and plain.strip():
        return clean_text(plain)
    verses = field(poem, "verses", "Verses", default=[])
    if isinstance(verses, list):
        items = []
        for v in verses:
            if isinstance(v, str):
                items.append(v)
            elif isinstance(v, dict):
                text = field(v, "text", "Text", "verseText", "VerseText")
                if isinstance(text, str):
                    items.append(text)
        return clean_text("\n".join(items))
    return ""


def normalized_text_fingerprint(body: str) -> str:
    # Ignore whitespace, punctuation variants and framing markers when checking repeats.
    cleaned = re.sub(r"[\W_]+", "", clean_text(body), flags=re.UNICODE)
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def normalized_source(path_or_url: str) -> str | None:
    path_or_url = str(path_or_url or "").strip()
    if not path_or_url:
        return None
    if path_or_url.startswith("/") and not path_or_url.startswith("//"):
        path = path_or_url
    else:
        u = urllib.parse.urlsplit(path_or_url)
        if u.scheme != "https" or u.hostname not in {"ganjoor.net", "www.ganjoor.net"}:
            return None
        path = u.path
    if not re.fullmatch(r"/[A-Za-z0-9_/%.-]+", path) or ".." in path:
        return None
    return "https://ganjoor.net" + path.rstrip("/")


def poem_from_api(data: object, poet: tuple[int, str, str], max_chars: int) -> dict | None:
    if not isinstance(data, dict):
        return None
    poem = data
    for key in ("poem", "Poem", "ganjoorPoem", "result", "Result"):
        if isinstance(poem.get(key), dict):
            poem = poem[key]
            break
    text = extract_lines(poem)
    text_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(text) > max_chars or len(text) < 70 or len(text_lines) < 4:
        return None
    if not all(re.search(r"[آ-یءئأإؤۀ]", line) for line in text_lines[:4]):
        return None
    source = normalized_source(field(poem, "fullUrl", "FullUrl", "url", "Url", default=""))
    if not source:
        return None
    parsed = urllib.parse.urlsplit(source)
    if parsed.path.split("/")[1] != poet[2]:
        # Never label a poem with the wrong poet.
        return None
    title = clean_text(field(poem, "title", "Title", default=""))
    first_line = text_lines[0].strip("، ؛.:!؟ ")
    if not title or re.search(r"^(غزل|رباعی|دوبیتی|قطعه|شماره|بخش)\s", title):
        title = first_line
    title = title[:100]
    if len(title) < 4:
        return None
    excerpt = " / ".join(text_lines[:2])[:174]
    # The text is preserved verbatim (no AI reconstruction or invented lines).
    poem_id = hashlib.sha256(source.encode("utf-8")).hexdigest()[:17]
    published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "id": "g-" + poem_id,
        "cat": "poetry",
        "title": title,
        "excerpt": excerpt,
        "body": "::" + text + "::",
        "person": poet[1],
        "source": source,
        "feature": False,
        "publishedAt": published_at,
        "auto": True,
    }


def parse_site(site_html: str) -> tuple[re.Match, list[dict]]:
    m = CONTENT_RE.search(site_html)
    if not m:
        raise ValueError("Cannot find the content-data script in index.html")
    posts = json.loads(m.group(2))
    if not isinstance(posts, list):
        raise ValueError("content-data is not an array")
    return m, posts


def candidates(count: int):
    """Sample diverse poets instead of scraping search results or whole websites."""
    random.shuffle(POETS)
    for i in range(count):
        poet = POETS[i % len(POETS)]
        url = f"{BASE}/api/ganjoor/poem/random?poetId={poet[0]}"
        yield poet, url


def editorial_score(post: dict) -> float:
    """Simple transparent quality proxy, not a claim of genuine aesthetic judgement."""
    text = post["body"]
    lines = [x for x in text.splitlines() if x.strip() and x.strip() != "::"]
    length = len(text)
    score = 10.0
    # Prefer complete, readable selections over very long or single-couplet pages.
    score += max(0, 7 - abs(len(lines) - 14) * 0.3)
    score += max(0, 7 - abs(length - 1100) / 200)
    keywords = ["عشق", "دل", "جان", "جهان", "یار", "مهر", "بهار", "شب", "زندگی", "امید", "ماه", "خرد", "آسمان"]
    score += min(5, sum(0.6 for k in keywords if k in text))
    return score


def publish():
    config = json.loads(SETTINGS.read_text(encoding="utf-8"))
    if not config.get("enabled", True):
        print("Publisher is disabled by config.json")
        return 0
    per_run = max(0, min(5, int(config.get("poems_per_day", 2))))
    max_chars = max(300, min(8500, int(config.get("maximum_poem_characters", 4000))))
    max_requests = max(per_run, min(40, int(config.get("maximum_candidate_requests", 22))))
    mode = config.get("mode", "auto")
    if mode not in ("auto", "review"):
        raise ValueError("mode must be auto or review")
    source = SITE.read_text(encoding="utf-8")
    match, posts = parse_site(source)
    old_sources = {str(p.get("source", "")).lower().rstrip("/") for p in posts}
    old_ids = {p.get("id") for p in posts}
    seen_texts = {normalized_text_fingerprint(str(p.get("body", ""))) for p in posts if p.get("cat") == "poetry"}
    accepted_candidates = []
    if per_run == 0:
        print("Daily article count is zero")
        return 0
    consecutive_connection_errors = 0
    for poet, url in candidates(max_requests):
        try:
            result = fetch_json(url)
            consecutive_connection_errors = 0
            post = poem_from_api(result, poet, max_chars)
            if not post:
                print(f"Skipped: unexpected structure, disallowed attribution, or length ({poet[1]})")
                continue
            normalized = post["source"].lower().rstrip("/")
            if normalized in old_sources or post["id"] in old_ids:
                print(f"Duplicate skipped: {post['source']}")
                continue
            fingerprint = normalized_text_fingerprint(post["body"])
            if fingerprint in seen_texts:
                print(f"Text duplicate skipped: {post['source']}")
                continue
            accepted_candidates.append(post)
            seen_texts.add(fingerprint)
            old_sources.add(normalized)
            old_ids.add(post["id"])
            print(f"Candidate {post['person']}: {post['title']}")
        except Exception as exc:
            consecutive_connection_errors += 1
            print(f"Source fetch failed ({poet[1]}): {exc}")
            if consecutive_connection_errors >= 2:
                print("Ganjoor API seems unavailable; skipping remaining requests safely")
                break
        time.sleep(0.4)  # deliberately low-volume
    if not accepted_candidates:
        print("No validated new poems found today; the site remains unchanged")
        return 0
    ordered = sorted(accepted_candidates, key=editorial_score, reverse=True)
    additions = []
    distinct_poets = set()
    for candidate in ordered:
        if len(additions) >= per_run:
            break
        if candidate["person"] in distinct_poets and len(distinct_poets) < per_run:
            continue
        additions.append(candidate)
        distinct_poets.add(candidate["person"])
    for candidate in ordered:
        if len(additions) >= per_run:
            break
        if candidate["id"] not in {p["id"] for p in additions}:
            additions.append(candidate)
    print(f"Editorial filter chose {len(additions)} of {len(accepted_candidates)} candidates")
    if mode == "review":
        pending_path = ROOT / "pending-review.json"
        pending = []
        if pending_path.exists():
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
        known = {p.get("id") for p in pending}
        pending.extend(p for p in additions if p["id"] not in known)
        pending_path.write_text(json.dumps(pending, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Review mode: added {len(additions)} candidates to pending-review.json (NOT published)")
        return 0
    updated = additions + posts
    payload = json.dumps(updated, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    output = source[:match.start(2)] + payload + source[match.end(2):]
    # Basic on-disk verification before replacement.
    _, reloaded_posts = parse_site(output)
    assert len(reloaded_posts) == len(posts) + len(additions)
    tmp = SITE.with_suffix(".html.tmp")
    tmp.write_text(output, encoding="utf-8")
    os.replace(tmp, SITE)
    print(f"Published {len(additions)} poems. Total article count: {len(updated)}")
    return len(additions)


if __name__ == "__main__":
    try:
        publish()
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"Publisher stopped safely: {e}", file=sys.stderr)
        raise SystemExit(1)
