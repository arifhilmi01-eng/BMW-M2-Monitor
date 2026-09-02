#Requires -Version 5.1
<#
.SYNOPSIS
    BMW M2 used car monitor - scrapes AutoTrader, PistonHeads and BMW UK,
    then writes dashboard.html which you open in your browser.

.USAGE
    Normal run:   .\car_monitor.ps1
    Test mode:    .\car_monitor.ps1 -Test
#>
param([switch]$Test)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile  = Join-Path $ScriptDir 'config.json'
$SeenFile    = Join-Path $ScriptDir 'seen_listings.json'
$DashFile    = Join-Path $ScriptDir 'dashboard.html'
$LogFile     = Join-Path $ScriptDir 'last_run.log'

'' | Set-Content $LogFile   # clear log each run

function Log($level, $msg) {
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $level, $msg
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}
function Info($m)  { Log 'INFO'  $m }
function Warn($m)  { Log 'WARN'  $m }
function Err($m)   { Log 'ERROR' $m }

# ---------------------------------------------------------------------------
# HTTP helper — returns raw HTML string or $null on failure
# ---------------------------------------------------------------------------
$BaseHeaders = @{
    'User-Agent'      = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    'Accept'          = 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8'
    'Accept-Language' = 'en-GB,en;q=0.9'
    'DNT'             = '1'
    'Cache-Control'   = 'max-age=0'
}

function Get-Html($url, $params = @{}) {
    try {
        $uri = $url
        if ($params.Count -gt 0) {
            $qs = ($params.GetEnumerator() | ForEach-Object { "$([Uri]::EscapeDataString($_.Key))=$([Uri]::EscapeDataString([string]$_.Value))" }) -join '&'
            $uri = "${url}?${qs}"
        }
        $resp = Invoke-WebRequest -Uri $uri -Headers $BaseHeaders -UseBasicParsing -TimeoutSec 20
        return $resp.Content
    } catch {
        Err "Request failed for ${url}: $_"
        return $null
    }
}

# ---------------------------------------------------------------------------
# Year / mileage extractor from a text blob
# ---------------------------------------------------------------------------
function Get-YearMileage($text) {
    $year    = ''
    $mileage = ''
    if ($text -match '\b(199\d|20[0-3]\d)\b')               { $year    = $Matches[1] }
    if ($text -match '([\d,]+)\s*(?:miles|mile|mi)\b')       { $mileage = $Matches[1] -replace ',','' }
    elseif ($text -match '\b(\d{2,3},\d{3})\b')             { $mileage = $Matches[1] -replace ',','' }
    return @{ year = $year; mileage = $mileage }
}

# ---------------------------------------------------------------------------
# Extract all JSON-LD blocks from a page
# ---------------------------------------------------------------------------
function Get-JsonLd($html) {
    $results = @()
    $pattern = '(?s)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
    foreach ($m in [regex]::Matches($html, $pattern)) {
        try { $results += $m.Groups[1].Value | ConvertFrom-Json } catch {}
    }
    return $results
}

