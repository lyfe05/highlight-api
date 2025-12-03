#!/usr/bin/env python3
"""
Auto-updated scraper for hoofoot.com
– pulls match list
– extracts m3u8 from embed pages
– decorates with team logos
– dumps api/matches.json
"""

import pycurl
from io import BytesIO
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re
import time
import json
import requests
import os
from datetime import datetime

# ------------------------------------------------------------------
# CONFIG – only places you ever need to change URLs
# ------------------------------------------------------------------
BASE = "https://hoofoot.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
LOGOS_URL = ("https://raw.githubusercontent.com/lyfe05/foot_logo/"
             "refs/heads/main/logos.txt")


# ------------------------------------------------------------------
# LOW-LEVEL FETCH
# ------------------------------------------------------------------
def fetch(url: str, timeout: int = 30) -> str:
    buf = BytesIO()
    c = pycurl.Curl()
    c.setopt(c.URL, url)
    c.setopt(c.WRITEFUNCTION, buf.write)
    c.setopt(c.FOLLOWLOCATION, True)
    c.setopt(c.USERAGENT, UA)
    c.setopt(c.ACCEPT_ENCODING, "gzip, deflate")
    c.setopt(c.CONNECTTIMEOUT, 10)
    c.setopt(c.TIMEOUT, timeout)
    c.setopt(c.SSL_VERIFYPEER, False)
    c.setopt(c.SSL_VERIFYHOST, False)

    headers = [
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language: en-US,en;q=0.9",
        "Cache-Control: no-cache",
        "Connection: keep-alive",
        "Pragma: no-cache",
        "Sec-Fetch-Dest: document",
        "Sec-Fetch-Mode: navigate",
        "Sec-Fetch-Site: none",
        "Upgrade-Insecure-Requests: 1",
    ]
    c.setopt(c.HTTPHEADER, headers)

    try:
        c.perform()
        if c.getinfo(pycurl.RESPONSE_CODE) != 200:
            return ""
    except Exception:
        return ""
    finally:
        c.close()
    return buf.getvalue().decode("utf-8", errors="ignore")

# ------------------------------------------------------------------
# URL HELPERS
# ------------------------------------------------------------------
def normalize_url(src: str) -> str | None:
    if not src:
        return None
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return urljoin(BASE, src)
    if not src.startswith("http"):
        return urljoin(BASE, src)
    return src

# ------------------------------------------------------------------
# HOMEPAGE MATCHES
# ------------------------------------------------------------------
def find_matches_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    matches = []

    for container in soup.find_all("div", id=lambda x: x and x.startswith("port")):
        try:
            title_el = container.find("h2")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)

            link_el = container.find("a", href=True)
            if not link_el or "match=" not in link_el["href"]:
                continue
            url = normalize_url(link_el["href"])

            img_el = container.find("img", src=True)
            image_url = normalize_url(img_el["src"]) if img_el else None

            info = container.find("div", class_="info")
            date, league = None, None
            if info:
                date_span = info.find("span")
                if date_span:
                    date = date_span.get_text(strip=True)
                league_img = info.find("img", src=True)
                if league_img and "/x/" in league_img["src"]:
                    league = (league_img["src"].split("/x/")[-1]
                              .replace(".jpg", "").replace("_", " "))

            matches.append({
                "title": title,
                "url": url,
                "image": image_url,
                "date": date,
                "league": league,
            })
        except Exception:
            continue
    return matches

# ------------------------------------------------------------------
# EMBED + M3U8 EXTRACTION  (CDN REWRITE HAPPENS HERE)
# ------------------------------------------------------------------
def extract_embed_url(match_html: str) -> str | None:
    soup = BeautifulSoup(match_html, "html.parser")
    player = soup.find("div", id="player")
    if player:
        a = player.find("a", href=True)
        if a:
            return urljoin(BASE, a["href"])
    for a in soup.find_all("a", href=True):
        if "embed" in a["href"] or "spotlightmoment" in a["href"]:
            return urljoin(BASE, a["href"])
    return None

def extract_m3u8_from_embed(embed_html: str) -> str | None:
    # grab the first absolute URL inside src:{hls:'<here>'}
    m = re.search(r"src\s*:\s*{\s*hls\s*:\s*'(https://[^']+)'", embed_html)
    if m:
        return m.group(1)          # already absolute, no fix-up needed
    return None
      

# ------------------------------------------------------------------
# PROCESS ONE MATCH
# ------------------------------------------------------------------
def process_match(match: dict) -> dict:
    try:
        m_html = fetch(match["url"])
        embed = extract_embed_url(m_html)
        if not embed:
            return {**match, "embed": None, "m3u8": None}
        embed_html = fetch(embed)
        m3u8 = extract_m3u8_from_embed(embed_html)
        return {**match, "embed": embed, "m3u8": m3u8}
    except Exception:
        return {**match, "embed": None, "m3u8": None}

