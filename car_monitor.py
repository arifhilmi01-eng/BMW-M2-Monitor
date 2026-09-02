#!/usr/bin/env python3
"""
Car Monitor - BMW M2 used car listing scraper and HTML dashboard.
Uses Playwright (headless browser) to bypass JS rendering and bot detection.

Usage:
    python car_monitor.py          Normal run
    python car_monitor.py --test   Treat all listings as new
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "config.json"
SEEN_FILE   = SCRIPT_DIR / "seen_listings.json"
DASHBOARD_FILE = SCRIPT_DIR / "dashboard.html"
LOG_FILE    = SCRIPT_DIR / "last_run.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

_PW = None
_BROWSER = None
_PAGE = None


def start_browser():
    global _PW, _BROWSER, _PAGE
    _PW      = sync_playwright().start()
    _BROWSER = _PW.chromium.launch(headless=True)
    _PAGE    = _BROWSER.new_page(
