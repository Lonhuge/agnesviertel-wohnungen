#!/usr/bin/env python3
"""Lightweight new-listing watcher for GitHub Actions.

Self-contained (only requests + beautifulsoup4 + lxml). Fetches the *list* pages
of markt.de and ohne-makler (NO photos), keeps the Agnesviertel matches, and
diffs them against seen_ids.json committed in the repo. Prints the new ones and
writes an email body — the workflow mails it. On the very first run (no state
file) it seeds silently so you are not emailed the entire existing market.

Outputs (for the workflow, via $GITHUB_OUTPUT): has_new, count.
Artifacts: new_listings.txt (email body), seen_ids.json (updated state).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

POSTAL_CODES = {"50668", "50670", "50672"}
KEYWORDS = ["agnesviertel", "agnes-viertel", "agnesplatz", "agnesstraße",
            "agnesstrasse", "neusser straße", "neusser strasse", "balthasarstraße",
            "weißenburgstraße", "lupusstraße", "gladbacher straße"]
MARKT_PAGES = 12       # newest-first, so the top pages hold anything new
OHNE_PAGES = 6
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_ids.json")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
})
PLZ_RE = re.compile(r"\b(\d{5})\b")
OHNE_DETAIL = re.compile(r"/immobilie/\d{4,}")


def enclosing_card(anchor, detail_re):
    """Tightest ancestor still holding exactly one listing link."""
    node, best = anchor, anchor
    for _ in range(8):
        parent = node.parent
        if parent is None:
            break
        links = [a for a in parent.find_all("a", href=True)
                 if detail_re.search(a.get("href", ""))]
        if len(links) > 1:
            break
        best, node = parent, parent
    return best


def get(url: str, referer: str) -> str | None:
    try:
        r = SESSION.get(url, timeout=20, headers={"Referer": referer})
        return r.text if r.status_code == 200 else None
    except requests.RequestException:
        return None


def is_agnes(text: str, plz: str | None) -> str | None:
    if plz and plz in POSTAL_CODES:
        return f"PLZ {plz}"
    for m in PLZ_RE.findall(text):
        if m in POSTAL_CODES:
            return f"PLZ {m}"
    low = text.lower()
    for kw in KEYWORDS:
        if kw in low:
            return f"keyword '{kw}'"
    return None


def scrape_markt() -> list[dict]:
    out, seen = [], set()
    base = "https://www.markt.de/koeln/immobilien/kaufen/wohnungen/"
    for page in range(1, MARKT_PAGES + 1):
        html = get(base + (f"?page={page}" if page > 1 else ""), "https://www.markt.de/")
        if not html:
            break
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("li.clsy-c-result-list-item")
        if not cards:
            break
        for card in cards:
            a = card.find("a", href=re.compile(r"/a/[0-9a-f]{6,}/"))
            if not a:
                continue
            sid = re.search(r"/a/([0-9a-f]{6,})/", a["href"]).group(1)
            if sid in seen:
                continue
            seen.add(sid)
            loc = card.select_one(".clsy-c-result-list-item__location")
            title = card.select_one(".clsy-c-result-list-item__title")
            loc_txt = loc.get_text(" ", strip=True) if loc else ""
            plz_m = PLZ_RE.search(loc_txt) or PLZ_RE.search(card.get_text(" ", strip=True))
            url = a["href"] if a["href"].startswith("http") else "https://www.markt.de" + a["href"]
            out.append({
                "source": "markt", "id": sid,
                "title": (title.get_text(" ", strip=True) if title else "Wohnung")[:160],
                "url": url, "plz": plz_m.group(1) if plz_m else "",
                "text": card.get_text(" ", strip=True),
            })
        time.sleep(0.4)
    return out


def scrape_ohne() -> list[dict]:
    out, seen = [], set()
    base = "https://www.ohne-makler.net/immobilien/wohnung-kaufen/nordrhein-westfalen/koln/"
    for page in range(1, OHNE_PAGES + 1):
        html = get(base + (f"?page={page}" if page > 1 else ""), "https://www.ohne-makler.net/")
        if not html:
            break
        soup = BeautifulSoup(html, "lxml")
        found = False
        for a in soup.find_all("a", href=re.compile(r"/immobilie/\d{4,}")):
            sid = re.search(r"/immobilie/(\d{4,})", a["href"]).group(1)
            if sid in seen:
                continue
            seen.add(sid)
            found = True
            card = enclosing_card(a, OHNE_DETAIL)
            text = card.get_text(" ", strip=True)
            plz_m = PLZ_RE.search(text)
            url = a["href"] if a["href"].startswith("http") else "https://www.ohne-makler.net" + a["href"]
            out.append({
                "source": "ohnemakler", "id": sid,
                "title": re.sub(r"^\s*[\d.\s]+€\s*", "", text)[:160].strip() or "Wohnung",
                "url": url, "plz": plz_m.group(1) if plz_m else "", "text": text,
            })
        if not found:
            break
        time.sleep(0.4)
    return out


def main() -> int:
    matches = []
    for lst in (scrape_markt() + scrape_ohne()):
        reason = is_agnes(lst["text"], lst["plz"])
        if reason:
            lst["reason"] = reason
            matches.append(lst)
    by_uid = {f"{m['source']}:{m['id']}": m for m in matches}
    print(f"scanned: {len(matches)} Agnesviertel matches in scan window")

    seeding = not os.path.exists(STATE_FILE)
    seen = set()
    if not seeding:
        try:
            seen = set(json.load(open(STATE_FILE)).get("uids", []))
        except (OSError, ValueError):
            seen = set()

    new = [m for uid, m in by_uid.items() if uid not in seen]

    # persist union so a listing dropping off the scan window is not re-alerted
    all_uids = sorted(seen | set(by_uid))
    json.dump({"uids": all_uids}, open(STATE_FILE, "w"))

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if seeding:
        print(f"SEEDED baseline with {len(by_uid)} listings — no alert on first run.")
        if gh_out:
            with open(gh_out, "a") as fh:
                fh.write("has_new=false\ncount=0\n")
        return 0

    if not new:
        print("no new listings.")
        if gh_out:
            with open(gh_out, "a") as fh:
                fh.write("has_new=false\ncount=0\n")
        return 0

    lines = [f"{len(new)} neue Wohnung(en) im Agnesviertel:\n"]
    for m in sorted(new, key=lambda x: x["plz"]):
        lines.append(f"• [{m['plz']} · {m['source']}] {m['title']}\n  {m['reason']}\n  {m['url']}\n")
    body = "\n".join(lines)
    with open(os.path.join(os.path.dirname(STATE_FILE), "new_listings.txt"), "w") as fh:
        fh.write(body)
    print("\n" + body)
    if gh_out:
        with open(gh_out, "a") as fh:
            fh.write(f"has_new=true\ncount={len(new)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