# ------------------------------------------------------------------
# TEAM LOGOS HELPERS  (country-aware)
# ------------------------------------------------------------------
def fetch_and_parse_logos() -> dict[str, dict]:
    """
    Returns dict  filename -> {url, country, name_clean}
    country & name_clean are lower-cased for easy matching.
    """
    print("📸 Fetching team logos...")
    try:
        txt = requests.get(LOGOS_URL, timeout=30).text
    except Exception as e:
        print(f"❌ Logo fetch failed: {e}")
        return {}

    logos: dict[str, dict] = {}
    for chunk in txt.split("------------------------------"):
        # Filename
        fname_m = re.search(r"Filename: (.+?)\.png", chunk)
        # URL
        url_m   = re.search(r"URL: (https?://[^\s]+)", chunk)
        # Description  (everything after "Description: " up to next blank line or dashes)
        desc_m  = re.search(r"Description: ([^\r\n]+)", chunk, re.I)

        if not (fname_m and url_m and desc_m):
            continue

        fname = fname_m.group(1).strip()
        url   = url_m.group(1).strip()
        desc  = desc_m.group(1).strip()

        # ---- extract country inside parentheses ----
        country_m = re.search(r"\(([^)]+)\)", desc)
        country   = country_m.group(1).lower() if country_m else ""

        # ---- clean team name (remove parenthetical country) ----
        name_clean = re.sub(r"\s*\([^)]*\)", "", desc).strip().lower()

        logos[fname] = {"url": url, "country": country, "name_clean": name_clean}

    print(f"✅ Loaded {len(logos)} logos")
    return logos


def league_to_country(league: str) -> str:
    """Tiny mapper – extend as you add leagues."""
    league = league.lower()
    if "premier league" in league:
        return "england"
    if "la liga" in league:
        return "spain"
    if "serie a" in league:
        return "italy"
    if "bundesliga" in league or "dfb-pokal" in league:
        return "germany"
    if "super lig" in league:
        return "turkey"
    return ""


def find_logo_url(team_name: str, league: str, logos: dict[str, dict]) -> str:
    """
    team_name : e.g. "Manchester City"
    league    : e.g. "Premier League"
    logos     : dict from fetch_and_parse_logos()
    """
    team   = team_name.lower().strip()
    country = league_to_country(league)

    # 1. remove league-country from team string  (Barcelona (Spain) -> barcelona)
    team_no_country = re.sub(rf"\b{re.escape(country)}\b", "", team).strip()

    # 2. exact filename match  (fulham, manchester-city, barcelona)
    exact = team_no_country.replace(" ", "-")
    if exact in logos:
        return logos[exact]["url"]

    # 3. word-wise match on cleaned description
    team_words = set(team_no_country.split())
    for data in logos.values():
        if team_words.issubset(set(data["name_clean"].split())):
            return data["url"]
    return ""

# ------------------------------------------------------------------
# BUILD FINAL JSON
# ------------------------------------------------------------------
def process_matches_to_json(matches_data: list[dict], logos: dict[str, str]):
    print("🔄 Matching logos & grouping streams...")
    groups: dict[str, dict] = {}
    referer = "|Referer=https://hoofootay4.spotlightmoment.com/"

    for m in matches_data:
        if not m.get("m3u8"):
            continue
        title = m["title"]
        m3u8 = m["m3u8"] + referer
        if title not in groups:
            groups[title] = {
                "image": m.get("image") or "",
                "date": m.get("date") or "",
                "league": m.get("league") or "",
                "streams": [],
            }
        groups[title]["streams"].append(m3u8)

    result = []
    for title, data in groups.items():
        if " v " not in title:
            continue
        home, away = (x.strip() for x in title.split(" v ", 1))
        result.append({
            "home": {"name": home, "logo_url": find_logo_url(home, logos)},
            "away": {"name": away, "logo_url": find_logo_url(away, logos)},
            "stream_urls": data["streams"],
            "date": data["date"],
            "league": data["league"],
        })
    return result

# ------------------------------------------------------------------
# MAIN PIPELINE
# ------------------------------------------------------------------
def main():
    print("🚀 Starting hoofoot scraper…")
    t0 = time.time()

    html = fetch(BASE)
    if not html:
        print("❌ Could not reach homepage")
        return

    matches = find_matches_from_html(html)
    if not matches:
        print("❌ No matches found")
        return
    print(f"✅ Found {len(matches)} matches")

    print("🎬 Extracting streams …")
    data = [process_match(m) for m in matches]

    logos = fetch_and_parse_logos()
    final = process_matches_to_json(data, logos)

    os.makedirs("api", exist_ok=True)
    with open("api/matches.json", "w", encoding="utf-8") as f:
        json.dump({
            "last_updated": datetime.now().isoformat(),
            "matches_count": len(final),
            "data": final,
        }, f, indent=2, ensure_ascii=False)

    print(f"✅ Done – {len(final)} matches saved in {time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()
