#!/usr/bin/env python3
"""
Car Monitor - Used car listing scraper and HTML dashboard.
Scrapes AutoTrader UK, PistonHeads, and BMW UK for new listings matching
user-defined filters, then writes/updates dashboard.html with all results.

Open dashboard.html in your browser and refresh it to see the latest listings.
New listings (not seen on the previous run) are highlighted with a NEW badge.

Usage:
    python car_monitor.py          # Normal run — updates dashboard.html
    python car_monitor.py --test   # Treat all listings as new (ignores seen state)
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

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "config.json"
SEEN_FILE = SCRIPT_DIR / "seen_listings.json"
DASHBOARD_FILE = SCRIPT_DIR / "dashboard.html"
LOG_FILE = SCRIPT_DIR / "last_run.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared HTTP session with realistic browser headers
# ---------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }
)


def get_page(url: str, params: Optional[dict] = None, json_response: bool = False, extra_headers: Optional[dict] = None):
    """Fetch a URL, returning parsed JSON or BeautifulSoup object. Returns None on error."""
    try:
        headers = {}
        if extra_headers:
            headers.update(extra_headers)
        resp = SESSION.get(url, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        if json_response:
            return resp.json()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as exc:
        log.error("Request failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# AutoTrader UK scraper
# ---------------------------------------------------------------------------

def scrape_autotrader(filters: dict) -> list[dict]:
    listings = []
    postcode_clean = filters["postcode"].replace(" ", "")

    params = {
        "make": filters["make"],
        "model": filters["model"],
        "price-to": filters["max_price"],
        "year-from": filters["min_year"],
        "year-to": filters["max_year"],
        "postcode": postcode_clean,
        "radius": filters["radius_miles"],
        "include-delivery-option": "on",
        "advertising-location": "at_cars",
    }

    base_url = "https://www.autotrader.co.uk/car-search"
    log.info("Scraping AutoTrader: %s", base_url)

    for page_num in range(1, 6):
        params["page"] = page_num
        soup = get_page(base_url, params=params)
        if soup is None:
            break

        articles = soup.find_all("article", attrs={"data-standout-type": True})
        if not articles:
            articles = soup.select("li.search-page__result")
        if not articles:
            articles = soup.select("ul.search-form__results li article")
        if not articles:
            articles = soup.find_all("article")

        if not articles:
            log.warning("AutoTrader: no listing elements found on page %d (markup may have changed)", page_num)
            break

        page_listings = []
        for art in articles:
            try:
                listing = _parse_autotrader_article(art)
                if listing:
                    page_listings.append(listing)
            except Exception as exc:
                log.debug("AutoTrader: failed to parse article: %s", exc)

        if not page_listings:
            break

        listings.extend(page_listings)
        log.info("AutoTrader page %d: %d listings", page_num, len(page_listings))

        next_btn = soup.select_one("a[data-gui='pagination-next']") or soup.select_one("a.next-page")
        if not next_btn:
            break

        time.sleep(1.5)

    if not listings:
        log.info("AutoTrader: attempting JSON-LD fallback")
        soup = get_page(base_url, params={k: v for k, v in params.items() if k != "page"})
        if soup:
            listings = _extract_autotrader_jsonld(soup)

    log.info("AutoTrader: total %d listings found", len(listings))
    return listings


def _parse_autotrader_article(art) -> Optional[dict]:
    listing_id = art.get("id") or art.get("data-advert-id")

    link_el = art.find("a", href=True)
    url = ""
    if link_el:
        href = link_el["href"]
        url = href if href.startswith("http") else "https://www.autotrader.co.uk" + href
        if not listing_id:
            m = re.search(r"/(\d{8,})", href)
            if m:
                listing_id = m.group(1)

    if not listing_id:
        return None

    title_el = (
        art.find("h2")
        or art.find("h3")
        or art.select_one("[data-gui='advert-title']")
        or art.select_one(".listing-title")
    )
    title = title_el.get_text(strip=True) if title_el else "Unknown"

    price_el = (
        art.select_one("[data-gui='advert-price']")
        or art.select_one(".vehicle-price")
        or art.find(class_=re.compile(r"price", re.I))
    )
    price = price_el.get_text(strip=True) if price_el else "N/A"

    year, mileage = _extract_year_mileage(art.get_text(" "))

    img_el = art.find("img")
    image_url = img_el.get("src") or img_el.get("data-src") if img_el else ""

    return {
        "id": listing_id,
        "title": title,
        "price": price,
        "year": year,
        "mileage": mileage,
        "url": url,
        "site": "autotrader",
        "image_url": image_url or "",
    }


def _extract_autotrader_jsonld(soup) -> list[dict]:
    listings = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") in ("Car", "Vehicle", "Product"):
                    listing_id = str(item.get("productID") or item.get("identifier") or "")
                    if not listing_id:
                        continue
                    offers = item.get("offers", {})
                    price = offers.get("price") or "N/A"
                    url = item.get("url") or offers.get("url") or ""
                    listings.append(
                        {
                            "id": listing_id,
                            "title": item.get("name", "Unknown"),
                            "price": f"£{price}" if str(price).isdigit() else str(price),
                            "year": str(item.get("vehicleModelDate", "")),
                            "mileage": str(item.get("mileageFromOdometer", {}).get("value", "")),
                            "url": url,
                            "site": "autotrader",
                            "image_url": item.get("image", ""),
                        }
                    )
        except (json.JSONDecodeError, AttributeError):
            pass
    return listings


# ---------------------------------------------------------------------------
# PistonHeads scraper
# ---------------------------------------------------------------------------

def scrape_pistonheads(filters: dict) -> list[dict]:
    listings = []
    postcode_clean = filters["postcode"].replace(" ", "")

    base_url = "https://www.pistonheads.com/classifieds/used-cars"
    log.info("Scraping PistonHeads: %s", base_url)

    for page_num in range(1, 6):
        params = {
            "make": filters["make"],
            "model": filters["model"],
            "priceTo": filters["max_price"],
            "yearFrom": filters["min_year"],
            "yearTo": filters["max_year"],
            "within": filters["radius_miles"],
            "postcode": postcode_clean,
            "page": page_num,
        }

        soup = get_page(base_url, params=params)
        if soup is None:
            break

        cards = soup.select("div.listing-masthead") or soup.select("li.listing") or soup.select("article.listing-card")
        if not cards:
            cards = soup.find_all("div", class_=re.compile(r"listing", re.I))

        if not cards:
            log.warning("PistonHeads: no listing cards found on page %d", page_num)
            break

        page_listings = []
        for card in cards:
            try:
                listing = _parse_pistonheads_card(card)
                if listing:
                    page_listings.append(listing)
            except Exception as exc:
                log.debug("PistonHeads: failed to parse card: %s", exc)

        if not page_listings:
            break

        listings.extend(page_listings)
        log.info("PistonHeads page %d: %d listings", page_num, len(page_listings))

        next_btn = soup.select_one("a[rel='next']") or soup.select_one("a.next")
        if not next_btn:
            break

        time.sleep(1.5)

    log.info("PistonHeads: total %d listings found", len(listings))
    return listings


def _parse_pistonheads_card(card) -> Optional[dict]:
    link_el = card.find("a", href=True)
    if not link_el:
        return None

    href = link_el["href"]
    url = href if href.startswith("http") else "https://www.pistonheads.com" + href

    m = re.search(r"/(\d+)(?:[/?#]|$)", href)
    listing_id = m.group(1) if m else href.split("/")[-1]
    if not listing_id:
        return None

    title_el = card.find("h2") or card.find("h3") or card.find(class_=re.compile(r"title", re.I))
    title = title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True) or "Unknown"

    price_el = card.find(class_=re.compile(r"price", re.I))
    price = price_el.get_text(strip=True) if price_el else "N/A"

    year, mileage = _extract_year_mileage(card.get_text(" "))

    img_el = card.find("img")
    image_url = ""
    if img_el:
        image_url = img_el.get("src") or img_el.get("data-src") or img_el.get("data-lazy-src") or ""

    return {
        "id": listing_id,
        "title": title,
        "price": price,
        "year": year,
        "mileage": mileage,
        "url": url,
        "site": "pistonheads",
        "image_url": image_url,
    }


# ---------------------------------------------------------------------------
# BMW UK scraper
# ---------------------------------------------------------------------------

BMW_API_ENDPOINTS = [
    "https://www.bmw.co.uk/api/v1/used-cars/search",
    "https://www.bmw.co.uk/en/topics/find-a-car/used-cars/used-car-search.api.json",
]


def scrape_bmw_uk(filters: dict) -> list[dict]:
    listings = []

    for api_url in BMW_API_ENDPOINTS:
        log.info("BMW UK: trying API endpoint %s", api_url)
        params = {
            "make": filters["make"],
            "model": filters["model"],
            "priceMax": filters["max_price"],
            "yearMin": filters["min_year"],
            "yearMax": filters["max_year"],
            "mileageMax": filters["max_mileage"],
            "postcode": filters["postcode"].replace(" ", ""),
            "radius": filters["radius_miles"],
            "pageSize": 100,
        }
        try:
            data = get_page(api_url, params=params, json_response=True)
            if data and isinstance(data, (dict, list)):
                listings = _parse_bmw_api_response(data)
                if listings:
                    log.info("BMW UK API: %d listings found via %s", len(listings), api_url)
                    return listings
        except Exception as exc:
            log.debug("BMW UK API attempt failed (%s): %s", api_url, exc)

    log.info("BMW UK: falling back to HTML scraping")
    fallback_url = "https://www.bmw.co.uk/en/topics/find-a-car/used-cars/find-your-bmw.html"
    soup = get_page(fallback_url)
    if soup:
        listings = _parse_bmw_html(soup, filters)
        log.info("BMW UK HTML: %d listings found", len(listings))
    else:
        log.warning("BMW UK: all scraping methods failed — returning empty list")

    return listings


def _parse_bmw_api_response(data) -> list[dict]:
    listings = []
    items = data if isinstance(data, list) else data.get("vehicles") or data.get("results") or data.get("data") or []
    for item in items:
        try:
            listing_id = str(item.get("id") or item.get("vehicleId") or item.get("vin") or "")
            if not listing_id:
                continue
            title = item.get("title") or f"{item.get('make','')} {item.get('model','')} {item.get('derivative','')}".strip()
            price_raw = item.get("price") or item.get("retailPrice") or item.get("priceGBP") or 0
            price = f"£{price_raw:,}" if isinstance(price_raw, (int, float)) else str(price_raw)
            year = str(item.get("year") or item.get("modelYear") or "")
            mileage = str(item.get("mileage") or item.get("odometerReading") or "")
            url_path = item.get("url") or item.get("detailUrl") or ""
            url = url_path if url_path.startswith("http") else "https://www.bmw.co.uk" + url_path
            images = item.get("images") or item.get("media") or []
            image_url = ""
            if images and isinstance(images, list):
                first = images[0]
                image_url = first if isinstance(first, str) else first.get("url") or first.get("src") or ""
            listings.append(
                {
                    "id": listing_id,
                    "title": title,
                    "price": price,
                    "year": year,
                    "mileage": mileage,
                    "url": url,
                    "site": "bmw_uk",
                    "image_url": image_url,
                }
            )
        except Exception as exc:
            log.debug("BMW UK: failed to parse API item: %s", exc)
    return listings


def _parse_bmw_html(soup, filters: dict) -> list[dict]:
    listings = []
    cards = (
        soup.select("div.used-car-tile")
        or soup.select("div[class*='vehicle-card']")
        or soup.select("div[class*='car-tile']")
        or soup.select("article[class*='vehicle']")
    )
    for card in cards:
        try:
            link_el = card.find("a", href=True)
            if not link_el:
                continue
            href = link_el["href"]
            url = href if href.startswith("http") else "https://www.bmw.co.uk" + href
            m = re.search(r"/(\d{6,})", href)
            listing_id = m.group(1) if m else href.rstrip("/").split("/")[-1]
            title_el = card.find("h2") or card.find("h3") or card.find(class_=re.compile(r"title|heading", re.I))
            title = title_el.get_text(strip=True) if title_el else "BMW Vehicle"
            price_el = card.find(class_=re.compile(r"price", re.I))
            price = price_el.get_text(strip=True) if price_el else "N/A"
            year, mileage = _extract_year_mileage(card.get_text(" "))
            img_el = card.find("img")
            image_url = img_el.get("src") or img_el.get("data-src") or "" if img_el else ""
            listings.append(
                {
                    "id": listing_id,
                    "title": title,
                    "price": price,
                    "year": year,
                    "mileage": mileage,
                    "url": url,
                    "site": "bmw_uk",
                    "image_url": image_url,
                }
            )
        except Exception as exc:
            log.debug("BMW UK HTML: failed to parse card: %s", exc)
    return listings


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _extract_year_mileage(text: str) -> tuple[str, str]:
    year = ""
    mileage = ""

    year_m = re.search(r"\b(19[9]\d|20[0-3]\d)\b", text)
    if year_m:
        year = year_m.group(1)

    mileage_m = re.search(r"([\d,]+)\s*(?:miles|mile|mi\b)", text, re.IGNORECASE)
    if mileage_m:
        mileage = mileage_m.group(1).replace(",", "")
    else:
        mileage_m2 = re.search(r"\b(\d{2,3},\d{3})\b", text)
        if mileage_m2:
            mileage = mileage_m2.group(1).replace(",", "")

    return year, mileage


# ---------------------------------------------------------------------------
# Seen listings persistence
# ---------------------------------------------------------------------------

def load_seen() -> dict:
    if SEEN_FILE.exists():
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"autotrader": [], "pistonheads": [], "bmw_uk": []}


def save_seen(seen: dict) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2)


# ---------------------------------------------------------------------------
# Dashboard HTML writer
# ---------------------------------------------------------------------------

def write_dashboard(all_listings: list[dict], new_ids: set, filters: dict) -> None:
    """Write/overwrite dashboard.html with all current listings."""
    make = filters.get("make", "Car")
    model = filters.get("model", "")
    now_str = datetime.now().strftime("%d %b %Y at %H:%M")
    new_count = sum(1 for l in all_listings if l["id"] in new_ids)

    site_label = {
        "autotrader": "AutoTrader",
        "pistonheads": "PistonHeads",
        "bmw_uk": "BMW UK",
    }
    site_color = {
        "autotrader": "#ef6c00",
        "pistonheads": "#1565c0",
        "bmw_uk": "#0066cc",
    }

    cards_html = ""
    # Sort: new listings first, then by site
    sorted_listings = sorted(all_listings, key=lambda l: (0 if l["id"] in new_ids else 1, l["site"]))

    for listing in sorted_listings:
        is_new = listing["id"] in new_ids
        new_badge = '<span style="background:#d32f2f;color:#fff;font-size:11px;font-weight:bold;padding:2px 8px;border-radius:4px;margin-left:8px;vertical-align:middle;text-transform:uppercase;letter-spacing:.5px;">NEW</span>' if is_new else ""
        border = "border:2px solid #d32f2f;" if is_new else "border:1px solid #e0e0e0;"

        img_html = ""
        if listing.get("image_url"):
            img_html = f'<img src="{listing["image_url"]}" alt="" style="width:100%;max-width:100%;height:180px;object-fit:cover;border-radius:6px 6px 0 0;display:block;">'

        color = site_color.get(listing["site"], "#555")
        label = site_label.get(listing["site"], listing["site"])

        mileage_raw = listing.get("mileage", "")
        mileage_display = f"{int(mileage_raw):,} miles" if mileage_raw.isdigit() else (mileage_raw or "N/A")

        cards_html += f"""
        <div style="{border}border-radius:8px;background:#fff;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 1px 4px rgba(0,0,0,.08);">
          {img_html}
          <div style="padding:14px 16px;flex:1;display:flex;flex-direction:column;gap:6px;">
            <div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px;">
              <span style="background:{color};color:#fff;font-size:10px;font-weight:bold;padding:2px 7px;border-radius:4px;text-transform:uppercase;">{label}</span>
              {new_badge}
            </div>
            <div style="font-size:15px;font-weight:600;color:#212121;line-height:1.3;">{listing['title']}</div>
            <div style="font-size:18px;font-weight:700;color:#2e7d32;">{listing['price']}</div>
            <div style="font-size:13px;color:#616161;">
              <span>&#128197; {listing.get('year','N/A') or 'N/A'}</span>
              &nbsp;&bull;&nbsp;
              <span>&#128663; {mileage_display}</span>
            </div>
            <a href="{listing['url']}" target="_blank" style="margin-top:auto;padding:8px 14px;background:#1565c0;color:#fff;text-decoration:none;border-radius:4px;font-size:13px;font-weight:bold;text-align:center;display:block;">View Listing &rarr;</a>
          </div>
        </div>
        """

    summary_text = f"{new_count} new" if new_count else "No new listings"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>BMW M2 Monitor</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #f0f2f5; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; color: #212121; }}
    .header {{ background: #1a237e; color: #fff; padding: 20px 24px; }}
    .header h1 {{ font-size: 20px; font-weight: 700; }}
    .header p {{ font-size: 13px; opacity: .75; margin-top: 4px; }}
    .stats {{ display: flex; gap: 12px; padding: 16px 24px; background: #fff; border-bottom: 1px solid #e0e0e0; flex-wrap: wrap; }}
    .stat {{ font-size: 13px; color: #616161; }}
    .stat strong {{ color: #212121; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; padding: 20px 24px; max-width: 1400px; margin: 0 auto; }}
    .empty {{ text-align: center; padding: 60px 24px; color: #9e9e9e; font-size: 15px; }}
  </style>
</head>
<body>
  <div class="header">
    <h1>&#128663; {make} {model} Monitor</h1>
    <p>Last updated: {now_str} &mdash; {len(all_listings)} total &mdash; <strong style="color:#ef9a9a;">{summary_text}</strong></p>
  </div>
  <div class="stats">
    <div class="stat">Filters: <strong>&ge; {filters.get('min_year','')} &bull; &le; {filters.get('max_mileage',0):,} miles &bull; &le; &pound;{filters.get('max_price',0):,}</strong></div>
    <div class="stat">Area: <strong>{filters.get('postcode','')} &bull; {filters.get('radius_miles','')} mile radius</strong></div>
    <div class="stat">Sources: <strong>AutoTrader &bull; PistonHeads &bull; BMW UK</strong></div>
  </div>
  {"<div class='grid'>" + cards_html + "</div>" if all_listings else "<div class='empty'>No listings found. Run the script again to check for cars.</div>"}
</body>
</html>"""

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    log.info("Dashboard written to %s (%d listings, %d new)", DASHBOARD_FILE, len(all_listings), new_count)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Car Monitor — used car listing scraper")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Treat all scraped listings as new (ignores seen state, does not update seen_listings.json)",
    )
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Car Monitor starting — %s%s", datetime.now().isoformat(timespec="seconds"), " [TEST MODE]" if args.test else "")

    if not CONFIG_FILE.exists():
        log.error("config.json not found at %s", CONFIG_FILE)
        sys.exit(1)

    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)

    filters = config["filters"]
    sites = config.get("sites", {"autotrader": True, "pistonheads": True, "bmw_uk": True})

    all_listings: list[dict] = []

    if sites.get("autotrader", True):
        try:
            all_listings.extend(scrape_autotrader(filters))
        except Exception as exc:
            log.error("AutoTrader scraper crashed: %s", exc)

    if sites.get("pistonheads", True):
        time.sleep(2)
        try:
            all_listings.extend(scrape_pistonheads(filters))
        except Exception as exc:
            log.error("PistonHeads scraper crashed: %s", exc)

    if sites.get("bmw_uk", True):
        time.sleep(2)
        try:
            all_listings.extend(scrape_bmw_uk(filters))
        except Exception as exc:
            log.error("BMW UK scraper crashed: %s", exc)

    log.info("Total listings scraped across all sites: %d", len(all_listings))

    seen = load_seen()

    if args.test:
        new_ids = {l["id"] for l in all_listings}
        log.info("TEST MODE: treating all %d listings as new", len(new_ids))
    else:
        new_ids = set()
        for listing in all_listings:
            site = listing["site"]
            if listing["id"] not in seen.get(site, []):
                new_ids.add(listing["id"])

    log.info("New listings (not previously seen): %d", len(new_ids))

    write_dashboard(all_listings, new_ids, filters)

    if not args.test:
        for listing in all_listings:
            site = listing["site"]
            if site not in seen:
                seen[site] = []
            if listing["id"] not in seen[site]:
                seen[site].append(listing["id"])
        save_seen(seen)
        log.info("seen_listings.json updated.")
    else:
        log.info("TEST MODE: seen_listings.json not updated.")

    log.info(
        "Run complete — %d total, %d new. Dashboard: %s",
        len(all_listings),
        len(new_ids),
        DASHBOARD_FILE,
    )
    log.info("=" * 60)


if __name__ == "__main__":
    main()