# ---------------------------------------------------------------------------
# AutoTrader
# ---------------------------------------------------------------------------
function Invoke-AutotraderScrape($filters) {
    $listings = @()
    $postcode = $filters.postcode -replace '\s',''
    $baseUrl  = 'https://www.autotrader.co.uk/car-search'
    Info "Scraping AutoTrader: $baseUrl"

    for ($page = 1; $page -le 5; $page++) {
        $params = @{
            make                       = $filters.make
            model                      = $filters.model
            'price-to'                 = $filters.max_price
            'year-from'                = $filters.min_year
            'year-to'                  = $filters.max_year
            postcode                   = $postcode
            radius                     = $filters.radius_miles
            'include-delivery-option'  = 'on'
            'advertising-location'     = 'at_cars'
            page                       = $page
        }
        $html = Get-Html $baseUrl $params
        if (-not $html) { break }

        # --- Try to pull the embedded JSON state first (most reliable) ---
        $pageListings = @()

        # AutoTrader embeds listing data as JSON in the page; try a few patterns
        $jsonPatterns = @(
            '(?s)window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});\s*</script>',
            '(?s)"advertCard"\s*:\s*(\[.*?\])',
            '(?s)"listings"\s*:\s*(\[.*?\])'
        )
        foreach ($pat in $jsonPatterns) {
            $m = [regex]::Match($html, $pat)
            if ($m.Success) {
                try {
                    $data = $m.Groups[1].Value | ConvertFrom-Json
                    # Walk the object tree looking for listing arrays
                    $items = $null
                    if ($data -is [array])          { $items = $data }
                    elseif ($data.listings)         { $items = $data.listings }
                    elseif ($data.search.listings)  { $items = $data.search.listings }
                    if ($items) {
                        foreach ($item in $items) {
                            $id = [string]($item.id -or $item.advertId -or $item.adId -or '')
                            if (-not $id) { continue }
                            $urlPath = [string]($item.url -or $item.link -or '')
                            $url = if ($urlPath -match '^http') { $urlPath } else { "https://www.autotrader.co.uk$urlPath" }
                            $ym = Get-YearMileage ([string]$item)
                            $pageListings += [PSCustomObject]@{
                                id       = $id
                                title    = [string]($item.title -or $item.name -or 'Unknown')
                                price    = [string]($item.price -or $item.advertisedPrice -or 'N/A')
                                year     = $ym.year
                                mileage  = $ym.mileage
                                url      = $url
                                site     = 'autotrader'
                                imageUrl = [string]($item.imageUrl -or $item.image -or '')
                            }
                        }
                    }
                } catch {}
                if ($pageListings.Count -gt 0) { break }
            }
        }

        # --- JSON-LD fallback ---
        if ($pageListings.Count -eq 0) {
            foreach ($obj in (Get-JsonLd $html)) {
                $type = [string]($obj.'@type')
                if ($type -notin @('Car','Vehicle','Product')) { continue }
                $id = [string]($obj.productID -or $obj.identifier -or '')
                if (-not $id) { continue }
                $offers = $obj.offers
                $price  = if ($offers) { [string]$offers.price } else { 'N/A' }
                $url    = if ($offers) { [string]($offers.url -or $obj.url) } else { [string]$obj.url }
                $ym     = Get-YearMileage ([string]$obj)
                $pageListings += [PSCustomObject]@{
                    id       = $id
                    title    = [string]($obj.name -or 'Unknown')
                    price    = if ($price -match '^\d+$') { "£$price" } else { $price }
                    year     = [string]($obj.vehicleModelDate -or $ym.year)
                    mileage  = [string]($obj.mileageFromOdometer.value -or $ym.mileage)
                    url      = $url
                    site     = 'autotrader'
                    imageUrl = [string]($obj.image -or '')
                }
            }
        }

        # --- Regex HTML fallback ---
        if ($pageListings.Count -eq 0) {
            $artPattern = '(?s)<article[^>]+data-advert-id="([^"]+)"[^>]*>(.*?)</article>'
            foreach ($m in [regex]::Matches($html, $artPattern)) {
                $id      = $m.Groups[1].Value
                $artHtml = $m.Groups[2].Value
                $linkM   = [regex]::Match($artHtml, 'href="(/car-details/[^"]+)"')
                $url     = if ($linkM.Success) { "https://www.autotrader.co.uk$($linkM.Groups[1].Value)" } else { '' }
                $titleM  = [regex]::Match($artHtml, '<h[23][^>]*>\s*(.*?)\s*</h[23]>')
                $title   = if ($titleM.Success) { [System.Web.HttpUtility]::HtmlDecode($titleM.Groups[1].Value -replace '<[^>]+>','') } else { 'Unknown' }
                $priceM  = [regex]::Match($artHtml, '£[\d,]+')
                $price   = if ($priceM.Success) { $priceM.Value } else { 'N/A' }
                $imgM    = [regex]::Match($artHtml, '<img[^>]+src="([^"]+)"')
                $imgUrl  = if ($imgM.Success) { $imgM.Groups[1].Value } else { '' }
                $ym      = Get-YearMileage ($artHtml -replace '<[^>]+>',' ')
                $pageListings += [PSCustomObject]@{
                    id = $id; title = $title; price = $price
                    year = $ym.year; mileage = $ym.mileage
                    url = $url; site = 'autotrader'; imageUrl = $imgUrl
                }
            }
        }

        if ($pageListings.Count -eq 0) {
            Warn "AutoTrader: no listings on page $page (markup may have changed)"
            break
        }
        $listings += $pageListings
        Info "AutoTrader page $page: $($pageListings.Count) listings"

        if ($html -notmatch 'data-gui="pagination-next"' -and $html -notmatch 'class="next-page"') { break }
        Start-Sleep -Milliseconds 1500
    }

    Info "AutoTrader: total $($listings.Count) listings"
    return $listings
}

