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
import difflib
import unicodedata

BASE = "https://hoofoot.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
LOGOS_URL = ("https://raw.githubusercontent.com/lyfe05/foot_logo/"
             "refs/heads/main/logos.txt")

# ------------------------------------------------------------------
# 1. KNOWN ALIASES (The Fix for Wolves, Gladbach, etc.)
# ------------------------------------------------------------------
# This dictionary maps the "Match Feed Name" -> "Logo File Name"
KNOWN_ALIASES = {
    # England
    "wolves": "wolverhampton wanderers",
    "spurs": "tottenham hotspur",
    "man utd": "manchester united",
    "man city": "manchester city",
    
    # Germany
    "m'gladbach": "borussia monchengladbach",
    "mgladbach": "borussia monchengladbach",
    "gladbach": "borussia monchengladbach",
    "mainz": "mainz 05",
    "schalke": "schalke 04",
    
    # Rest of Europe
    "qarabag": "qarabag fk",
    "malmo ff": "malmo",
    "ferencvaros": "ferencvarosi",
    "psg": "paris saint-germain",
    "sporting cp": "sporting",
    "ac milan": "milan",
    "inter milan": "inter",
    "inter": "inter",
    "atm": "atletico madrid"
}

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
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language: en-US,en;q=0.9",
        "Cache-Control: no-cache",
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
# STREAM EXTRACTION
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

def extract_m3u8_from_embed(embed_html: str) -> list[str] | None:
    urls = []
    patterns = [
        (r"src\s*:\s*{\s*hls\s*:\s*'(https?://[^']+)'", False),
        (r"src\s*:\s*{\s*hls\s*:\s*'//([^']+)'", True),
        (r"backupSrc\s*:\s*{\s*hls\s*:\s*'(https?://[^']+)'", False),
        (r"backupSrc\s*:\s*{\s*hls\s*:\s*'//([^']+)'", True)
    ]

    for pattern, add_https in patterns:
        match = re.search(pattern, embed_html)
        if match:
            url = match.group(1)
            if add_https:
                url = "https:" + url
            if not url.startswith("https://"):
                url = url.replace("https:", "https://", 1)
            urls.append(url)

    return urls if urls else None

def extract_score(detail_html: str):
    bare = re.sub(r"<[^>]+>", "", detail_html)

    m = re.search(r"document\.querySelector\('#bts'\)\.innerHTML\s*=\s*'(\d+):(\d+)", bare)
    if m:
        return int(m.group(1)), int(m.group(2))

    return None, None

def process_match(match: dict) -> dict:
    try:
        m_html = fetch(match["url"])
        if not m_html:
            return {**match, "embed": None, "m3u8": None, "home_score": None, "away_score": None}

        home_score, away_score = extract_score(m_html)

        embed = extract_embed_url(m_html)
        if not embed:
            return {**match, "embed": None, "m3u8": None, "home_score": home_score, "away_score": away_score}

        embed_html = fetch(embed)
        m3u8_urls = extract_m3u8_from_embed(embed_html)

        return {**match, "embed": embed, "m3u8": m3u8_urls, "home_score": home_score, "away_score": away_score}

    except Exception:
        return {**match, "embed": None, "m3u8": None, "home_score": None, "away_score": None}

# ------------------------------------------------------------------
# LOGO HANDLING (REFINED)
# ------------------------------------------------------------------
def normalize_string(s: str) -> str:
    """Aggressive string normalization: strip accents, lower, alpha-only."""
    if not s:
        return ""
    # Normalize unicode characters (e.g., é -> e, ö -> o)
    s = unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode("utf-8")
    # Convert to lower case and strip special chars (keep spaces)
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return s.strip()

def fetch_and_parse_logos():
    try:
        txt = requests.get(LOGOS_URL, timeout=30).text
    except Exception:
        return {}

    logos = {}
    for chunk in txt.split("------------------------------"):
        fname_m = re.search(r"Filename: (.+?)\.png", chunk)
        url_m = re.search(r"URL: (https?://[^\s]+)", chunk)
        desc_m = re.search(r"Description: ([^\r\n]+)", chunk)

        if not (fname_m and url_m and desc_m):
            continue

        desc = desc_m.group(1).strip()
        # Clean the description: remove country in parens, remove "logo" at end
        clean_desc = re.sub(r"\([^)]*\)", "", desc) 
        clean_desc = re.sub(r"\s+logo$", "", clean_desc, flags=re.IGNORECASE)
        
        search_key = normalize_string(clean_desc)
        filename = fname_m.group(1).strip()

        logos[filename] = {
            "url": url_m.group(1).strip(),
            "search_key": search_key,
            "filename": filename
        }

    return logos

def normalize_team_name(name: str):
    return normalize_string(name)

def load_manual_logos():
    try:
        manual_url = "https://raw.githubusercontent.com/lyfe05/foot_logo/refs/heads/main/manual.txt"
        txt = requests.get(manual_url, timeout=30).text
    except Exception:
        return {}

    manual = {}
    for line in txt.splitlines():
        if "=" not in line:
            continue
        names, url = line.split("=", 1)
        url = url.strip()
        for alias in names.split(","):
            alias = normalize_team_name(alias.strip())
            if alias and url:
                manual[alias] = url
    return manual

def collect_missing_teams(missing_teams: set):
    if missing_teams:
        print("\n=== MISSING TEAM LOGOS ===")
        print("Teams without logos found:")
        for team in sorted(missing_teams):
            print(f"  - {team}")
        print("==========================\n")

