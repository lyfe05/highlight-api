#!/usr/bin/env python3
import pycurl
from io import BytesIO
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re
import time
import json
import requests
import logging
import os

logger = logging.getLogger(__name__)

BASE = "https://hoofoot.com/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
LOGOS_URL = "https://raw.githubusercontent.com/lyfe05/foot_logo/refs/heads/main/logos.txt"
CACHE_FILE = "matches_cache.json"

def fetch(url, timeout=30):
    buf = BytesIO()
    c = pycurl.Curl()
    c.setopt(c.URL, url)
    c.setopt(c.WRITEFUNCTION, buf.write)
    c.setopt(c.FOLLOWLOCATION, True)
    c.setopt(c.USERAGENT, UA)
    c.setopt(c.ACCEPT_ENCODING, "gzip, deflate")
    c.setopt(c.CONNECTTIMEOUT, 10)
    c.setopt(c.TIMEOUT, timeout)
    try:
        c.perform()
    finally:
        c.close()
    return buf.getvalue().decode("utf-8", errors="ignore")

def normalize_url(src):
    if not src:
        return None
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return urljoin(BASE, src)
    if not src.startswith("http"):
        return urljoin(BASE, src)
    return src

def find_matches_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    matches = []

    match_containers = soup.find_all("div", id=lambda x: x and x.startswith("port"))

    for container in match_containers:
        try:
            title_element = container.find("h2")
            if not title_element:
                continue
            title = title_element.get_text(strip=True)

            link_element = container.find("a", href=True)
            if not link_element or "match=" not in link_element["href"]:
                continue
            url = normalize_url(link_element["href"])

            img_element = container.find("img", src=True)
            image_url = normalize_url(img_element["src"]) if img_element else None

            # Extract date and league from info section
            info_section = container.find("div", class_="info")
            date = None
            league = None
            
            if info_section:
                date_span = info_section.find("span")
                if date_span:
                    date = date_span.get_text(strip=True)
                
                league_img = info_section.find("img", src=True)
                if league_img and "src" in league_img.attrs:
                    league_src = league_img["src"]
                    if "/x/" in league_src:
                        league_name = league_src.split("/x/")[-1].replace(".jpg", "").replace("_", " ")
                        league = league_name

            matches.append({
                "title": title,
                "url": url,
                "image": image_url,
                "date": date,
                "league": league
            })
        except Exception as e:
            continue

    return matches

def extract_embed_url(match_html):
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

def extract_m3u8_from_embed(embed_html):
    m = re.search(r"src\s*:\s*{\s*hls\s*:\s*'(?P<u>//[^']+)'\s*}", embed_html)
    if m:
        return "https:" + m.group("u")
    m = re.search(r"backupSrc\s*:\s*{\s*hls\s*:\s*'(?P<u>//[^']+)'\s*}", embed_html)
    if m:
        return "https:" + m.group("u")
    m = re.search(r"(https?:)?//[^\s'\";]+\.m3u8[^\s'\";]*", embed_html)
    if m:
        url = m.group(0)
        if url.startswith("//"):
            return "https:" + url
        return url
    return None

def process_match(match):
    try:
        m_html = fetch(match['url'])
        embed = extract_embed_url(m_html)
        if not embed:
            return {
                "title": match['title'], 
                "embed": None, 
                "m3u8": None, 
                "image": match.get("image"),
                "date": match.get("date"),
                "league": match.get("league")
            }
        embed_html = fetch(embed)
        m3u8 = extract_m3u8_from_embed(embed_html)
        return {
            "title": match['title'], 
            "embed": embed, 
            "m3u8": m3u8, 
            "image": match.get("image"),
            "date": match.get("date"),
            "league": match.get("league")
        }
    except Exception:
        return {
            "title": match['title'], 
            "embed": None, 
            "m3u8": None, 
            "image": match.get("image"),
            "date": match.get("date"),
            "league": match.get("league")
        }

