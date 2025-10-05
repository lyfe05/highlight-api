#!/usr/bin/env python3
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
import os
import time
import json
import logging
from typing import List, Optional

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Football Matches API", version="1.0.0")
security = HTTPBearer()

# Cache storage
CACHE_FILE = "matches_cache.json"
CACHE_DURATION = 1200  # 20 minutes in seconds

# API Keys (comma-separated in environment variable)
API_KEYS = os.getenv("API_KEYS", "default-secret-key").split(",")

def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials not in API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )
    return credentials.credentials

def get_cached_matches():
    """Read matches from cache file if valid"""
    try:
        if not os.path.exists(CACHE_FILE):
            return None
            
        with open(CACHE_FILE, 'r') as f:
            cache_data = json.load(f)
            
        # Check if cache is still valid
        if time.time() - cache_data.get('timestamp', 0) < CACHE_DURATION:
            return cache_data['matches']
        else:
            logger.info("Cache expired, needs refresh")
            return None
    except Exception as e:
        logger.error(f"Error reading cache: {e}")
        return None

@app.get("/")
async def root():
    return {"message": "Football Matches API", "status": "running"}

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    cache_status = "valid" if get_cached_matches() is not None else "expired/missing"
    return {
        "status": "healthy",
        "cache_status": cache_status,
        "timestamp": time.time()
    }

@app.get("/matches")
async def get_matches(api_key: str = Depends(verify_api_key)):
    """Get all football matches with streams (API key required)"""
    logger.info(f"API request from key: {api_key[:8]}...")
    
    matches = get_cached_matches()
    if matches is not None:
        logger.info("Serving cached matches")
        return JSONResponse(content=matches)
    else:
        logger.warning("No valid cache available")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Data temporarily unavailable. Please try again shortly."
        )

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
