#!/usr/bin/env python3
from fastapi import FastAPI, HTTPException, Depends, status, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import os
import time
import json
import logging
import threading
import pycurl
from io import BytesIO
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re
import requests
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# API Keys (comma-separated in environment variable)
API_KEYS = os.getenv("API_KEYS", "hoofoot_stream_2025_ZxY9wV8u").split(",")
CACHE_DURATION = 1200  # 20 minutes

security = HTTPBearer()

# ========== IN-MEMORY CACHE ==========
class MatchCache:
    def __init__(self):
        self.data = []
        self.last_updated = 0
        self.lock = threading.Lock()
        self.is_updating = False
    
    def is_stale(self):
        return (time.time() - self.last_updated) > CACHE_DURATION
    
    def get(self):
        with self.lock:
            return {
                "matches": self.data,
                "last_updated": datetime.fromtimestamp(self.last_updated).isoformat() if self.last_updated else None,
                "cache_age_seconds": int(time.time() - self.last_updated) if self.last_updated else None
            }
    
    def set(self, data):
        with self.lock:
            self.data = data
            self.last_updated = time.time()
            self.is_updating = False
            logger.info(f"✅ Cache updated with {len(data)} matches")

cache = MatchCache()

# ========== SCRAPER FUNCTIONS ==========

BASE = "https://hoofoot.com/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
LOGOS_URL = "https://raw.githubusercontent.com/lyfe05/foot_logo/refs/heads/main/logos.txt"

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
    c.setopt(c.SSL_VERIFYPEER, False)
    c.setopt(c.SSL_VERIFYHOST, False)
    
    try:
        c.perform()
        status_code = c.getinfo(pycurl.RESPONSE_CODE)
        if status_code != 200:
            logger.error(f"HTTP {status_code} from {url}")
            return ""
    except Exception as e:
        logger.error(f"Fetch error for {url}: {e}")
        return ""
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
    if not html:
        return []
        
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
            logger.debug(f"Error processing match container: {e}")
            continue

    return matches

def extract_embed_url(match_html):
    if not match_html:
        return None
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
    if not embed_html:
        return None
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
    except Exception as e:
        logger.error(f"Error processing match {match['title']}: {e}")
        return {
            "title": match['title'], 
            "embed": None, 
            "m3u8": None, 
            "image": match.get("image"),
            "date": match.get("date"),
            "league": match.get("league")
        }

def fetch_and_parse_logos():
    logger.info("📸 Fetching team logos...")
    try:
        logos_response = requests.get(LOGOS_URL, timeout=30)
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
    """Main scraping function"""
    if cache.is_updating:
        logger.info("⏳ Scraping already in progress, skipping...")
        return
        
    cache.is_updating = True
    logger.info("🚀 Starting scheduled scraping job...")
    start_time = time.time()
    
    try:
        home_html = fetch(BASE)
        
        if not home_html:
            logger.error("❌ No HTML content received")
            cache.is_updating = False
            return
            
        matches = find_matches_from_html(home_html)
        if not matches:
            logger.error("❌ No matches found")
            cache.is_updating = False
            return

        logger.info(f"✅ Found {len(matches)} matches")
        
        matches_data = []
        for i, match in enumerate(matches, 1):
            logger.info(f"⏳ [{i}/{len(matches)}] Processing: {match['title']}")
            result = process_match(match)
            matches_data.append(result)
            time.sleep(0.5)  # Be gentle on the server
        
        logo_dict = fetch_and_parse_logos()
        final_json = process_matches_to_json(matches_data, logo_dict)
        
        cache.set(final_json)
        
        elapsed = time.time() - start_time
        logger.info(f"✅ Scraping completed in {elapsed:.2f}s - {len(final_json)} matches cached")
        
    except Exception as e:
        logger.error(f"❌ Scraping job failed: {e}")
        cache.is_updating = False

# ========== BACKGROUND SCHEDULER ==========
def schedule_scraping():
    """Background thread to periodically update cache"""
    logger.info("🔄 Starting background scraping scheduler...")
    
    # Run immediately on startup
    run_scraping_job()
    
    while True:
        try:
            time.sleep(60)  # Check every minute
            
            if cache.is_stale():
                logger.info("⏰ Cache is stale, triggering refresh...")
                run_scraping_job()
                
        except Exception as e:
            logger.error(f"Scheduler error: {e}")

# ========== FASTAPI APP ==========

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background scraping thread
    scraping_thread = threading.Thread(target=schedule_scraping, daemon=True)
    scraping_thread.start()
    logger.info("✅ Background scraper started")
    
    yield
    
    logger.info("🛑 Shutting down...")

app = FastAPI(
    title="HooFoot Match Scraper API",
    description="Scrapes and caches football match streams every 20 minutes",
    version="2.0",
    lifespan=lifespan
)

# ========== AUTH ==========
def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials not in API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )
    return credentials.credentials

# ========== ENDPOINTS ==========

@app.get("/")
async def root():
    return {
        "message": "HooFoot Match Scraper API",
        "version": "2.0",
        "endpoints": {
            "/matches": "Get cached matches (requires auth)",
            "/health": "Health check",
            "/refresh": "Force refresh cache (requires auth)"
        }
    }

@app.get("/health")
async def health():
    cache_data = cache.get()
    return {
        "status": "healthy",
        "cache_status": "empty" if not cache_data["matches"] else "populated",
        "matches_count": len(cache_data["matches"]),
        "last_updated": cache_data["last_updated"],
        "cache_age_seconds": cache_data["cache_age_seconds"],
        "is_updating": cache.is_updating
    }

@app.get("/matches")
async def get_matches(api_key: str = Depends(verify_api_key)):
    cache_data = cache.get()
    
    if not cache_data["matches"]:
        raise HTTPException(
            status_code=503,
            detail="Cache is still initializing, please try again in a moment"
        )
    
    return cache_data

@app.post("/refresh")
async def force_refresh(background_tasks: BackgroundTasks, api_key: str = Depends(verify_api_key)):
    if cache.is_updating:
        return {
            "message": "Scraping already in progress",
            "status": "pending"
        }
    
    background_tasks.add_task(run_scraping_job)
    
    return {
        "message": "Cache refresh triggered",
        "status": "initiated"
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
