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
    variant: Optional[str] = None


@dataclass
class SaleRecord:
    sale_date: str
    price: float
    currency: str


class BaseScraper:
    name: str = "base"

    def fetch_sales(self, vehicle: VehicleQuery) -> List[SaleRecord]:
        raise NotImplementedError


# ---------- FX (to USD) ----------

FX_API_URL = "https://api.exchangerate.host/convert"


def convert_to_usd(amount: float, from_ccy: str) -> float:
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
    today = datetime.utcnow().date()
    cutoff = today - timedelta(days=years * 365)
    result: List[SaleRecord] = []
    for s in sales:
        try:
            sd = _parse_date_iso(s.sale_date)
        except ValueError:
            continue
        if sd > today:
            continue
        if sd >= cutoff:
            result.append(s)
    return result


def dedupe_sales(sales: List[SaleRecord]) -> List[SaleRecord]:
    seen = set()
    unique: List[SaleRecord] = []
    for s in sales:
        key = (
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

    # 5-year subset
    sales_5y = filter_last_n_years(all_sales, 5)

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
        })

    # 1-year subset
    sales_1y = filter_last_n_years(all_sales, 1)
    prices_1y_usd = []
    for s in sales_1y:
        try:
            prices_1y_usd.append(convert_to_usd(s.price, s.currency))
        except Exception:
            continue

    if prices_1y_usd:
        avg_price_1y_usd = sum(prices_1y_usd) / len(prices_1y_usd)
        sample_size_1y = len(prices_1y_usd)
    else:
        avg_price_1y_usd = 0.0
        sample_size_1y = 0

    # Most recent sale
    last_sale_price_usd = None
    last_sale_date = None

    if enriched_5y:
        sorted_sales = sorted(enriched_5y, key=lambda x: x["sale_date"], reverse=True)
        last_sale = sorted_sales[0]
        last_sale_price_usd = last_sale["price_usd"]
        last_sale_date = last_sale["sale_date"]

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


# ---------- Bring a Trailer scraper ----------

BAT_RESULTS_URL = (
    "https://bringatrailer.com/auctions/results/?search={query}&sort=recent"
)

BAT_PRICE_RE = re.compile(
    r"(Sold for|Bid to)\s+(USD|EUR|GBP)\s+[^0-9]*(\d[\d,]*)\s+on\s+(\d{1,2}/\d{1,2}/\d{2})",
    re.IGNORECASE,
)


