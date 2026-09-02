#!/usr/bin/env node
/**
 * Car Monitor - BMW M2 used car listing scraper and HTML dashboard.
 * Scrapes AutoTrader UK, PistonHeads, and BMW UK for new listings,
 * then writes/updates dashboard.html.
 *
 * Usage:
 *   node car_monitor.js          Normal run
 *   node car_monitor.js --test   Treat all listings as new (for testing)
 */

const axios = require('axios');
const cheerio = require('cheerio');
const fs = require('fs');
const path = require('path');

const SCRIPT_DIR = __dirname;
const CONFIG_FILE = path.join(SCRIPT_DIR, 'config.json');
const SEEN_FILE = path.join(SCRIPT_DIR, 'seen_listings.json');
const DASHBOARD_FILE = path.join(SCRIPT_DIR, 'dashboard.html');

const TEST_MODE = process.argv.includes('--test');

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------
function log(level, ...args) {
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  const line = `${ts} [${level}] ${args.join(' ')}`;
  console.log(line);
  fs.appendFileSync(path.join(SCRIPT_DIR, 'last_run.log'), line + '\n', 'utf8');
}
const info = (...a) => log('INFO', ...a);
const warn = (...a) => log('WARN', ...a);
const error = (...a) => log('ERROR', ...a);
const debug = (...a) => log('DEBUG', ...a);

// ---------------------------------------------------------------------------
// HTTP client
// ---------------------------------------------------------------------------
const http = axios.create({
  timeout: 20000,
  headers: {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-GB,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Cache-Control': 'max-age=0',
  },
  decompress: true,
});