# ---------------------------------------------------------------------------
# PistonHeads
# ---------------------------------------------------------------------------
function Invoke-PistonheadsScrape($filters) {
    $listings = @()
    $postcode = $filters.postcode -replace '\s',''
    $baseUrl  = 'https://www.pistonheads.com/classifieds/used-cars'
    Info "Scraping PistonHeads: $baseUrl"

    for ($page = 1; $page -le 5; $page++) {
        $params = @{
            make     = $filters.make
            model    = $filters.model
            priceTo  = $filters.max_price
            yearFrom = $filters.min_year
            yearTo   = $filters.max_year
            within   = $filters.radius_miles
            postcode = $postcode
            page     = $page
        }
        $html = Get-Html $baseUrl $params
        if (-not $html) { break }

        $pageListings = @()

        # PH embeds data as JSON in script tags
        foreach ($obj in (Get-JsonLd $html)) {
            $type = [string]($obj.'@type')
            if ($type -notin @('Car','Vehicle','Product')) { continue }
            $id = [string]($obj.productID -or $obj.identifier -or '')
            if (-not $id) { continue }
            $offers = $obj.offers
            $price  = if ($offers) { [string]$offers.price } else { 'N/A' }
            $url    = if ($offers) { [string]($offers.url -or $obj.url) } else { [string]$obj.url }
            $ym     = Get-YearMileage ([string]$obj)
            $pageListings += [PSCustomObject]@{
                id       = $id
                title    = [string]($obj.name -or 'Unknown')
                price    = if ($price -match '^\d+$') { "£$price" } else { $price }
                year     = [string]($obj.vehicleModelDate -or $ym.year)
                mileage  = [string]($obj.mileageFromOdometer.value -or $ym.mileage)
                url      = $url
                site     = 'pistonheads'
                imageUrl = [string]($obj.image -or '')
            }
        }

        # Regex HTML fallback
        if ($pageListings.Count -eq 0) {
            $pattern = '(?s)<(?:li|article)[^>]+class="[^"]*listing[^"]*"[^>]*>(.*?)</(?:li|article)>'
            foreach ($m in [regex]::Matches($html, $pattern)) {
                $cardHtml = $m.Groups[1].Value
                $linkM    = [regex]::Match($cardHtml, 'href="(/classifieds/[^"]+)"')
                if (-not $linkM.Success) { continue }
                $href     = $linkM.Groups[1].Value
                $url      = "https://www.pistonheads.com$href"
                $idM      = [regex]::Match($href, '/(\d+)(?:[/?#]|$)')
                $id       = if ($idM.Success) { $idM.Groups[1].Value } else { $href.Split('/')[-1] }
                $titleM   = [regex]::Match($cardHtml, '<h[23][^>]*>(.*?)</h[23]>')
                $title    = if ($titleM.Success) { $titleM.Groups[1].Value -replace '<[^>]+>','' } else { 'Unknown' }
                $priceM   = [regex]::Match($cardHtml, '£[\d,]+')
                $price    = if ($priceM.Success) { $priceM.Value } else { 'N/A' }
                $imgM     = [regex]::Match($cardHtml, 'src="(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"')
                $imgUrl   = if ($imgM.Success) { $imgM.Groups[1].Value } else { '' }
                $ym       = Get-YearMileage ($cardHtml -replace '<[^>]+>',' ')
                $pageListings += [PSCustomObject]@{
                    id = $id; title = $title; price = $price
                    year = $ym.year; mileage = $ym.mileage
                    url = $url; site = 'pistonheads'; imageUrl = $imgUrl
                }
            }
        }

        if ($pageListings.Count -eq 0) {
            Warn "PistonHeads: no listings on page $page"
            break
        }
        $listings += $pageListings
        Info "PistonHeads page $page: $($pageListings.Count) listings"

        if ($html -notmatch 'rel="next"' -and $html -notmatch 'class="next"') { break }
        Start-Sleep -Milliseconds 1500
    }

    Info "PistonHeads: total $($listings.Count) listings"
    return $listings
}

