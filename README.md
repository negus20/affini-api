# Classic Market Scraper (Local Test, De-dup Version)

This is a small prototype that:

- Reads a list of vehicles from `vehicles.csv`
- Scrapes:
  - **Bring a Trailer** auction results (via Playwright)
  - **Classic.com** market pages (if a `classic_market_url` is provided)
  - **Collecting Cars** (stub only for now)
- Normalizes all sale prices to **USD** using a free FX API
- **De-duplicates** obvious duplicate sale entries
- Computes:
  - Average sale price over the last **1 year**
  - Last sale price and date
  - List of all sales over the last **5 years**
- Writes the results to local JSON files and (optionally) POSTs them to your API.

> ⚠️ **Important:** Even though this runs only on your machine, you are responsible for ensuring your use complies with each site's Terms of Service and robots.txt. Use this for experimentation and testing, and consider contacting data providers (e.g., Classic.com) for official data access in production.

---

## 1. Prerequisites

- Python **3.9+** installed
- `pip` available in your shell
- Internet access (for:
  - scraping the sites
  - FX API calls to `exchangerate.host`
)

---

## 2. Install dependencies

From the project directory:

```bash
cd classic_market_scraper_v2
pip install -r requirements.txt
```

Then install the Playwright browser binary (Chromium):

```bash
playwright install chromium
```

---

## 3. Configure vehicles

Edit `vehicles.csv` to list the vehicles you care about. Example:

```csv
name,year,make,model,variant,classic_market_url
Ferrari F50,1995,Ferrari,F50,,https://www.classic.com/m/ferrari/f50/
Porsche 911 Carrera 4S,1997,Porsche,911 Carrera 4S,993,
Toyota Supra Twin Turbo,1994,Toyota,Supra Twin Turbo,MK4,
```

- `name` is free-form (what you want to see in output)
- `year`, `make`, `model`, `variant` are used to build search queries
- `classic_market_url` should be a Classic.com *market* URL if you want to include that data; otherwise leave it blank for that row

You can add/remove rows freely.

---

## 4. (Optional) Configure your API endpoint

If you want the script to POST results into your own API, set these environment variables:

### macOS / Linux (bash/zsh):

```bash
export MARKET_API_URL="http://127.0.0.1:8000/market-data"
export MARKET_API_TOKEN="YOUR_OPTIONAL_BEARER_TOKEN"
```

### Windows PowerShell:

```powershell
$env:MARKET_API_URL = "http://127.0.0.1:8000/market-data"
$env:MARKET_API_TOKEN = "YOUR_OPTIONAL_BEARER_TOKEN"
```

- If `MARKET_API_URL` is **not** set, the script will **skip the API POST** and just write local JSON files.
- If `MARKET_API_TOKEN` is set, it’ll be used as a Bearer token.

For quick testing, you can point `MARKET_API_URL` at the local FastAPI server from `api_server.py` (if you’ve created one) or any endpoint that accepts JSON.

---

## 5. Run the scraper locally

From the `classic_market_scraper_v2` directory:

```bash
python market_pipeline.py
```

You’ll see logs like:

```text
Processing Ferrari F50 (1995)
  Scraping Bring a Trailer...
    3 BaT sales
  Scraping Collecting Cars (stub)...
    0 Collecting Cars sales
  Scraping Classic.com market: https://www.classic.com/m/ferrari/f50/
    12 Classic.com sales
  After de-duplication: 10 unique sales
  Wrote out_1995_Ferrari_F50.json
  MARKET_API_URL not set; skipping API POST.
```

For each vehicle, a file named like this will be created:

- `out_1995_Ferrari_F50.json`
- `out_1997_Porsche_911_Carrera_4S.json`
- etc.

Each JSON file has the structure:

```json
{
  "vehicle_name": "Ferrari F50",
  "vehicle_year": 1995,
  "stats": {
    "avg_price_1y_usd": 123456.78,
    "sample_size_1y": 5,
    "last_sale_price_usd": 234567.89,
    "last_sale_date": "2025-03-01"
  },
  "sales_5y": [
    {
      "sale_date": "2025-03-01",
      "price": 200000.0,
      "currency": "EUR",
      "price_usd": 215000.0,
      "source": "classic_com",
      "auction_house": "Classic.com aggregated",
      "location": "Monaco, Monaco",
      "url": "https://www.classic.com/listing/..."
    }
  ]
}
```