def fetch_and_parse_logos():
    """Fetch and parse team logos"""
    logger.info("📸 Fetching team logos...")
    try:
        logos_response = requests.get(LOGOS_URL)
        logos_response.raise_for_status()
        logos_content = logos_response.text
        
        logo_dict = {}
        logo_entries = logos_content.split("------------------------------")
        
        for entry in logo_entries:
            filename_match = re.search(r"Filename: (.+?)\.png", entry)
            url_match = re.search(r"URL: (https?://[^\s]+)", entry)
            
            if filename_match and url_match:
                filename = filename_match.group(1).strip()
                url = url_match.group(1).strip()
                logo_dict[filename] = url
        
        logger.info(f"✅ Loaded {len(logo_dict)} team logos")
        return logo_dict
    except Exception as e:
        logger.error(f"❌ Error fetching logos: {e}")
        return {}

def find_logo_url(team_name, logo_dict):
    """Find logo URL using improved matching"""
    team_lower = team_name.lower().strip()
    
    exact_match = team_lower.replace(" ", "-")
    if exact_match in logo_dict:
        return logo_dict[exact_match]
    
    for logo_filename, logo_url in logo_dict.items():
        words = logo_filename.split('-')
        
        if team_lower in words:
            return logo_url
            
        team_words = team_lower.split()
        for team_word in team_words:
            if team_word in words:
                return logo_url
    
    return ""

def process_matches_to_json(matches_data, logo_dict):
    """Process scraped matches into structured JSON"""
    logger.info("🔄 Processing matches and matching team logos...")
    
    match_groups = {}
    referer = "|Referer=https://hoofootay4.spotlightmoment.com/"
    
    for match_data in matches_data:
        if match_data['m3u8']:
            match_name = match_data['title']
            m3u8_url = match_data['m3u8'] + referer
            
            if match_name not in match_groups:
                match_groups[match_name] = {
                    'image': match_data['image'] or '',
                    'stream_urls': [],
                    'date': match_data.get('date', ''),
                    'league': match_data.get('league', '')
                }
            
            match_groups[match_name]['stream_urls'].append(m3u8_url)
    
    result = []
    
    for match_name, data in match_groups.items():
        if " v " in match_name:
            home_team, away_team = match_name.split(" v ", 1)
            
            home_logo = find_logo_url(home_team, logo_dict)
            away_logo = find_logo_url(away_team, logo_dict)
            
            match_data = {
                "home": {
                    "name": home_team.strip(),
                    "logo_url": home_logo
                },
                "away": {
                    "name": away_team.strip(),
                    "logo_url": away_logo
                },
                "stream_urls": data['stream_urls'],
                "date": data['date'],
                "league": data['league']
            }
            result.append(match_data)
    
    return result

def run_scraping_job():
    """Main scraping function to be called by scheduler"""
    logger.info("🚀 Starting scheduled scraping job...")
    start_time = time.time()
    
    try:
        # Step 1: Fetch matches from HooFoot
        logger.info("📡 Fetching HooFoot homepage...")
        home_html = fetch(BASE)
        
        matches = find_matches_from_html(home_html)
        if not matches:
            logger.error("❌ No matches found.")
            return False

        logger.info(f"✅ Found {len(matches)} matches")
        
        # Step 2: Extract stream URLs
        logger.info("🎬 Extracting stream URLs...")
        matches_data = []
        for i, match in enumerate(matches, 1):
            logger.info(f"⏳ [{i}/{len(matches)}] Processing: {match['title']}")
            result = process_match(match)
            matches_data.append(result)
            time.sleep(1)  # Be respectful to the server
        
        # Step 3: Fetch logos
        logo_dict = fetch_and_parse_logos()
        
        # Step 4: Process everything into final JSON
        final_json = process_matches_to_json(matches_data, logo_dict)
        
        # Step 5: Save to cache
        cache_data = {
            'timestamp': time.time(),
            'matches': final_json
        }
        
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache_data, f, indent=2)
        
        elapsed_time = time.time() - start_time
        logger.info(f"✅ Scraping completed in {elapsed_time:.1f}s. Found {len(final_json)} matches. Cache updated.")
        return True
        
    except Exception as e:
        logger.error(f"❌ Scraping job failed: {e}")
        return False