def find_logo_url(team_name, league, logos, manual_logos, missing_teams: set):
    """Robust logo finder using internal aliases, accent stripping and fuzzy matching."""
    
    # 1. Normalize the incoming team name
    raw_normalized = normalize_string(team_name)
    
    # 2. Check Manual List (GitHub) matches first
    if raw_normalized in manual_logos:
        return manual_logos[raw_normalized]

    # 3. Check INTERNAL ALIASES (This fixes Wolves, M'gladbach, etc.)
    # We check if the raw normalized name (e.g. "wolves") is in our list
    target_search_name = raw_normalized
    if raw_normalized in KNOWN_ALIASES:
        # If found, we swap "wolves" for "wolverhampton wanderers"
        target_search_name = normalize_string(KNOWN_ALIASES[raw_normalized])
        # Re-check manual list with the new alias just in case
        if target_search_name in manual_logos:
            return manual_logos[target_search_name]

    # 4. Define Helper to get "Core" name (strip common prefixes/suffixes)
    def get_core_name(name):
        garbage = [" fc", " fk", " sc", " ff", " tc", " as", " cf", " sv", " sk", " sp", " cd"]
        core = name
        for g in garbage:
            if core.endswith(g):
                core = core[:-len(g)]
            if core.startswith(g.strip() + " "):
                core = core[len(g.strip())+1:]
        return core.strip()

    team_core = get_core_name(target_search_name)

    # 5. Direct Lookup Strategies in Logos Dictionary
    # Check A: Exact match on filename (e.g. "wolverhampton-wanderers")
    fname_guess = target_search_name.replace(" ", "-")
    if fname_guess in logos:
        return logos[fname_guess]["url"]

    # Check B: Core match on filename
    core_guess = team_core.replace(" ", "-")
    if core_guess in logos:
        return logos[core_guess]["url"]

    # 6. Search in Description Keys (The robust search)
    candidates = []
    
    for filename, data in logos.items():
        logo_key = data["search_key"]
        
        # Exact match of core names (strongest signal)
        if team_core == logo_key:
            return data["url"]
            
        # Substring match: "ferencvaros" in "ferencvarosi tc"
        # We enforce a length check to avoid matching short words like "fc"
        if len(team_core) > 3:
            if team_core in logo_key:
                return data["url"]
        
        candidates.append(logo_key)

    # 7. Fuzzy Matching (Last Resort)
    # We match against the 'target_search_name' (e.g. "wolverhampton wanderers")
    # NOT the original "wolves", because "wolves" is too short/different.
    
    # Attempt 1: Fuzzy match the full alias name
    matches = difflib.get_close_matches(target_search_name, candidates, n=1, cutoff=0.85)
    if matches:
        best_key = matches[0]
        for data in logos.values():
            if data["search_key"] == best_key:
                return data["url"]

    # Attempt 2: Fuzzy match the core name
    matches_core = difflib.get_close_matches(team_core, candidates, n=1, cutoff=0.85)
    if matches_core:
        best_key = matches_core[0]
        for data in logos.values():
            if data["search_key"] == best_key:
                return data["url"]

    # If all fails, mark as missing
    missing_teams.add(team_name)
    return ""

# ------------------------------------------------------------------
# BUILD JSON
# ------------------------------------------------------------------
def process_matches_to_json(matches_data, logos, manual_logos):
    groups = {}
    referer = "|Referer=https://hoofootay4.spotlightmoment.com/"
    missing_teams = set()

    for m in matches_data:
        if not m.get("m3u8"):
            continue

        title = m["title"]
        m3u8_list = m["m3u8"] if isinstance(m["m3u8"], list) else [m["m3u8"]]

        streams = []
        for url in m3u8_list:
            url = url.split("|")[0].strip()
            if "/manifest/0.m3u8" in url:
                streams.append(f"{url}{referer}")

        if title not in groups:
            groups[title] = {
                "image": m.get("image") or "",
                "date": m.get("date") or "",
                "league": m.get("league") or "",
                "streams": [],
                "home_score": m.get("home_score"),
                "away_score": m.get("away_score"),
            }

        groups[title]["streams"].extend(streams)

    result = []
    for title, data in groups.items():
        if " v " not in title:
            continue

        home, away = (x.strip() for x in title.split(" v ", 1))

        result.append({
            "home": {
                "name": home,
                "logo_url": find_logo_url(home, data["league"], logos, manual_logos, missing_teams),
                "score": data["home_score"],
            },
            "away": {
                "name": away,
                "logo_url": find_logo_url(away, data["league"], logos, manual_logos, missing_teams),
                "score": data["away_score"],
            },
            "stream_urls": data["streams"],
            "date": data["date"],
            "league": data["league"],
        })

    collect_missing_teams(missing_teams)
    
    return result

# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def main():
    html = fetch(BASE)
    if not html:
        return

    matches = find_matches_from_html(html)
    if not matches:
        return

    matches_data = []
    for match in matches:
        matches_data.append(process_match(match))
        time.sleep(0.2)

    logos = fetch_and_parse_logos()
    manual_logos = load_manual_logos()

    final = process_matches_to_json(matches_data, logos, manual_logos)

    os.makedirs("api", exist_ok=True)

    with open("api/matches.json", "w", encoding="utf-8") as f:
        json.dump({
            "last_updated": datetime.now().isoformat(),
            "matches_count": len(final),
            "data": final,
        }, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
