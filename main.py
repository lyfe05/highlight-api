#!/usr/bin/env python3
"""
Termux-grade scraper exposed as Render-hosted FastAPI service.
Keeps 100 % of the original pycurl / BeautifulSoup logic.
Only additions: minimal async wrapper + optional auth + cache.
"""
import os
import time
import json
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
import threading
import requests
import re
import pycurl
from io import BytesIO
from bs4 import BeautifulSoup
from urllib.parse import urljoin

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------- CONFIG ----------
BASE        = "https://hoofoot.com/"
UA          = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
LOGOS_URL   = "https://raw.githubusercontent.com/lyfe05/foot_logo/refs/heads/main/logos.txt"
API_KEYS    = os.getenv("API_KEYS", "hoofoot_stream_2025_ZxY9wV8u").split(",")
CACHE_FILE  = "matches_cache.json"
CACHE_TTL   = 1200          # 20 min
security    = HTTPBearer()

# ---------- TERMUX-GRADE FETCH ----------
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
    try:
        c.perform()
        if c.getinfo(pycurl.RESPONSE_CODE) != 200:
            return ""
    except Exception as e:
        logger.error("Fetch fail: %s", e)
        return ""
    finally:
        c.close()
    return buf.getvalue().decode("utf-8", errors="ignore")

# ---------- ORIGINAL HELPERS ----------
normalize_url = lambda src: (
    None if not src else
    "https:" + src if src.startswith("//") else
    urljoin(BASE, src) if src.startswith("/") else
    urljoin(BASE, src) if not src.startswith("http") else
    src
)

def find_matches_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    matches = []
    for container in soup.find_all("div", id=lambda x: x and x.startswith("port")):
        try:
            title = container.find("h2").get_text(strip=True)
            link = container.find("a", href=True)
            if not link or "match=" not in link["href"]:
                continue
            url = normalize_url(link["href"])
            img = container.find("img", src=True)
            image_url = normalize_url(img["src"]) if img else None

            info = container.find("div", class_="info")
            date = info and info.find("span") and info.find("span").get_text(strip=True)
            league_img = info and info.find("img", src=True)
            league = None
            if league_img and "/x/" in league_img["src"]:
                league = league_img["src"].split("/x/")[-1].replace(".jpg", "").replace("_", " ")

            matches.append({"title": title, "url": url, "image": image_url, "date": date, "league": league})
        except Exception:
            continue
    return matches

def extract_embed_url(match_html: str):
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

def extract_m3u8(embed_html: str):
    for pat in [
        r"src\s*:\s*{\s*hls\s*:\s*'(?P<u>//[^']+)'\s*}",
        r"backupSrc\s*:\s*{\s*hls\s*:\s*'(?P<u>//[^']+)'\s*}",
        r"(https?:)?//[^\s'\";]+\.m3u8[^\s'\";]*"
    ]:
        m = re.search(pat, embed_html)
        if m:
            u = m.group(1) if m.lastindex else m.group(0)
            return "https:" + u if u.startswith("//") else u
    return None

def fetch_logos() -> dict:
    try:
        txt = requests.get(LOGOS_URL, timeout=30).text
        logos = {}
        for block in txt.split("------------------------------"):
            fname = re.search(r"Filename: (.+?)\.png", block)
            url   = re.search(r"URL: (https?://.+)", block)
            if fname and url:
                logos[fname.group(1).strip()] = url.group(1).strip()
        return logos
    except Exception as e:
        logger.error("Logo fetch: %s", e)
        return {}

def find_logo(team: str, logos: dict) -> str:
    team = team.lower().strip()
    key  = team.replace(" ", "-")
    if key in logos:
        return logos[key]
    for logo_name, url in logos.items():
        if team in logo_name.split("-"):
            return url
    return ""

# ---------- FULL SCRAPE ----------
def scrape_full() -> list:
    logger.info("Scraping full cycle …")
    home = fetch(BASE)
    if not home:
        return []
    matches = find_matches_from_html(home)
    logos   = fetch_logos()
    out     = []
    referer = "|Referer=https://hoofootay4.spotlightmoment.com/"
    for idx, m in enumerate(matches, 1):
        logger.info("[%d/%d] %s", idx, len(matches), m["title"])
        html   = fetch(m["url"])
        embed  = extract_embed_url(html) if html else None
        m3u8   = None
        if embed:
            m3u8 = extract_m3u8(fetch(embed))
        if m3u8:
            if " v " in m["title"]:
                home_team, away_team = m["title"].split(" v ", 1)
                out.append({
                    "home": {"name": home_team.strip(), "logo_url": find_logo(home_team, logos)},
                    "away": {"name": away_team.strip(), "logo_url": find_logo(away_team, logos)},
                    "stream_urls": [m3u8 + referer],
                    "date": m.get("date") or "",
                    "league": m.get("league") or ""
                })
        time.sleep(1)
    return out

# ---------- CACHE ----------
def read_cache():
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
            return data["matches"] if time.time() - data["timestamp"] < CACHE_TTL else None
    except Exception:
        return None

def write_cache(matches):
    with open(CACHE_FILE, "w") as f:
        json.dump({"timestamp": time.time(), "matches": matches}, f, indent=2)

# ---------- FASTAPI ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=cache_refresher, daemon=True).start()
    yield

def cache_refresher():
    while True:
        logger.info("Background refresh …")
        matches = scrape_full()
        write_cache(matches)
        logger.info("Cache refreshed – %d matches", len(matches))
        time.sleep(CACHE_TTL)

app = FastAPI(title="Football Matches API", version="1.0.0", lifespan=lifespan)

def check_key(creds: HTTPAuthorizationCredentials = Depends(security)):
    if creds.credentials not in API_KEYS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return creds.credentials

@app.get("/")
def root():
    return {"message": "Football Matches API", "docs": "/docs", "matches": "/matches"}

@app.get("/matches")
def get_matches(api_key: str = Depends(check_key)):
    cached = read_cache()
    if cached is None:
        raise HTTPException(status_code=503, detail="Data temporarily unavailable – retry shortly.")
    return JSONResponse(content=cached)

# ---------- RENDER ENTRY ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
                
