import csv
import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Optional, List, Dict, Any

import requests
from playwright.sync_api import sync_playwright


# ---------- Models ----------

@dataclass
class VehicleQuery:
    name: str
    year: int
    make: str
    model: str
    variant: Optional[str] = None  # optional extra descriptor


@dataclass
class SaleRecord:
    sale_date: str        # "YYYY-MM-DD"
    price: float          # original currency
    currency: str         # "USD", "GBP", "EUR", etc.
    source: str           # "bring_a_trailer", "collecting_cars", "classic_com"
    auction_house: Optional[str] = None
    location: Optional[str] = None
    url: Optional[str] = None


class BaseScraper:
    name: str = "base"

    def fetch_sales(self, vehicle: VehicleQuery) -> List[SaleRecord]:
        raise NotImplementedError


# ---------- FX (to USD) ----------

FX_API_URL = "https://api.exchangerate.host/convert"


def convert_to_usd(amount: float, from_ccy: str) -> float:
    """
    Convert amount from from_ccy to USD using exchangerate.host.
    If conversion fails, this will raise an exception.
    """
    from_ccy = from_ccy.upper()
    if from_ccy == "USD":
        return amount

    resp = requests.get(
        FX_API_URL,
        params={"from": from_ccy, "to": "USD", "amount": amount},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if "result" not in data or data["result"] is None:
        raise ValueError(f"FX API returned no result for {from_ccy} -> USD")
    return float(data["result"])


# ---------- Aggregation & De-duplication ----------

def _parse_date_iso(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()


def filter_last_n_years(sales: List[SaleRecord], years: int) -> List[SaleRecord]:
    """
    Keep only sales within the last N years and never keep future-dated sales.
    """
    today = datetime.utcnow().date()
    cutoff = today - timedelta(days=years * 365)  # approximate is fine
    result: List[SaleRecord] = []
    for s in sales:
        try:
            sd = _parse_date_iso(s.sale_date)
        except ValueError:
            continue
        # drop any future dates defensively
        if sd > today:
            continue
        if sd >= cutoff:
            result.append(s)
    return result


def dedupe_sales(sales: List[SaleRecord]) -> List[SaleRecord]:
    """
    Remove exact duplicate sales based on (source, url, sale_date, price, currency).
    Keeps the first occurrence.
    """
    seen = set()
    unique: List[SaleRecord] = []

    for s in sales:
        key = (
            s.source,
            s.url or "",
            s.sale_date,
            round(s.price, 2),
            s.currency.upper(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)

    return unique


def build_vehicle_market_json(
    vehicle: VehicleQuery,
    all_sales: List[SaleRecord]
) -> Dict[str, Any]:
    """
    Build the final JSON payload for a single vehicle:

    {
      "vehicle_name": "...",
      "vehicle_year": 1990,
      "stats": {
        "avg_price_1y_usd": ...,
        "sample_size_1y": ...,
        "last_sale_price_usd": ...,
        "last_sale_date": "YYYY-MM-DD"
      },
      "sales_5y": [ ... ]
    }
    """

    # 5-year subset
    sales_5y = filter_last_n_years(all_sales, 5)

    # Enrich with price_usd
    enriched_5y: List[Dict[str, Any]] = []
    for s in sales_5y:
        try:
            price_usd = convert_to_usd(s.price, s.currency)
        except Exception:
            price_usd = None

        enriched_5y.append({
            "sale_date": s.sale_date,
            "price": s.price,
            "currency": s.currency,
            "price_usd": price_usd,
            "source": s.source,
            "auction_house": s.auction_house,
            "location": s.location,
            "url": s.url,
        })

    # 1-year subset
    sales_1y = filter_last_n_years(all_sales, 1)
    prices_1y_usd: List[float] = []
    for s in sales_1y:
        try:
            usd = convert_to_usd(s.price, s.currency)
            prices_1y_usd.append(usd)
        except Exception:
            continue

    if prices_1y_usd:
        avg_price_1y_usd = sum(prices_1y_usd) / len(prices_1y_usd)
        sample_size_1y = len(prices_1y_usd)
    else:
        avg_price_1y_usd = 0.0
        sample_size_1y = 0

    # Last sale (most recent in 5 years)
    last_sale_price_usd = None
    last_sale_date = None
    if enriched_5y:
        sorted_sales = sorted(
            enriched_5y, key=lambda x: x["sale_date"], reverse=True
        )
        last_sale = sorted_sales[0]
        last_sale_date = last_sale["sale_date"]
        last_sale_price_usd = last_sale["price_usd"]

    return {
        "vehicle_name": vehicle.name,
        "vehicle_year": vehicle.year,
        "stats": {
            "avg_price_1y_usd": avg_price_1y_usd,
            "sample_size_1y": sample_size_1y,
            "last_sale_price_usd": last_sale_price_usd,
            "last_sale_date": last_sale_date,
        },
        "sales_5y": enriched_5y,
    }


# ---------- Bring a Trailer scraper (Playwright) ----------

BAT_RESULTS_URL = (
    "https://bringatrailer.com/auctions/results/?search={query}&sort=recent"
)

# Example text on BaT:
# "Sold for USD $107,000 on 11/20/25"
# "Bid to USD $50,000 on 11/19/25"
BAT_PRICE_RE = re.compile(
    r"(Sold for|Bid to)\s+(USD|EUR|GBP)\s+[^0-9]*(\d[\d,]*)\s+on\s+(\d{1,2}/\d{1,2}/\d{2})",
    re.IGNORECASE,
)


def _parse_bat_date(us_short: str) -> str:
    """
    Convert MM/DD/YY → YYYY-MM-DD using a sliding window so we don't create
    future years like 2026 when we're still in 2025.

    Rule:
      - If YY <= current two-digit year → treat as this century (20xx)
      - If YY >  current two-digit year → treat as previous century (19xx)
    """
    m, d, yy = (int(part) for part in us_short.split("/"))

    today = datetime.utcnow().date()
    current_year = today.year                       # e.g. 2025
    current_century = (current_year // 100) * 100   # e.g. 2000
    current_two_digit = current_year % 100          # e.g. 25

    if yy <= current_two_digit:
        # same century (e.g. 00–25 → 2000–2025)
        year = current_century + yy
    else:
        # previous century (e.g. 26–99 → 1926–1999)
        year = (current_century - 100) + yy

    return date(year, m, d).isoformat()


class BaTScraper(BaseScraper):
    name = "bring_a_trailer"

    def __init__(self, max_results_per_vehicle: int = 150, headless: bool = True, include_bid_to: bool = False):
        """
        include_bid_to=False means we skip "Bid to" (unsold) entries and keep
        only "Sold for" records. Set to True if you want both.
        """
        self.max_results = max_results_per_vehicle
        self.headless = headless
        self.include_bid_to = include_bid_to

    def _build_query(self, vehicle: VehicleQuery) -> str:
        parts: List[str] = []
        if vehicle.year:
            parts.append(str(vehicle.year))
        if vehicle.make:
            parts.append(vehicle.make)
        if vehicle.model:
            parts.append(vehicle.model)
        if vehicle.variant:
            parts.append(vehicle.variant)
        return " ".join(parts)

    def fetch_sales(self, vehicle: VehicleQuery) -> List[SaleRecord]:
        query_str = self._build_query(vehicle)
        url = BAT_RESULTS_URL.format(query=urllib.parse.quote_plus(query_str))

        sales: List[SaleRecord] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(2000)  # crude wait; can be refined with selectors

            # BaT results are laid out as article cards.
            cards = page.query_selector_all("article")

            for card in cards:
                if len(sales) >= self.max_results:
                    break

                text = card.inner_text()

                m = BAT_PRICE_RE.search(text)
                if not m:
                    continue

                status, ccy, price_raw, date_raw = m.groups()

                # Optionally skip unsold "Bid to" lots
                if not self.include_bid_to and status.lower().startswith("bid to"):
                    continue

                try:
                    price = float(price_raw.replace(",", ""))
                    sale_date_iso = _parse_bat_date(date_raw)
                except Exception:
                    continue

                # Location is optional; this is a best-effort guess.
                location = None
                try:
                    # This selector is intentionally generic; feel free to tweak.
                    loc_el = card.query_selector("div:has-text('USA'), div:has-text('UK')")
                    if loc_el:
                        location = (loc_el.inner_text() or "").strip()
                except Exception:
                    pass

                link = None
                try:
                    a = card.query_selector("a")
                    if a:
                        href = a.get_attribute("href")
                        if href and href.startswith("http"):
                            link = href
                except Exception:
                    pass

                sales.append(
                    SaleRecord(
                        sale_date=sale_date_iso,
                        price=price,
                        currency=ccy.upper(),
                        source=self.name,
                        auction_house="Bring a Trailer",
                        location=location,
                        url=link,
                    )
                )

            browser.close()

        return sales


# ---------- Classic.com scraper (market page) ----------

class ClassicComScraper(BaseScraper):
    """
    Scrapes a specific Classic.com market page (e.g. Ferrari F50).

    It parses SOLD cards for price and date.
    You must provide a 'classic_market_url' column in vehicles.csv for it to use.
    """

    name = "classic_com"

    def __init__(self, headless: bool = True):
        self.headless = headless

    def fetch_sales_for_market(self, market_url: str) -> List[SaleRecord]:
        sales: List[SaleRecord] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_page()
            page.goto(market_url, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(2000)

            # This is intentionally generic; you may need to refine selectors
            # after inspecting the actual Classic.com HTML for your markets.
            cards = page.query_selector_all("div:has-text('Sold')")

            for card in cards:
                text = card.inner_text()

                # Currency symbol + number, e.g. "$1,234,000" or "€4,842,500"
                m_price = re.search(r"([€$£])([\d,]+(?:\.\d{3})?)", text)
                # Date like "Oct 18, 2025"
                m_date = re.search(
                    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}",
                    text,
                )
                if not (m_price and m_date):
                    continue

                symbol = m_price.group(1)
                price_raw = m_price.group(2)
                dt_str = m_date.group(0)

                symbol_to_ccy = {"$": "USD", "€": "EUR", "£": "GBP"}
                ccy = symbol_to_ccy.get(symbol, "USD")

                try:
                    price = float(price_raw.replace(",", ""))
                except Exception:
                    continue

                try:
                    dt = datetime.strptime(dt_str, "%b %d, %Y").date()
                    sale_date_iso = dt.isoformat()
                except Exception:
                    continue

                location = None
                try:
                    loc_el = card.query_selector("a:has-text(',')")
                    if loc_el:
                        location = (loc_el.inner_text() or "").strip()
                except Exception:
                    pass

                link = None
                try:
                    a = card.query_selector("a[href*='/listing/'], a[href*='/auction/']")
                    if a:
                        href = a.get_attribute("href")
                        if href and href.startswith("http"):
                            link = href
                except Exception:
                    pass

                sales.append(
                    SaleRecord(
                        sale_date=sale_date_iso,
                        price=price,
                        currency=ccy,
                        source=self.name,
                        auction_house="Classic.com aggregated",
                        location=location,
                        url=link,
                    )
                )

            browser.close()

        return sales


# ---------- Collecting Cars scraper (skeleton) ----------

class CollectingCarsScraper(BaseScraper):
    """
    Placeholder scraper for Collecting Cars.

    TODO: Inspect their HTML and implement Playwright scraping similar to BaT.
    Currently returns no sales so the pipeline still runs.
    """

    name = "collecting_cars"

    def __init__(self, max_results_per_vehicle: int = 100, headless: bool = True):
        self.max_results = max_results_per_vehicle
        self.headless = headless

    def fetch_sales(self, vehicle: VehicleQuery) -> List[SaleRecord]:
        return []  # implement later


# ---------- API + IO ----------

def post_to_market_api(payload: Dict[str, Any]) -> None:
    """
    POST the payload to your API.

    Env vars:
      MARKET_API_URL  (optional; if not set, API POST is skipped)
      MARKET_API_TOKEN (optional, Bearer token)
    """
    api_url = os.getenv("MARKET_API_URL")
    if not api_url:
        print("  MARKET_API_URL not set; skipping API POST.")
        return

    headers = {"Content-Type": "application/json"}
    token = os.getenv("MARKET_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.post(api_url, headers=headers, json=payload, timeout=15)
    try:
        resp.raise_for_status()
    except Exception as e:
        print("Error posting to API:", e)
        print("Status:", resp.status_code)
        print("Body:", resp.text)
        raise

    print("  Posted to API:", resp.status_code)


def load_vehicles_from_csv(path: str) -> List[Dict[str, Any]]:
    """
    Returns each row as a dict so we can read classic_market_url, etc.
    """
    rows: List[Dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


# ---------- Main pipeline ----------

def main():
    bat_scraper = BaTScraper(include_bid_to=False)  # skip "Bid to" by default
    classic_scraper = ClassicComScraper()
    cc_scraper = CollectingCarsScraper()

    rows = load_vehicles_from_csv("vehicles.csv")
    print(f"Loaded {len(rows)} vehicle rows from CSV")

    # collect per-vehicle payloads here (IMPORTANT: outside the loop)
    all_vehicle_payloads: List[Dict[str, Any]] = []

    for row in rows:
        vehicle = VehicleQuery(
            name=row["name"],
            year=int(row["year"]),
            make=row["make"],
            model=row["model"],
            variant=row.get("variant") or None,
        )

        print(f"\nProcessing {vehicle.name} ({vehicle.year})")

        all_sales: List[SaleRecord] = []

        # 1) Bring a Trailer
        try:
            print("  Scraping Bring a Trailer...")
            bat_sales = bat_scraper.fetch_sales(vehicle)
            print(f"    {len(bat_sales)} BaT sales")
            all_sales.extend(bat_sales)
        except Exception as e:
            print("    Error (BaT):", e)

        # 2) Collecting Cars (currently stub)
        try:
            print("  Scraping Collecting Cars (stub)...")
            cc_sales = cc_scraper.fetch_sales(vehicle)
            print(f"    {len(cc_sales)} Collecting Cars sales")
            all_sales.extend(cc_sales)
        except Exception as e:
            print("    Error (Collecting Cars):", e)

        # 3) Classic.com – use market URL from CSV if present
        classic_market_url = row.get("classic_market_url") or ""
        if classic_market_url:
            try:
                print(f"  Scraping Classic.com market: {classic_market_url}")
                classic_sales = classic_scraper.fetch_sales_for_market(classic_market_url)
                print(f"    {len(classic_sales)} Classic.com sales")
                all_sales.extend(classic_sales)
            except Exception as e:
                print("    Error (Classic.com):", e)
        else:
            print("  No Classic.com market URL in CSV; skipping Classic.com.")

        if not all_sales:
            print("  No sales found; skipping JSON/API for this vehicle.")
            continue

        # De-duplicate before aggregation
        all_sales = dedupe_sales(all_sales)
        print(f"  After de-duplication: {len(all_sales)} unique sales")

        payload = build_vehicle_market_json(vehicle, all_sales)

        # Local JSON for debugging (keep this per-vehicle)
        safe_name = vehicle.name.replace(" ", "_").replace("/", "-")
        out_name = f"out_{vehicle.year}_{safe_name}.json"
        with open(out_name, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"  Wrote {out_name}")

        # collect payload instead of posting per vehicle
        all_vehicle_payloads.append(payload)
        print(f"  Collected payloads so far: {len(all_vehicle_payloads)}")

    # After processing all vehicles, send a single aggregated snapshot to the API
    if not all_vehicle_payloads:
        print("\nNo vehicles produced results; nothing to POST.")
        return

    snapshot: Dict[str, Any] = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "vehicle_count": len(all_vehicle_payloads),
        "vehicles": all_vehicle_payloads,
    }

    print(
        f"\nPosting aggregated snapshot with {len(all_vehicle_payloads)} "
        f"vehicles to API..."
    )
    # Optional: log the vehicle names for sanity
    print("  Vehicles in snapshot:", [v["vehicle_name"] for v in all_vehicle_payloads])

    try:
        post_to_market_api(snapshot)
    except Exception:
        # errors already logged in post_to_market_api
        pass


if __name__ == "__main__":
    main()