def _parse_bat_date(us_short: str) -> str:
    m, d, yy = (int(part) for part in us_short.split("/"))
    today = datetime.utcnow().date()
    current_year = today.year
    current_century = (current_year // 100) * 100
    current_two_digit = current_year % 100

    if yy <= current_two_digit:
        year = current_century + yy
    else:
        year = (current_century - 100) + yy

    return date(year, m, d).isoformat()


class BaTScraper(BaseScraper):
    name = "bring_a_trailer"

    def __init__(self, max_results_per_vehicle: int = 150, headless: bool = True, include_bid_to: bool = False):
        self.max_results = max_results_per_vehicle
        self.headless = headless
        self.include_bid_to = include_bid_to

    def _build_query(self, vehicle: VehicleQuery) -> str:
        parts = [
            str(vehicle.year),
            vehicle.make,
            vehicle.model,
        ]
        if vehicle.variant:
            parts.append(vehicle.variant)
        return " ".join([p for p in parts if p])

    def fetch_sales(self, vehicle: VehicleQuery) -> List[SaleRecord]:
        query_str = self._build_query(vehicle)
        url = BAT_RESULTS_URL.format(query=urllib.parse.quote_plus(query_str))

        sales: List[SaleRecord] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(2000)

            cards = page.query_selector_all("article")

            for card in cards:
                if len(sales) >= self.max_results:
                    break

                text = card.inner_text()
                m = BAT_PRICE_RE.search(text)
                if not m:
                    continue

                status, ccy, price_raw, date_raw = m.groups()

                if not self.include_bid_to and status.lower().startswith("bid to"):
                    continue

                try:
                    price = float(price_raw.replace(",", ""))
                    sale_date_iso = _parse_bat_date(date_raw)
                except Exception:
                    continue

                sales.append(
                    SaleRecord(
                        sale_date=sale_date_iso,
                        price=price,
                        currency=ccy.upper(),
                    )
                )

            browser.close()

        return sales


# ---------- Classic.com scraper ----------

class ClassicComScraper(BaseScraper):
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

            cards = page.query_selector_all("div:has-text('Sold')")

            for card in cards:
                text = card.inner_text()

                # Price like "$490,000"
                m_price = re.search(r"([€$£])([\d,]+)", text)
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
                except:
                    continue

                try:
                    dt = datetime.strptime(dt_str, "%b %d, %Y").date()
                    sale_date_iso = dt.isoformat()
                except:
                    continue

                sales.append(
                    SaleRecord(
                        sale_date=sale_date_iso,
                        price=price,
                        currency=ccy,
                    )
                )

            browser.close()

        return sales


# ---------- Collecting Cars scraper (stub) ----------

class CollectingCarsScraper(BaseScraper):
    name = "collecting_cars"

    def fetch_sales(self, vehicle: VehicleQuery) -> List[SaleRecord]:
        return []


# ---------- API + IO ----------

def post_to_market_api(payload: Dict[str, Any]) -> None:
    api_url = os.getenv("MARKET_API_URL")
    if not api_url:
        print("  MARKET_API_URL not set; skipping API POST.")
        return

    headers = {"Content-Type": "application/json"}
    token = os.getenv("MARKET_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.post(api_url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()
    print("  Posted to API:", resp.status_code)


def load_vehicles_from_csv(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


# ---------- Main pipeline ----------

def main():
    bat_scraper = BaTScraper(include_bid_to=False)
    classic_scraper = ClassicComScraper()
    cc_scraper = CollectingCarsScraper()

    rows = load_vehicles_from_csv("vehicles.csv")
    print(f"Loaded {len(rows)} vehicles.")

    all_vehicle_payloads = []

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

        # Bring a Trailer
        try:
            print("  Scraping BaT...")
            bat_sales = bat_scraper.fetch_sales(vehicle)
            print(f"    {len(bat_sales)} BaT sales")
            all_sales.extend(bat_sales)
        except Exception as e:
            print("    Error:", e)

        # Collecting Cars
        try:
            cc_sales = cc_scraper.fetch_sales(vehicle)
            all_sales.extend(cc_sales)
        except Exception as e:
            print("    Error:", e)

        # Classic.com
        classic_url = row.get("classic_market_url") or ""
        if classic_url:
            try:
                print(f"  Scraping Classic.com: {classic_url}")
                classic_sales = classic_scraper.fetch_sales_for_market(classic_url)
                print(f"    {len(classic_sales)} Classic.com sales")
                all_sales.extend(classic_sales)
            except Exception as e:
                print("    Error:", e)

        if not all_sales:
            print("  No sales found for this vehicle.")
            continue

        all_sales = dedupe_sales(all_sales)
        print(f"  After de-duplication: {len(all_sales)} sales")

        payload = build_vehicle_market_json(vehicle, all_sales)

        # Save per-vehicle JSON
        safe_name = vehicle.name.replace(" ", "_").replace("/", "-")
        out_name = f"out_{vehicle.year}_{safe_name}.json"
        with open(out_name, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        all_vehicle_payloads.append(payload)

    if not all_vehicle_payloads:
        print("No vehicles produced results.")
        return

    snapshot = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "vehicle_count": len(all_vehicle_payloads),
        "vehicles": all_vehicle_payloads,
    }

    print("\nPosting aggregated snapshot...")
    post_to_market_api(snapshot)


if __name__ == "__main__":
    main()
