#!/usr/bin/env python3
"""
Car Monitor - BMW M2 used car listing scraper and HTML dashboard.
Routes requests through ScraperAPI to bypass bot detection.

Usage:
    python car_monitor.py          Normal run
    python car_monitor.py --test   Treat all listings as new
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, quote

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR     = Path(__file__).parent.resolve()
CONFIG_FILE    = SCRIPT_DIR / "config.json"
SEEN_FILE      = SCRIPT_DIR / "seen_listings.json"
DASHBOARD_FILE = SCRIPT_DIR / "dashboard.html"
LOG_FILE       = SCRIPT_DIR / "last_run.log"

SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


def get_page(url: str, params: Optional[dict] = None) -> Optional[BeautifulSoup]:
    """Fetch a URL, routing through ScraperAPI if a key is available."""
    try:
        if params:
            url = url + "?" + urlencode(params)

        if SCRAPER_API_KEY:
            api_url = f"https://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={quote(url)}&render=true"
            resp = SESSION.get(api_url, timeout=60)
        else:
            resp = SESSION.get(url, timeout=20)

        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as exc:
        log.error("Request failed for %s: %s", url, exc)
        return None


def _extract_year_mileage(text: str):
    year = ""
    mileage = ""
    m = re.search(r"\b(199\d|20[0-3]\d)\b", text)
    if m: year = m.group(1)
    m = re.search(r"([\d,]+)\s*(?:miles|mile|mi\b)", text, re.I)
    if m:
        mileage = m.group(1).replace(",", "")
    else:
        m = re.search(r"\b(\d{2,3},\d{3})\b", text)
        if m: mileage = m.group(1).replace(",", "")
    return year, mileage


# ---------------------------------------------------------------------------
# AutoTrader
# ---------------------------------------------------------------------------
def scrape_autotrader(filters: dict) -> list:
    listings = []
    postcode = filters["postcode"].replace(" ", "")
    base_url = "https://www.autotrader.co.uk/car-search"
    log.info("Scraping AutoTrader")

    for page_num in range(1, 6):
        params = {
            "make": filters["make"],
            "model": filters["model"],
            "price-to": filters["max_price"],
            "year-from": filters["min_year"],
            "year-to": filters["max_year"],
            "postcode": postcode,
            "radius": filters["radius_miles"],
            "include-delivery-option": "on",
            "advertising-location": "at_cars",
            "page": page_num,
        }
        soup = get_page(base_url, params)
        if not soup:
            break

        page_listings = []

        # JSON-LD (most reliable when available)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data  = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") not in ("Car", "Vehicle", "Product"):
                        continue
                    lid = str(item.get("productID") or item.get("identifier") or "")
                    if not lid: continue
                    offers = item.get("offers", {})
                    price  = offers.get("price") or "N/A"
                    url    = item.get("url") or offers.get("url") or ""
                    ym     = _extract_year_mileage(str(item))
                    page_listings.append({
                        "id": lid,
                        "title": item.get("name", "Unknown"),
                        "price": f"£{price}" if str(price).isdigit() else str(price),
                        "year": str(item.get("vehicleModelDate", "") or ym[0]),
                        "mileage": str((item.get("mileageFromOdometer") or {}).get("value", "") or ym[1]),
                        "url": url, "site": "autotrader", "image_url": item.get("image", ""),
                    })
            except Exception:
                pass

        # HTML article fallback
        if not page_listings:
            articles = (
                soup.find_all("article", attrs={"data-standout-type": True})
                or soup.find_all("article")
            )
            for art in articles:
                lid  = art.get("id") or art.get("data-advert-id")
                link = art.find("a", href=True)
                url  = ""
                if link:
                    href = link["href"]
                    url  = href if href.startswith("http") else "https://www.autotrader.co.uk" + href
                    if not lid:
                        m = re.search(r"/(\d{8,})", href)
                        if m: lid = m.group(1)
                if not lid: continue
                title_el = art.find("h2") or art.find("h3") or art.select_one("[data-gui='advert-title']")
                title    = title_el.get_text(strip=True) if title_el else "Unknown"
                price_el = art.select_one("[data-gui='advert-price']") or art.find(class_=re.compile(r"price", re.I))
                price    = price_el.get_text(strip=True) if price_el else "N/A"
                year, mileage = _extract_year_mileage(art.get_text(" "))
                img = art.find("img")
                page_listings.append({
                    "id": lid, "title": title, "price": price, "year": year,
                    "mileage": mileage, "url": url, "site": "autotrader",
                    "image_url": (img.get("src") or img.get("data-src") or "") if img else "",
                })

        if not page_listings:
            log.warning("AutoTrader page %d: no listings found", page_num)
            break

        listings.extend(page_listings)
        log.info("AutoTrader page %d: %d listings", page_num, len(page_listings))

        if not soup.select_one("a[data-gui='pagination-next']"):
            break
        time.sleep(1)

    log.info("AutoTrader: total %d listings", len(listings))
    return listings


# ---------------------------------------------------------------------------
# PistonHeads
# ---------------------------------------------------------------------------
def scrape_pistonheads(filters: dict) -> list:
    listings = []
    postcode = filters["postcode"].replace(" ", "")
    base_url = "https://www.pistonheads.com/classifieds/used-cars"
    log.info("Scraping PistonHeads")

    for page_num in range(1, 6):
        params = {
            "make": filters["make"],
            "model": filters["model"],
            "priceTo": filters["max_price"],
            "yearFrom": filters["min_year"],
            "yearTo": filters["max_year"],
            "within": filters["radius_miles"],
            "postcode": postcode,
            "page": page_num,
        }
        soup = get_page(base_url, params)
        if not soup:
            break

        page_listings = []

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data  = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") not in ("Car", "Vehicle", "Product"):
                        continue
                    lid = str(item.get("productID") or item.get("identifier") or "")
                    if not lid: continue
                    offers = item.get("offers", {})
                    price  = offers.get("price") or "N/A"
                    url    = item.get("url") or offers.get("url") or ""
                    ym     = _extract_year_mileage(str(item))
                    page_listings.append({
                        "id": lid,
                        "title": item.get("name", "Unknown"),
                        "price": f"£{price}" if str(price).isdigit() else str(price),
                        "year": str(item.get("vehicleModelDate", "") or ym[0]),
                        "mileage": str((item.get("mileageFromOdometer") or {}).get("value", "") or ym[1]),
                        "url": url, "site": "pistonheads", "image_url": item.get("image", ""),
                    })
            except Exception:
                pass

        if not page_listings:
            cards = soup.select("li.listing, article.listing-card, div.listing-masthead")
            for card in cards:
                link = card.find("a", href=True)
                if not link: continue
                href  = link["href"]
                url   = href if href.startswith("http") else "https://www.pistonheads.com" + href
                m     = re.search(r"/(\d+)(?:[/?#]|$)", href)
                lid   = m.group(1) if m else href.split("/")[-1]
                title_el = card.find("h2") or card.find("h3")
                title = title_el.get_text(strip=True) if title_el else "Unknown"
                price_el = card.find(class_=re.compile(r"price", re.I))
                price = price_el.get_text(strip=True) if price_el else "N/A"
                year, mileage = _extract_year_mileage(card.get_text(" "))
                img = card.find("img")
                page_listings.append({
                    "id": lid, "title": title, "price": price, "year": year,
                    "mileage": mileage, "url": url, "site": "pistonheads",
                    "image_url": (img.get("src") or img.get("data-src") or "") if img else "",
                })

        if not page_listings:
            log.warning("PistonHeads page %d: no listings", page_num)
            break

        listings.extend(page_listings)
        log.info("PistonHeads page %d: %d listings", page_num, len(page_listings))

        if not soup.select_one("a[rel='next'], a.next"):
            break
        time.sleep(1)

    log.info("PistonHeads: total %d listings", len(listings))
    return listings


# ---------------------------------------------------------------------------
# BMW UK
# ---------------------------------------------------------------------------
def scrape_bmw_uk(filters: dict) -> list:
    log.info("Scraping BMW UK Approved Used")
    url  = (
        "https://www.bmw.co.uk/en/topics/find-a-car/used-cars/find-your-bmw.html"
        f"#model=M2&yearFrom={filters['min_year']}&mileageTo={filters['max_mileage']}"
        f"&priceTo={filters['max_price']}"
    )
    soup = get_page(url)
    if not soup:
        log.warning("BMW UK: page load failed")
        return []

    listings = []
    cards = (
        soup.select("div.used-car-tile")
        or soup.select("div[class*='vehicle-card']")
        or soup.select("article[class*='vehicle']")
        or soup.select("div[class*='car-tile']")
    )

    for card in cards:
        try:
            link = card.find("a", href=True)
            if not link: continue
            href  = link["href"]
            url_v = href if href.startswith("http") else "https://www.bmw.co.uk" + href
            m     = re.search(r"/(\d{6,})", href)
            lid   = m.group(1) if m else href.rstrip("/").split("/")[-1]
            title_el = card.find("h2") or card.find("h3") or card.find(class_=re.compile(r"title|heading", re.I))
            title = title_el.get_text(strip=True) if title_el else "BMW Vehicle"
            price_el = card.find(class_=re.compile(r"price", re.I))
            price = price_el.get_text(strip=True) if price_el else "N/A"
            year, mileage = _extract_year_mileage(card.get_text(" "))
            img = card.find("img")
            listings.append({
                "id": lid, "title": title, "price": price, "year": year,
                "mileage": mileage, "url": url_v, "site": "bmw_uk",
                "image_url": (img.get("src") or img.get("data-src") or "") if img else "",
            })
        except Exception as exc:
            log.debug("BMW UK card error: %s", exc)

    log.info("BMW UK: %d listings found", len(listings))
    return listings


# ---------------------------------------------------------------------------
# Seen listings
# ---------------------------------------------------------------------------
def load_seen() -> dict:
    if SEEN_FILE.exists():
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"autotrader": [], "pistonheads": [], "bmw_uk": []}


def save_seen(seen: dict):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def write_dashboard(all_listings: list, new_ids: set, filters: dict):
    now_str   = datetime.now().strftime("%d %b %Y at %H:%M")
    new_count = sum(1 for l in all_listings if l["id"] in new_ids)
    total     = len(all_listings)

    site_label = {"autotrader": "AutoTrader", "pistonheads": "PistonHeads", "bmw_uk": "BMW UK"}
    site_color = {"autotrader": "#ef6c00",    "pistonheads": "#1565c0",     "bmw_uk": "#0066cc"}

    sorted_listings = sorted(all_listings, key=lambda l: (0 if l["id"] in new_ids else 1, l["site"]))

    cards_html = ""
    for l in sorted_listings:
        is_new    = l["id"] in new_ids
        new_badge = '<span style="background:#d32f2f;color:#fff;font-size:11px;font-weight:bold;padding:2px 8px;border-radius:4px;text-transform:uppercase;">NEW</span>' if is_new else ""
        border    = "border:2px solid #d32f2f;" if is_new else "border:1px solid #e0e0e0;"
        img_html  = f'<img src="{l["image_url"]}" alt="" style="width:100%;height:180px;object-fit:cover;border-radius:6px 6px 0 0;display:block;">' if l.get("image_url") else ""
        color     = site_color.get(l["site"], "#555")
        label     = site_label.get(l["site"], l["site"])
        mile_disp = f"{int(l['mileage']):,} miles" if l.get("mileage", "").isdigit() else (l.get("mileage") or "N/A")

        cards_html += f"""
        <div style="{border}border-radius:8px;background:#fff;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 1px 4px rgba(0,0,0,.08);">
          {img_html}
          <div style="padding:14px 16px;flex:1;display:flex;flex-direction:column;gap:6px;">
            <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;">
              <span style="background:{color};color:#fff;font-size:10px;font-weight:bold;padding:2px 7px;border-radius:4px;text-transform:uppercase;">{label}</span>
              {new_badge}
            </div>
            <div style="font-size:15px;font-weight:600;color:#212121;">{l['title']}</div>
            <div style="font-size:18px;font-weight:700;color:#2e7d32;">{l['price']}</div>
            <div style="font-size:13px;color:#616161;">&#128197; {l.get('year') or 'N/A'} &nbsp;&bull;&nbsp; &#128663; {mile_disp}</div>
            <a href="{l['url']}" target="_blank" style="margin-top:auto;padding:8px 14px;background:#1565c0;color:#fff;text-decoration:none;border-radius:4px;font-size:13px;font-weight:bold;text-align:center;display:block;">View Listing &rarr;</a>
          </div>
        </div>"""

    summary       = f"{new_count} new" if new_count else "No new listings"
    max_mile_fmt  = f"{filters.get('max_mileage', 0):,}"
    max_price_fmt = f"{filters.get('max_price', 0):,}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>BMW M2 Monitor</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#212121}}
    .header{{background:#1a237e;color:#fff;padding:20px 24px}}
    .header h1{{font-size:20px;font-weight:700}}
    .header p{{font-size:13px;opacity:.75;margin-top:4px}}
    .stats{{display:flex;gap:16px;padding:14px 24px;background:#fff;border-bottom:1px solid #e0e0e0;flex-wrap:wrap}}
    .stat{{font-size:13px;color:#616161}}
    .stat strong{{color:#212121}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;padding:20px 24px;max-width:1400px;margin:0 auto}}
    .empty{{text-align:center;padding:60px 24px;color:#9e9e9e;font-size:15px}}
  </style>
</head>
<body>
  <div class="header">
    <h1>&#128663; BMW M2 Monitor</h1>
    <p>Last updated: {now_str} &mdash; {total} total &mdash; <strong style="color:#ef9a9a;">{summary}</strong></p>
  </div>
  <div class="stats">
    <div class="stat">Filters: <strong>&ge; {filters.get('min_year','')} &bull; &le; {max_mile_fmt} miles &bull; &le; &pound;{max_price_fmt}</strong></div>
    <div class="stat">Area: <strong>{filters.get('postcode','')} &bull; {filters.get('radius_miles','')} mile radius</strong></div>
    <div class="stat">Sources: <strong>AutoTrader &bull; PistonHeads &bull; BMW UK</strong></div>
  </div>
  {"<div class='grid'>" + cards_html + "</div>" if total else "<div class='empty'>No listings found matching your filters.</div>"}
</body>
</html>"""

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("Dashboard written: %d listings, %d new", total, new_count)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Car Monitor starting — %s%s", datetime.now().isoformat(timespec="seconds"), " [TEST MODE]" if args.test else "")

    if SCRAPER_API_KEY:
        log.info("ScraperAPI key found — routing through proxy")
    else:
        log.warning("No SCRAPER_API_KEY — requests may be blocked by sites")

    if not CONFIG_FILE.exists():
        log.error("config.json not found")
        sys.exit(1)

    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)

    filters = config["filters"]
    sites   = config.get("sites", {"autotrader": True, "pistonheads": True, "bmw_uk": True})

    all_listings = []

    if sites.get("autotrader", True):
        try:    all_listings.extend(scrape_autotrader(filters))
        except Exception as exc: log.error("AutoTrader crashed: %s", exc)

    if sites.get("pistonheads", True):
        try:    all_listings.extend(scrape_pistonheads(filters))
        except Exception as exc: log.error("PistonHeads crashed: %s", exc)

    if sites.get("bmw_uk", True):
        try:    all_listings.extend(scrape_bmw_uk(filters))
        except Exception as exc: log.error("BMW UK crashed: %s", exc)

    log.info("Total listings scraped: %d", len(all_listings))

    seen = load_seen()

    if args.test:
        new_ids = {l["id"] for l in all_listings}
        log.info("TEST MODE: %d listings all marked new", len(new_ids))
    else:
        new_ids = {l["id"] for l in all_listings if l["id"] not in seen.get(l["site"], [])}

    log.info("New listings: %d", len(new_ids))
    write_dashboard(all_listings, new_ids, filters)

    if not args.test:
        for l in all_listings:
            if l["site"] not in seen:
                seen[l["site"]] = []
            if l["id"] not in seen[l["site"]]:
                seen[l["site"]].append(l["id"])
        save_seen(seen)

    log.info("Run complete — %d total, %d new.", len(all_listings), len(new_ids))
    log.info("=" * 60)


if __name__ == "__main__":
    main()