Values will, of course, depend on real-world data and what the scrapers are able to parse.

---

## 6. What changed vs the first version

### 6.1 De-duplication

Before building the JSON, the script now calls:

```python
all_sales = dedupe_sales(all_sales)
```

The `dedupe_sales` function removes exact duplicates based on:

- `source`
- `url` (or empty string if missing)
- `sale_date`
- `price` (rounded to 2 decimals)
- `currency` (uppercased)

This gets rid of obvious accidental duplicates, especially from:

- Classic.com cards being parsed more than once
- BaT scraping quirks

### 6.2 Skipping "Bid to" (unsold) entries on Bring a Trailer

The `BaTScraper` now has a flag `include_bid_to` (default `False`), and the pipeline initializes it with:

```python
bat_scraper = BaTScraper(include_bid_to=False)
```

Inside the scraper:

```python
status, ccy, price_raw, date_raw = m.groups()

# Optionally skip unsold "Bid to" lots
if not self.include_bid_to and status.lower().startswith("bid to"):
    continue
```

So by default you only keep **actual sold** results.  
If you later want to include “Bid to” (unsold high bids), create the scraper like this:

```python
bat_scraper = BaTScraper(include_bid_to=True)
```

---

## 7. Notes about each scraper

### Bring a Trailer (`BaTScraper`)

- Uses Playwright (Chromium) to load the auction results page for each vehicle
- Builds a search query from `year + make + model + variant`
- Parses the text of each auction card with a regex looking for:
  - `Sold for` (and optionally `Bid to`)
  - Currency (USD/EUR/GBP)
  - Price
  - Sale date (MM/DD/YY)
- Returns `SaleRecord` objects with:
  - `sale_date` in ISO format (YYYY-MM-DD)
  - `price` in original currency
  - `currency`
  - `auction_house = "Bring a Trailer"`

Selector details may need tuning over time if the site’s HTML changes.

### Classic.com (`ClassicComScraper`)

- Expects a Classic.com *market page* URL from the CSV, e.g.:
  - `https://www.classic.com/m/ferrari/f50/`
- Uses Playwright to load that page
- Looks for generic elements containing `Sold`
- Inside each card, uses regex to find:
  - A currency symbol (`$`, `€`, `£`) and price
  - A date like `Oct 18, 2025`
- Outputs `SaleRecord` objects with original price + currency

This is intentionally generic and may need refinement for specific markets.

### Collecting Cars (`CollectingCarsScraper`)

- Currently a **stub** that returns no data.
- Once you’ve inspected their past auction results HTML, you can:
  - Use the same Playwright approach as BaT
  - Implement a similar `fetch_sales` that yields `SaleRecord` objects

---

## 8. Scheduling (optional)

Once you’re happy with the behavior, you can run this script on a schedule, e.g.:

### macOS / Linux via `cron`

```bash
crontab -e
```

Add something like:

```cron
0 6,18 * * * cd /path/to/classic_market_scraper_v2 && /usr/bin/python market_pipeline.py >> scraper.log 2>&1
```

This would run it at 06:00 and 18:00 every day.

---

## 9. Troubleshooting

- **Playwright errors about browser not installed**

  Make sure you ran:

  ```bash
  playwright install chromium
  ```

- **SSL / connection errors**

  Check you have working internet and that none of the sites are blocking your IP.

- **FX conversion issues**

  If the FX API returns an error or no `result`, the script sets `price_usd` to `null` for that sale.

- **No sales found**

  Could be:
  - No matching auctions for the search query (try simplifying the query or dropping `variant`)
  - HTML structure changed (selectors might need tweaking)
  - Classic.com market URL not set or not correct in the CSV

---

If you run into issues, feel free to share the error message and I can help you tweak the scrapers, selectors, or de-dup logic.
