"""Healthcheck for scraper service."""

import sys
from scraper import WhoscoredScraper

def main():
    """Run healthcheck."""
    scraper = WhoscoredScraper()
    if scraper.healthcheck():
        print("Scraper is healthy")
        sys.exit(0)
    else:
        print("Scraper healthcheck failed")
        sys.exit(1)

if __name__ == "__main__":
    main()
