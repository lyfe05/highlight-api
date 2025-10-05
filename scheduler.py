#!/usr/bin/env python3
import time
import logging
from scraper import run_scraping_job

logger = logging.getLogger(__name__)

def start_scheduler():
    """Start the 20-minute scraping scheduler"""
    logger.info("⏰ Starting background scheduler (20-minute intervals)")
    
    # Run immediately on startup
    logger.info("🔄 Running initial scrape...")
    run_scraping_job()
    
    # Then run every 20 minutes
    while True:
        time.sleep(1200)  # 20 minutes
        logger.info("🔄 Running scheduled scrape...")
        run_scraping_job()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    start_scheduler()