async function getPage(url, params = {}, jsonResponse = false) {
  try {
    const resp = await http.get(url, { params, responseType: jsonResponse ? 'json' : 'text' });
    if (jsonResponse) return resp.data;
    return cheerio.load(resp.data);
  } catch (err) {
    error(`Request failed for ${url}: ${err.message}`);
    return null;
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function extractYearMileage(text) {
  let year = '';
  let mileage = '';

  const yearM = text.match(/\b(199\d|20[0-3]\d)\b/);
  if (yearM) year = yearM[1];

  const mileM = text.match(/([\d,]+)\s*(?:miles|mile|mi\b)/i);
  if (mileM) {
    mileage = mileM[1].replace(/,/g, '');
  } else {
    const mileM2 = text.match(/\b(\d{2,3},\d{3})\b/);
    if (mileM2) mileage = mileM2[1].replace(/,/g, '');
  }

  return { year, mileage };
}

// ---------------------------------------------------------------------------
// AutoTrader scraper
// ---------------------------------------------------------------------------
async function scrapeAutotrader(filters) {
  const listings = [];
  const postcode = filters.postcode.replace(/\s/g, '');

  const baseParams = {
    make: filters.make,
    model: filters.model,
    'price-to': filters.max_price,
    'year-from': filters.min_year,
    'year-to': filters.max_year,
    postcode,
    radius: filters.radius_miles,
    'include-delivery-option': 'on',
    'advertising-location': 'at_cars',
  };

  const baseUrl = 'https://www.autotrader.co.uk/car-search';
  info('Scraping AutoTrader:', baseUrl);

  for (let page = 1; page <= 5; page++) {
    const $ = await getPage(baseUrl, { ...baseParams, page });
    if (!$ ) break;

    let articles = $('article[data-standout-type]').toArray();
    if (!articles.length) articles = $('li.search-page__result article').toArray();
    if (!articles.length) articles = $('article').toArray();

    if (!articles.length) {
      warn(`AutoTrader: no listing elements on page ${page} (markup may have changed)`);
      break;
    }

    const pageListings = [];
    for (const art of articles) {
      const listing = parseAutotraderArticle($, art);
      if (listing) pageListings.push(listing);
    }

    if (!pageListings.length) break;
    listings.push(...pageListings);
    info(`AutoTrader page ${page}: ${pageListings.length} listings`);

    const hasNext = $("a[data-gui='pagination-next']").length || $('a.next-page').length;
    if (!hasNext) break;

    await sleep(1500);
  }

  info(`AutoTrader: total ${listings.length} listings found`);
  return listings;
}

function parseAutotraderArticle($, art) {
  const el = $(art);
  let listingId = el.attr('id') || el.attr('data-advert-id') || '';

  const linkEl = el.find('a[href]').first();
  let url = '';
  if (linkEl.length) {
    const href = linkEl.attr('href');
    url = href.startsWith('http') ? href : 'https://www.autotrader.co.uk' + href;
    if (!listingId) {
      const m = href.match(/\/(\d{8,})/);
      if (m) listingId = m[1];
    }
  }
  if (!listingId) return null;

  const titleEl = el.find('h2, h3, [data-gui="advert-title"], .listing-title').first();
  const title = titleEl.text().trim() || 'Unknown';

  const priceEl = el.find('[data-gui="advert-price"], .vehicle-price').first()
    || el.find('[class*="price"]').first();
  const price = el.find('[data-gui="advert-price"]').first().text().trim()
    || el.find('.vehicle-price').first().text().trim()
    || el.find('[class*="price"]').first().text().trim()
    || 'N/A';

  const { year, mileage } = extractYearMileage(el.text());

  const imgEl = el.find('img').first();
  const imageUrl = imgEl.attr('src') || imgEl.attr('data-src') || '';

  return { id: listingId, title, price, year, mileage, url, site: 'autotrader', imageUrl };
}

// ---------------------------------------------------------------------------
// PistonHeads scraper
// ---------------------------------------------------------------------------
async function scrapePistonheads(filters) {
  const listings = [];
  const postcode = filters.postcode.replace(/\s/g, '');
  const baseUrl = 'https://www.pistonheads.com/classifieds/used-cars';
  info('Scraping PistonHeads:', baseUrl);

  for (let page = 1; page <= 5; page++) {
    const $ = await getPage(baseUrl, {
      make: filters.make,
      model: filters.model,
      priceTo: filters.max_price,
      yearFrom: filters.min_year,
      yearTo: filters.max_year,
      within: filters.radius_miles,
      postcode,
      page,
    });
    if (!$) break;

    let cards = $('div.listing-masthead, li.listing, article.listing-card').toArray();
    if (!cards.length) cards = $('div[class*="listing"]').toArray();

    if (!cards.length) {
      warn(`PistonHeads: no listing cards on page ${page}`);
      break;
    }

    const pageListings = [];
    for (const card of cards) {
      const listing = parsePistonheadsCard($, card);
      if (listing) pageListings.push(listing);
    }

    if (!pageListings.length) break;
    listings.push(...pageListings);
    info(`PistonHeads page ${page}: ${pageListings.length} listings`);

    const hasNext = $("a[rel='next'], a.next").length;
    if (!hasNext) break;

    await sleep(1500);
  }

  info(`PistonHeads: total ${listings.length} listings found`);
  return listings;
}

function parsePistonheadsCard($, card) {
  const el = $(card);
  const linkEl = el.find('a[href]').first();
  if (!linkEl.length) return null;

  const href = linkEl.attr('href');
  const url = href.startsWith('http') ? href : 'https://www.pistonheads.com' + href;

  const m = href.match(/\/(\d+)(?:[/?#]|$)/);
  const listingId = m ? m[1] : href.split('/').pop();
  if (!listingId) return null;

  const titleEl = el.find('h2, h3, [class*="title"]').first();
  const title = titleEl.text().trim() || linkEl.text().trim() || 'Unknown';

  const price = el.find('[class*="price"]').first().text().trim() || 'N/A';

  const { year, mileage } = extractYearMileage(el.text());

  const imgEl = el.find('img').first();
  const imageUrl = imgEl.attr('src') || imgEl.attr('data-src') || imgEl.attr('data-lazy-src') || '';

  return { id: listingId, title, price, year, mileage, url, site: 'pistonheads', imageUrl };
}

// ---------------------------------------------------------------------------
// BMW UK scraper
// ---------------------------------------------------------------------------
const BMW_API_ENDPOINTS = [
  'https://www.bmw.co.uk/api/v1/used-cars/search',
  'https://www.bmw.co.uk/en/topics/find-a-car/used-cars/used-car-search.api.json',
];

async function scrapeBmwUk(filters) {
  const postcode = filters.postcode.replace(/\s/g, '');
  const params = {
    make: filters.make,
    model: filters.model,
    priceMax: filters.max_price,
    yearMin: filters.min_year,
    yearMax: filters.max_year,
    mileageMax: filters.max_mileage,
    postcode,
    radius: filters.radius_miles,
    pageSize: 100,
  };

  for (const apiUrl of BMW_API_ENDPOINTS) {
    info('BMW UK: trying API endpoint', apiUrl);
    try {
      const data = await getPage(apiUrl, params, true);
      if (data) {
        const listings = parseBmwApiResponse(data);
        if (listings.length) {
          info(`BMW UK API: ${listings.length} listings found via ${apiUrl}`);
          return listings;
        }
      }
    } catch (err) {
      debug(`BMW UK API attempt failed (${apiUrl}): ${err.message}`);
    }
  }

  info('BMW UK: falling back to HTML scraping');
  const fallbackUrl = 'https://www.bmw.co.uk/en/topics/find-a-car/used-cars/find-your-bmw.html';
  const $ = await getPage(fallbackUrl);
  if ($) {
    const listings = parseBmwHtml($);
    info(`BMW UK HTML: ${listings.length} listings found`);
    return listings;
  }

  warn('BMW UK: all scraping methods failed');
  return [];
}

function parseBmwApiResponse(data) {
  const items = Array.isArray(data) ? data : (data.vehicles || data.results || data.data || []);
  return items.map(item => {
    try {
      const listingId = String(item.id || item.vehicleId || item.vin || '');
      if (!listingId) return null;
      const title = item.title || `${item.make || ''} ${item.model || ''} ${item.derivative || ''}`.trim();
      const priceRaw = item.price || item.retailPrice || item.priceGBP || 0;
      const price = typeof priceRaw === 'number' ? `£${priceRaw.toLocaleString('en-GB')}` : String(priceRaw);
      const year = String(item.year || item.modelYear || '');
      const mileage = String(item.mileage || item.odometerReading || '');
      const urlPath = item.url || item.detailUrl || '';
      const url = urlPath.startsWith('http') ? urlPath : 'https://www.bmw.co.uk' + urlPath;
      const images = item.images || item.media || [];
      let imageUrl = '';
      if (images.length) {
        const first = images[0];
        imageUrl = typeof first === 'string' ? first : (first.url || first.src || '');
      }
      return { id: listingId, title, price, year, mileage, url, site: 'bmw_uk', imageUrl };
    } catch { return null; }
  }).filter(Boolean);
}

function parseBmwHtml($) {
  const cards = $('div.used-car-tile, div[class*="vehicle-card"], div[class*="car-tile"], article[class*="vehicle"]').toArray();
  return cards.map(card => {
    try {
      const el = $(card);
      const linkEl = el.find('a[href]').first();
      if (!linkEl.length) return null;
      const href = linkEl.attr('href');
      const url = href.startsWith('http') ? href : 'https://www.bmw.co.uk' + href;
      const m = href.match(/\/(\d{6,})/);
      const listingId = m ? m[1] : href.replace(/\/$/, '').split('/').pop();
      const title = el.find('h2, h3, [class*="title"]').first().text().trim() || 'BMW Vehicle';
      const price = el.find('[class*="price"]').first().text().trim() || 'N/A';
      const { year, mileage } = extractYearMileage(el.text());
      const imgEl = el.find('img').first();
      const imageUrl = imgEl.attr('src') || imgEl.attr('data-src') || '';
      return { id: listingId, title, price, year, mileage, url, site: 'bmw_uk', imageUrl };
    } catch { return null; }
  }).filter(Boolean);
}

// ---------------------------------------------------------------------------
// Seen listings persistence
// ---------------------------------------------------------------------------
function loadSeen() {
  if (fs.existsSync(SEEN_FILE)) {
    return JSON.parse(fs.readFileSync(SEEN_FILE, 'utf8'));
  }
  return { autotrader: [], pistonheads: [], bmw_uk: [] };
}

function saveSeen(seen) {
  fs.writeFileSync(SEEN_FILE, JSON.stringify(seen, null, 2), 'utf8');
}

// ---------------------------------------------------------------------------
// Dashboard HTML writer
// ---------------------------------------------------------------------------
function writeDashboard(allListings, newIds, filters) {
  const now = new Date().toLocaleString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  const newCount = allListings.filter(l => newIds.has(l.id)).length;

  const siteLabel = { autotrader: 'AutoTrader', pistonheads: 'PistonHeads', bmw_uk: 'BMW UK' };
  const siteColor = { autotrader: '#ef6c00', pistonheads: '#1565c0', bmw_uk: '#0066cc' };

  const sorted = [...allListings].sort((a, b) => {
    const aNew = newIds.has(a.id) ? 0 : 1;
    const bNew = newIds.has(b.id) ? 0 : 1;
    return aNew - bNew || a.site.localeCompare(b.site);
  });

  const cardsHtml = sorted.map(listing => {
    const isNew = newIds.has(listing.id);
    const newBadge = isNew
      ? `<span style="background:#d32f2f;color:#fff;font-size:11px;font-weight:bold;padding:2px 8px;border-radius:4px;text-transform:uppercase;letter-spacing:.5px;">NEW</span>`
      : '';
    const border = isNew ? 'border:2px solid #d32f2f;' : 'border:1px solid #e0e0e0;';
    const imgHtml = listing.imageUrl
      ? `<img src="${listing.imageUrl}" alt="" style="width:100%;height:180px;object-fit:cover;border-radius:6px 6px 0 0;display:block;">`
      : '';
    const color = siteColor[listing.site] || '#555';
    const label = siteLabel[listing.site] || listing.site;
    const mileageDisplay = /^\d+$/.test(listing.mileage)
      ? `${parseInt(listing.mileage).toLocaleString('en-GB')} miles`
      : (listing.mileage || 'N/A');

    return `
    <div style="${border}border-radius:8px;background:#fff;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 1px 4px rgba(0,0,0,.08);">
      ${imgHtml}
      <div style="padding:14px 16px;flex:1;display:flex;flex-direction:column;gap:6px;">
        <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;">
          <span style="background:${color};color:#fff;font-size:10px;font-weight:bold;padding:2px 7px;border-radius:4px;text-transform:uppercase;">${label}</span>
          ${newBadge}
        </div>
        <div style="font-size:15px;font-weight:600;color:#212121;line-height:1.3;">${listing.title}</div>
        <div style="font-size:18px;font-weight:700;color:#2e7d32;">${listing.price}</div>
        <div style="font-size:13px;color:#616161;">
          &#128197; ${listing.year || 'N/A'} &nbsp;&bull;&nbsp; &#128663; ${mileageDisplay}
        </div>
        <a href="${listing.url}" target="_blank" style="margin-top:auto;padding:8px 14px;background:#1565c0;color:#fff;text-decoration:none;border-radius:4px;font-size:13px;font-weight:bold;text-align:center;display:block;">View Listing &rarr;</a>
      </div>
    </div>`;
  }).join('\n');

  const maxMileageFmt = (filters.max_mileage || 0).toLocaleString('en-GB');
  const maxPriceFmt = (filters.max_price || 0).toLocaleString('en-GB');
  const summaryText = newCount ? `${newCount} new` : 'No new listings';

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>BMW M2 Monitor</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #f0f2f5; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; color: #212121; }
    .header { background: #1a237e; color: #fff; padding: 20px 24px; }
    .header h1 { font-size: 20px; font-weight: 700; }
    .header p { font-size: 13px; opacity: .75; margin-top: 4px; }
    .stats { display: flex; gap: 16px; padding: 14px 24px; background: #fff; border-bottom: 1px solid #e0e0e0; flex-wrap: wrap; }
    .stat { font-size: 13px; color: #616161; }
    .stat strong { color: #212121; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; padding: 20px 24px; max-width: 1400px; margin: 0 auto; }
    .empty { text-align: center; padding: 60px 24px; color: #9e9e9e; font-size: 15px; }
  </style>
</head>
<body>
  <div class="header">
    <h1>&#128663; BMW M2 Monitor</h1>
    <p>Last updated: ${now} &mdash; ${allListings.length} total &mdash; <strong style="color:#ef9a9a;">${summaryText}</strong></p>
  </div>
  <div class="stats">
    <div class="stat">Filters: <strong>&ge; ${filters.min_year} &bull; &le; ${maxMileageFmt} miles &bull; &le; &pound;${maxPriceFmt}</strong></div>
    <div class="stat">Area: <strong>${filters.postcode} &bull; ${filters.radius_miles} mile radius</strong></div>
    <div class="stat">Sources: <strong>AutoTrader &bull; PistonHeads &bull; BMW UK</strong></div>
  </div>
  ${allListings.length
    ? `<div class="grid">${cardsHtml}</div>`
    : `<div class="empty">No listings found matching your filters.</div>`
  }
</body>
</html>`;

  fs.writeFileSync(DASHBOARD_FILE, html, 'utf8');
  info(`Dashboard written to ${DASHBOARD_FILE} (${allListings.length} listings, ${newCount} new)`);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
async function main() {
  // Clear log for this run
  fs.writeFileSync(path.join(SCRIPT_DIR, 'last_run.log'), '', 'utf8');

  info('='.repeat(60));
  info(`Car Monitor starting — ${new Date().toISOString()}${TEST_MODE ? ' [TEST MODE]' : ''}`);

  if (!fs.existsSync(CONFIG_FILE)) {
    error('config.json not found at', CONFIG_FILE);
    process.exit(1);
  }

  const config = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
  const filters = config.filters;
  const sites = config.sites || { autotrader: true, pistonheads: true, bmw_uk: true };

  const allListings = [];

  if (sites.autotrader !== false) {
    try { allListings.push(...await scrapeAutotrader(filters)); }
    catch (err) { error('AutoTrader scraper crashed:', err.message); }
  }

  if (sites.pistonheads !== false) {
    await sleep(2000);
    try { allListings.push(...await scrapePistonheads(filters)); }
    catch (err) { error('PistonHeads scraper crashed:', err.message); }
  }

  if (sites.bmw_uk !== false) {
    await sleep(2000);
    try { allListings.push(...await scrapeBmwUk(filters)); }
    catch (err) { error('BMW UK scraper crashed:', err.message); }
  }

  info(`Total listings scraped: ${allListings.length}`);

  const seen = loadSeen();

  let newIds;
  if (TEST_MODE) {
    newIds = new Set(allListings.map(l => l.id));
    info(`TEST MODE: treating all ${newIds.size} listings as new`);
  } else {
    newIds = new Set();
    for (const listing of allListings) {
      const seenForSite = seen[listing.site] || [];
      if (!seenForSite.includes(listing.id)) {
        newIds.add(listing.id);
      }
    }
  }

  info(`New listings (not previously seen): ${newIds.size}`);

  writeDashboard(allListings, newIds, filters);

  if (!TEST_MODE) {
    for (const listing of allListings) {
      if (!seen[listing.site]) seen[listing.site] = [];
      if (!seen[listing.site].includes(listing.id)) {
        seen[listing.site].push(listing.id);
      }
    }
    saveSeen(seen);
    info('seen_listings.json updated.');
  } else {
    info('TEST MODE: seen_listings.json not updated.');
  }

  info(`Run complete — ${allListings.length} total, ${newIds.size} new.`);
  info('='.repeat(60));

  // Open dashboard in default browser (Windows)
  if (process.platform === 'win32') {
    const { exec } = require('child_process');
    exec(`start "" "${DASHBOARD_FILE}"`);
  }
}

main().catch(err => {
  error('Fatal error:', err.message);
  process.exit(1);
});