# ---------------------------------------------------------------------------
# BMW UK
# ---------------------------------------------------------------------------
function Invoke-BmwUkScrape($filters) {
    $postcode = $filters.postcode -replace '\s',''
    $apiEndpoints = @(
        'https://www.bmw.co.uk/api/v1/used-cars/search',
        'https://www.bmw.co.uk/en/topics/find-a-car/used-cars/used-car-search.api.json'
    )
    $params = @{
        make       = $filters.make
        model      = $filters.model
        priceMax   = $filters.max_price
        yearMin    = $filters.min_year
        yearMax    = $filters.max_year
        mileageMax = $filters.max_mileage
        postcode   = $postcode
        radius     = $filters.radius_miles
        pageSize   = 100
    }

    foreach ($apiUrl in $apiEndpoints) {
        Info "BMW UK: trying $apiUrl"
        try {
            $qs   = ($params.GetEnumerator() | ForEach-Object { "$([Uri]::EscapeDataString($_.Key))=$([Uri]::EscapeDataString([string]$_.Value))" }) -join '&'
            $resp = Invoke-RestMethod -Uri "${apiUrl}?${qs}" -Headers $BaseHeaders -TimeoutSec 20
            $items = if ($resp -is [array]) { $resp } else { $resp.vehicles -or $resp.results -or $resp.data -or @() }
            if ($items.Count -gt 0) {
                $listings = $items | ForEach-Object {
                    $id = [string]($_.id -or $_.vehicleId -or $_.vin -or '')
                    if (-not $id) { return }
                    $priceRaw = $_.price -or $_.retailPrice -or $_.priceGBP -or 0
                    $price    = if ($priceRaw -match '^\d+$') { "£$([int]$priceRaw.ToString('N0'))" } else { [string]$priceRaw }
                    $urlPath  = [string]($_.url -or $_.detailUrl -or '')
                    $url      = if ($urlPath -match '^http') { $urlPath } else { "https://www.bmw.co.uk$urlPath" }
                    $images   = $_.images -or $_.media -or @()
                    $imgUrl   = if ($images.Count) { if ($images[0] -is [string]) { $images[0] } else { [string]($images[0].url -or $images[0].src -or '') } } else { '' }
                    [PSCustomObject]@{
                        id       = $id
                        title    = [string]($_.title -or "$($_.make) $($_.model) $($_.derivative)".Trim())
                        price    = $price
                        year     = [string]($_.year -or $_.modelYear -or '')
                        mileage  = [string]($_.mileage -or $_.odometerReading -or '')
                        url      = $url
                        site     = 'bmw_uk'
                        imageUrl = $imgUrl
                    }
                } | Where-Object { $_ }
                if ($listings.Count -gt 0) {
                    Info "BMW UK API: $($listings.Count) listings"
                    return $listings
                }
            }
        } catch { }
    }

    # HTML fallback
    Info "BMW UK: falling back to HTML scraping"
    $html = Get-Html 'https://www.bmw.co.uk/en/topics/find-a-car/used-cars/find-your-bmw.html'
    if (-not $html) { Warn "BMW UK: all methods failed"; return @() }

    $listings = @()
    $cardPat = '(?s)<div[^>]+class="[^"]*(?:used-car-tile|vehicle-card|car-tile)[^"]*"[^>]*>(.*?)</div>\s*</div>'
    foreach ($m in [regex]::Matches($html, $cardPat)) {
        $cardHtml = $m.Groups[1].Value
        $linkM    = [regex]::Match($cardHtml, 'href="([^"]+)"')
        if (-not $linkM.Success) { continue }
        $href    = $linkM.Groups[1].Value
        $url     = if ($href -match '^http') { $href } else { "https://www.bmw.co.uk$href" }
        $idM     = [regex]::Match($href, '/(\d{6,})')
        $id      = if ($idM.Success) { $idM.Groups[1].Value } else { $href.TrimEnd('/').Split('/')[-1] }
        $titleM  = [regex]::Match($cardHtml, '<h[23][^>]*>(.*?)</h[23]>')
        $title   = if ($titleM.Success) { $titleM.Groups[1].Value -replace '<[^>]+>','' } else { 'BMW Vehicle' }
        $priceM  = [regex]::Match($cardHtml, '£[\d,]+')
        $price   = if ($priceM.Success) { $priceM.Value } else { 'N/A' }
        $imgM    = [regex]::Match($cardHtml, 'src="(https?://[^"]+)"')
        $imgUrl  = if ($imgM.Success) { $imgM.Groups[1].Value } else { '' }
        $ym      = Get-YearMileage ($cardHtml -replace '<[^>]+>',' ')
        $listings += [PSCustomObject]@{
            id = $id; title = $title; price = $price
            year = $ym.year; mileage = $ym.mileage
            url = $url; site = 'bmw_uk'; imageUrl = $imgUrl
        }
    }
    Info "BMW UK HTML: $($listings.Count) listings"
    return $listings
}

# ---------------------------------------------------------------------------
# Seen listings
# ---------------------------------------------------------------------------
function Load-Seen {
    if (Test-Path $SeenFile) {
        return Get-Content $SeenFile -Raw | ConvertFrom-Json
    }
    return [PSCustomObject]@{ autotrader = @(); pistonheads = @(); bmw_uk = @() }
}

function Save-Seen($seen) {
    $seen | ConvertTo-Json -Depth 5 | Set-Content $SeenFile -Encoding UTF8
}

# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------
function Write-Dashboard($allListings, $newIds, $filters) {
    $now      = Get-Date -Format 'dd MMM yyyy HH:mm'
    $newCount = ($allListings | Where-Object { $newIds -contains $_.id }).Count

    $siteLabel = @{ autotrader = 'AutoTrader'; pistonheads = 'PistonHeads'; bmw_uk = 'BMW UK' }
    $siteColor = @{ autotrader = '#ef6c00';    pistonheads = '#1565c0';     bmw_uk  = '#0066cc' }

    $sorted = $allListings | Sort-Object { if ($newIds -contains $_.id) { 0 } else { 1 } }, site

    $cardsHtml = foreach ($l in $sorted) {
        $isNew     = $newIds -contains $l.id
        $newBadge  = if ($isNew) { '<span style="background:#d32f2f;color:#fff;font-size:11px;font-weight:bold;padding:2px 8px;border-radius:4px;text-transform:uppercase;">NEW</span>' } else { '' }
        $border    = if ($isNew) { 'border:2px solid #d32f2f;' } else { 'border:1px solid #e0e0e0;' }
        $imgHtml   = if ($l.imageUrl) { "<img src=`"$($l.imageUrl)`" alt=`"`" style=`"width:100%;height:180px;object-fit:cover;border-radius:6px 6px 0 0;display:block;`">" } else { '' }
        $color     = $siteColor[$l.site]
        $label     = $siteLabel[$l.site]
        $mileDisp  = if ($l.mileage -match '^\d+$') { "{0:N0} miles" -f [int]$l.mileage } else { if ($l.mileage) { $l.mileage } else { 'N/A' } }
        $yearDisp  = if ($l.year) { $l.year } else { 'N/A' }
        @"

    <div style="${border}border-radius:8px;background:#fff;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 1px 4px rgba(0,0,0,.08);">
      $imgHtml
      <div style="padding:14px 16px;flex:1;display:flex;flex-direction:column;gap:6px;">
        <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;">
          <span style="background:${color};color:#fff;font-size:10px;font-weight:bold;padding:2px 7px;border-radius:4px;text-transform:uppercase;">$label</span>
          $newBadge
        </div>
        <div style="font-size:15px;font-weight:600;color:#212121;">$($l.title)</div>
        <div style="font-size:18px;font-weight:700;color:#2e7d32;">$($l.price)</div>
        <div style="font-size:13px;color:#616161;">&#128197; $yearDisp &nbsp;&bull;&nbsp; &#128663; $mileDisp</div>
        <a href="$($l.url)" target="_blank" style="margin-top:auto;padding:8px 14px;background:#1565c0;color:#fff;text-decoration:none;border-radius:4px;font-size:13px;font-weight:bold;text-align:center;display:block;">View Listing &rarr;</a>
      </div>
    </div>
"@
    }

    $summaryText  = if ($newCount -gt 0) { "$newCount new" } else { 'No new listings' }
    $maxMileFmt   = '{0:N0}' -f [int]$filters.max_mileage
    $maxPriceFmt  = '{0:N0}' -f [int]$filters.max_price
    $totalCount   = $allListings.Count
    $gridOrEmpty  = if ($totalCount -gt 0) {
        "<div class=`"grid`">$($cardsHtml -join '')</div>"
    } else {
        "<div class=`"empty`">No listings found. Run the script again to check for cars.</div>"
    }

    $html = @"
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>BMW M2 Monitor</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#212121}
    .header{background:#1a237e;color:#fff;padding:20px 24px}
    .header h1{font-size:20px;font-weight:700}
    .header p{font-size:13px;opacity:.75;margin-top:4px}
    .stats{display:flex;gap:16px;padding:14px 24px;background:#fff;border-bottom:1px solid #e0e0e0;flex-wrap:wrap}
    .stat{font-size:13px;color:#616161}
    .stat strong{color:#212121}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;padding:20px 24px;max-width:1400px;margin:0 auto}
    .empty{text-align:center;padding:60px 24px;color:#9e9e9e;font-size:15px}
  </style>
</head>
<body>
  <div class="header">
    <h1>&#128663; BMW M2 Monitor</h1>
    <p>Last updated: $now &mdash; $totalCount total &mdash; <strong style="color:#ef9a9a;">$summaryText</strong></p>
  </div>
  <div class="stats">
    <div class="stat">Filters: <strong>&ge; $($filters.min_year) &bull; &le; $maxMileFmt miles &bull; &le; &pound;$maxPriceFmt</strong></div>
    <div class="stat">Area: <strong>$($filters.postcode) &bull; $($filters.radius_miles) mile radius</strong></div>
    <div class="stat">Sources: <strong>AutoTrader &bull; PistonHeads &bull; BMW UK</strong></div>
  </div>
  $gridOrEmpty
</body>
</html>
"@

    [System.IO.File]::WriteAllText($DashFile, $html, [System.Text.Encoding]::UTF8)
    Info "Dashboard written: $DashFile ($totalCount listings, $newCount new)"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
Info ('=' * 60)
Info "Car Monitor starting — $(Get-Date -Format 'o')$(if ($Test) { ' [TEST MODE]' })"

if (-not (Test-Path $ConfigFile)) {
    Err "config.json not found at $ConfigFile"
    exit 1
}

$config  = Get-Content $ConfigFile -Raw | ConvertFrom-Json
$filters = $config.filters
$sites   = if ($config.sites) { $config.sites } else { [PSCustomObject]@{ autotrader = $true; pistonheads = $true; bmw_uk = $true } }

$allListings = @()

if ($sites.autotrader -ne $false) {
    try   { $allListings += Invoke-AutotraderScrape $filters }
    catch { Err "AutoTrader scraper crashed: $_" }
}

if ($sites.pistonheads -ne $false) {
    Start-Sleep -Seconds 2
    try   { $allListings += Invoke-PistonheadsScrape $filters }
    catch { Err "PistonHeads scraper crashed: $_" }
}

if ($sites.bmw_uk -ne $false) {
    Start-Sleep -Seconds 2
    try   { $allListings += Invoke-BmwUkScrape $filters }
    catch { Err "BMW UK scraper crashed: $_" }
}

Info "Total listings scraped: $($allListings.Count)"

$seen = Load-Seen

if ($Test) {
    $newIds = $allListings | ForEach-Object { $_.id }
    Info "TEST MODE: treating all $($newIds.Count) listings as new"
} else {
    $newIds = @()
    foreach ($l in $allListings) {
        $seenForSite = @($seen.($l.site))
        if ($seenForSite -notcontains $l.id) { $newIds += $l.id }
    }
}

Info "New listings: $($newIds.Count)"

Write-Dashboard $allListings $newIds $filters

if (-not $Test) {
    foreach ($l in $allListings) {
        $seenForSite = @($seen.($l.site))
        if ($seenForSite -notcontains $l.id) {
            $seen.($l.site) = $seenForSite + $l.id
        }
    }
    Save-Seen $seen
    Info "seen_listings.json updated."
} else {
    Info "TEST MODE: seen_listings.json not updated."
}

Info "Run complete — $($allListings.Count) total, $($newIds.Count) new."
Info ('=' * 60)

# Open dashboard in browser
Start-Process $DashFile
