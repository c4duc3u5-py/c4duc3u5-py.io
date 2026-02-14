"""
eBay Seller Listings Scraper
==============================
Scrapes a seller's active eBay listings from their public seller page.
No API keys required — uses the publicly accessible seller search page.

Two scraping backends:
  1. **Browser** (default): Uses Playwright with system Chrome to bypass bot detection.
  2. **HTTP** (fallback): Uses httpx — fast but gets blocked by eBay's CAPTCHA.

Extracts: title, price, image URL, listing URL, condition, shipping info.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urljoin

import config

logger = logging.getLogger(__name__)


@dataclass
class EbayListing:
    """Represents a single eBay listing scraped from the seller page."""

    item_id: str = ""
    title: str = ""
    price: float = 0.0
    currency: str = "GBP"
    condition: str = ""
    image_url: str = ""
    listing_url: str = ""
    shipping: str = ""
    bids: Optional[int] = None
    listing_type: str = "fixed"  # "fixed" or "auction"
    category_hint: str = ""  # derived from title keywords
    scraped_at: str = ""

    @property
    def content_hash(self) -> str:
        """Deterministic hash for deduplication."""
        raw = f"{self.item_id}:{self.title}:{self.price}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


@dataclass
class ScrapeResult:
    """Result of a full scrape run."""

    seller_name: str = ""
    total_listings: int = 0
    pages_scraped: int = 0
    listings: list[EbayListing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class EbayScraper:
    """
    Scrapes active listings from an eBay seller's public page.

    Supports two backends:
      - "browser": Uses Playwright with system Chrome (bypasses bot detection)
      - "http": Uses httpx + selectolax (fast but often blocked)
    """

    SEARCH_URL = "https://www.ebay.co.uk/sch/m.html"

    # Paths to system browsers (Playwright can use these directly)
    BROWSER_PATHS = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]

    def __init__(
        self,
        seller_name: str | None = None,
        max_listings: int | None = None,
        request_delay: float | None = None,
        backend: str = "browser",  # "browser" or "http"
    ):
        self.seller_name = seller_name or config.EBAY_SELLER_NAME
        self.max_listings = max_listings if max_listings is not None else config.EBAY_MAX_LISTINGS
        self.request_delay = request_delay if request_delay is not None else config.EBAY_REQUEST_DELAY
        self.backend = backend
        self._http_client = None

    def close(self):
        """Close any open resources."""
        if self._http_client:
            self._http_client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    def scrape_all_listings(self) -> ScrapeResult:
        """Scrape all active listings from the seller's page."""
        if self.backend == "browser":
            return self._scrape_with_browser()
        else:
            return self._scrape_with_http()

    # ─────────────────────────────────────────
    # Browser backend (Playwright)
    # ─────────────────────────────────────────

    def _find_browser(self) -> Optional[str]:
        """Find a system-installed Chromium browser."""
        import os
        for path in self.BROWSER_PATHS:
            if os.path.exists(path):
                logger.info("Found browser: %s", path)
                return path
        return None

    def _scrape_with_browser(self) -> ScrapeResult:
        """Scrape using Playwright with system Chrome/Edge."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error(
                "Playwright not installed. Install with: pip install playwright\n"
                "Falling back to HTTP backend."
            )
            return self._scrape_with_http()

        browser_path = self._find_browser()
        if not browser_path:
            logger.error(
                "No Chrome/Edge found. Install Chrome or use --backend http.\n"
                "Falling back to HTTP backend."
            )
            return self._scrape_with_http()

        result = ScrapeResult(seller_name=self.seller_name)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path=browser_path,
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="en-GB",
                extra_http_headers={
                    "Accept-Language": "en-GB,en;q=0.9",
                },
            )

            # Mask automation indicators
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """)

            page = context.new_page()
            page_number = 1

            logger.info("Starting browser scrape for seller: %s", self.seller_name)

            while len(result.listings) < self.max_listings:
                try:
                    params = {
                        "_ssn": self.seller_name,
                        "_pgn": str(page_number),
                        "_sop": "10",
                        "_language": "en",  # Force English listings
                    }
                    url = f"{self.SEARCH_URL}?{urlencode(params)}"
                    logger.info("Navigating to page %d: %s", page_number, url)

                    page.goto(url, wait_until="domcontentloaded", timeout=30000)

                    # Wait for listings or bot detection
                    try:
                        page.wait_for_selector(
                            'li[id^="item"], .s-item, .srp-results, #captcha_form',
                            timeout=15000,
                        )
                    except Exception:
                        pass

                    # Check for CAPTCHA
                    content = page.content()
                    if "Pardon Our Interruption" in content or "captcha" in content.lower():
                        logger.warning("Bot detection triggered. Waiting 15s for auto-resolve...")
                        time.sleep(15)
                        content = page.content()
                        if "Pardon Our Interruption" in content:
                            logger.warning("CAPTCHA persists. Trying with visible browser...")
                            browser.close()
                            # Retry with headed browser so CAPTCHA can be solved
                            browser = pw.chromium.launch(
                                executable_path=browser_path,
                                headless=False,
                            )
                            context = browser.new_context(
                                user_agent=(
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/131.0.0.0 Safari/537.36"
                                ),
                            )
                            context.add_init_script("""
                                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                            """)
                            page = context.new_page()
                            page.goto(url, wait_until="domcontentloaded", timeout=60000)

                            # Wait for user to solve CAPTCHA or for it to auto-resolve
                            logger.info("Waiting up to 60s for CAPTCHA resolution...")
                            try:
                                page.wait_for_selector('li[id^="item"], .s-item', timeout=60000)
                            except Exception:
                                logger.error("CAPTCHA not resolved within 60s.")
                                result.errors.append("CAPTCHA not resolved")
                                break

                    # Extract listings from the page
                    page_listings = self._extract_listings_from_page(page)

                    if not page_listings:
                        logger.info("No listings on page %d, reached end.", page_number)
                        break

                    result.listings.extend(page_listings)
                    result.pages_scraped += 1
                    logger.info(
                        "Page %d: %d listings (total: %d)",
                        page_number, len(page_listings), len(result.listings),
                    )

                    # Check for next page
                    next_btn = page.query_selector(
                        'a.pagination__next, a[aria-label="Go to next search page"], '
                        'a[data-track*="next"], button[aria-label*="next" i]'
                    )
                    if not next_btn:
                        logger.info("No next page, scrape complete.")
                        break

                    page_number += 1
                    time.sleep(self.request_delay)

                except Exception as e:
                    error_msg = f"Error on page {page_number}: {e}"
                    logger.error(error_msg)
                    result.errors.append(error_msg)
                    break

            browser.close()

        result.listings = result.listings[:self.max_listings]
        result.total_listings = len(result.listings)

        logger.info(
            "Browser scrape complete: %d listings from %d pages (%d errors)",
            result.total_listings, result.pages_scraped, len(result.errors),
        )
        return result

    def _extract_listings_from_page(self, page) -> list[EbayListing]:
        """Extract listing data from the current Playwright page using JS evaluation."""
        listings_data = page.evaluate("""
            () => {
                // eBay uses two possible layouts:
                // Legacy: .s-item containers
                // New (2025+): li[id^="item"] with .s-card class

                let items = document.querySelectorAll('li[id^="item"]');
                const isNewLayout = items.length > 0;

                if (!isNewLayout) {
                    items = document.querySelectorAll('.s-item');
                }

                const results = [];

                items.forEach(item => {
                    let title, url, priceText, imageUrl, condition, shipping, hasBids, bidText;

                    if (isNewLayout) {
                        // ── New eBay layout (s-card) ──
                        const titleEl = item.querySelector('.s-card__title span.su-styled-text');
                        title = titleEl ? titleEl.textContent.trim() : '';

                        const linkEl = item.querySelector('a[href*="/itm/"]');
                        url = linkEl ? linkEl.href.split('?')[0] : '';

                        // Price: span with bold + large text inside price area
                        const priceEl = item.querySelector('span.su-styled-text.bold');
                        priceText = priceEl ? priceEl.textContent.trim() : '';

                        const imgEl = item.querySelector('img[src*="ebayimg"]');
                        imageUrl = imgEl ? imgEl.src : '';

                        // Condition: look for secondary info text
                        const conditionEl = item.querySelector('.s-card__subtitle span, .SECONDARY_INFO');
                        condition = conditionEl ? conditionEl.textContent.trim() : '';

                        // Shipping
                        const shippingEl = item.querySelector('[class*="shipping"], [class*="delivery"]');
                        shipping = shippingEl ? shippingEl.textContent.trim() : '';

                        hasBids = false;
                        bidText = '';

                    } else {
                        // ── Legacy layout (.s-item) ──
                        const titleEl = item.querySelector('.s-item__title');
                        title = titleEl ? titleEl.textContent.trim() : '';

                        const linkEl = item.querySelector('.s-item__link');
                        url = linkEl ? linkEl.href.split('?')[0] : '';

                        const priceEl = item.querySelector('.s-item__price');
                        priceText = priceEl ? priceEl.textContent.trim() : '';

                        const imgEl = item.querySelector('.s-item__image-wrapper img');
                        imageUrl = imgEl ? (imgEl.src || imgEl.dataset.src || '') : '';

                        const conditionEl = item.querySelector('.SECONDARY_INFO');
                        condition = conditionEl ? conditionEl.textContent.trim() : '';

                        const shippingEl = item.querySelector('.s-item__shipping, .s-item__freeXDays');
                        shipping = shippingEl ? shippingEl.textContent.trim() : '';

                        const bidEl = item.querySelector('.s-item__bidCount');
                        hasBids = !!bidEl;
                        bidText = bidEl ? bidEl.textContent.trim() : '';
                    }

                    // Clean title
                    title = title.replace(/^(New Listing|SPONSORED)\s*/g, '').trim();

                    // Skip placeholder items
                    if (!title || title === 'Shop on eBay') return;

                    const itemIdMatch = url.match(/\\/itm\\/(\\d+)/);

                    results.push({
                        title: title,
                        listing_url: url,
                        item_id: itemIdMatch ? itemIdMatch[1] : '',
                        price_text: priceText,
                        image_url: imageUrl,
                        condition: condition,
                        shipping: shipping,
                        has_bids: hasBids,
                        bid_text: bidText,
                    });
                });

                return results;
            }
        """)

        listings = []
        for data in listings_data:
            listing = EbayListing(
                item_id=data.get("item_id", ""),
                title=data.get("title", ""),
                price=self._parse_price(data.get("price_text", "")),
                currency=self._detect_currency(data.get("price_text", "")),
                condition=data.get("condition", ""),
                image_url=data.get("image_url", ""),
                listing_url=data.get("listing_url", ""),
                shipping=data.get("shipping", ""),
                listing_type="auction" if data.get("has_bids") else "fixed",
                category_hint=self._guess_category(data.get("title", "")),
                scraped_at=datetime.now(timezone.utc).isoformat(),
            )

            if data.get("has_bids") and data.get("bid_text"):
                bid_match = re.search(r"(\d+)", data["bid_text"])
                if bid_match:
                    listing.bids = int(bid_match.group(1))

            listings.append(listing)

        return listings

    @staticmethod
    def _detect_currency(price_text: str) -> str:
        """Detect currency from price text."""
        if "$" in price_text or "USD" in price_text:
            return "USD"
        elif "€" in price_text or "EUR" in price_text:
            return "EUR"
        return "GBP"

    # ─────────────────────────────────────────
    # HTTP backend (fallback)
    # ─────────────────────────────────────────

    def _get_http_client(self):
        """Lazy-init httpx client."""
        if self._http_client is None:
            import httpx
            self._http_client = httpx.Client(
                headers={
                    "User-Agent": config.HTTP_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-GB,en;q=0.9",
                },
                follow_redirects=True,
                timeout=30.0,
            )
        return self._http_client

    def _scrape_with_http(self) -> ScrapeResult:
        """Scrape using plain HTTP (often blocked by eBay)."""
        import httpx
        from selectolax.parser import HTMLParser

        result = ScrapeResult(seller_name=self.seller_name)
        page_number = 1
        client = self._get_http_client()

        logger.info("Starting HTTP scrape for seller: %s", self.seller_name)
        logger.warning("HTTP scraping may be blocked by eBay bot detection.")

        while len(result.listings) < self.max_listings:
            try:
                params = {
                    "_ssn": self.seller_name,
                    "_pgn": str(page_number),
                    "_sop": "10",
                }
                url = f"{self.SEARCH_URL}?{urlencode(params)}"
                response = client.get(url)
                response.raise_for_status()

                if "Pardon Our Interruption" in response.text[:500]:
                    logger.error("eBay bot detection — HTTP backend blocked.")
                    result.errors.append("Bot detection triggered")
                    break

                tree = HTMLParser(response.text)
                items = tree.css(".s-item")
                page_listings = []

                for item in items:
                    listing = self._parse_http_item(item)
                    if listing and listing.title and listing.title != "Shop on eBay":
                        page_listings.append(listing)

                if not page_listings:
                    break

                result.listings.extend(page_listings)
                result.pages_scraped += 1
                logger.info("Page %d: %d listings", page_number, len(page_listings))

                # Check next page
                next_btn = tree.css_first("a.pagination__next")
                if not next_btn:
                    break

                page_number += 1
                time.sleep(self.request_delay)

            except Exception as e:
                result.errors.append(f"Error on page {page_number}: {e}")
                break

        result.listings = result.listings[:self.max_listings]
        result.total_listings = len(result.listings)
        return result

    def _parse_http_item(self, item) -> Optional[EbayListing]:
        """Parse a single .s-item element from selectolax."""
        listing = EbayListing()

        title_el = item.css_first(".s-item__title")
        if title_el:
            listing.title = title_el.text(strip=True)
            for prefix in ["New Listing", "SPONSORED"]:
                listing.title = listing.title.replace(prefix, "").strip()

        link_el = item.css_first(".s-item__link")
        if link_el:
            listing.listing_url = link_el.attributes.get("href", "")
            match = re.search(r"/itm/(\d+)", listing.listing_url)
            if match:
                listing.item_id = match.group(1)
            if "?" in listing.listing_url:
                listing.listing_url = listing.listing_url.split("?")[0]

        price_el = item.css_first(".s-item__price")
        if price_el:
            price_text = price_el.text(strip=True)
            listing.price = self._parse_price(price_text)
            listing.currency = self._detect_currency(price_text)

        img_el = item.css_first(".s-item__image-wrapper img")
        if img_el:
            listing.image_url = img_el.attributes.get("src", "") or img_el.attributes.get("data-src", "")

        condition_el = item.css_first(".SECONDARY_INFO")
        if condition_el:
            listing.condition = condition_el.text(strip=True)

        shipping_el = item.css_first(".s-item__shipping, .s-item__freeXDays")
        if shipping_el:
            listing.shipping = shipping_el.text(strip=True)

        bid_el = item.css_first(".s-item__bidCount")
        if bid_el:
            listing.listing_type = "auction"
            bid_match = re.search(r"(\d+)", bid_el.text(strip=True))
            if bid_match:
                listing.bids = int(bid_match.group(1))

        listing.category_hint = self._guess_category(listing.title)
        listing.scraped_at = datetime.now(timezone.utc).isoformat()

        return listing

    # ─────────────────────────────────────────
    # Utilities
    # ─────────────────────────────────────────

    @staticmethod
    def _parse_price(text: str) -> float:
        """Extract numeric price from text like '$29.99' or '$10.00 to $25.00'."""
        # Take the first price if it's a range
        match = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
        if match:
            try:
                return float(match.group())
            except ValueError:
                return 0.0
        return 0.0

    @staticmethod
    def _guess_category(title: str) -> str:
        """
        Guess a broad category from the listing title using keyword matching.
        This is a rough heuristic — the content planner will refine it with AI.
        """
        title_lower = title.lower()

        categories = {
            "Video Games": [
                "game", "xbox", "playstation", "nintendo", "ps5", "ps4", "ps3",
                "ps2", "wii", "switch", "gameboy", "sega", "atari", "controller",
                "console", "gaming",
            ],
            "Board Games & Puzzles": [
                "board game", "puzzle", "card game", "dice", "tabletop",
                "monopoly", "chess", "lego",
            ],
            "Tech & Electronics": [
                "phone", "laptop", "tablet", "computer", "monitor", "keyboard",
                "mouse", "headphones", "speaker", "camera", "lens", "drone",
                "charger", "cable", "adapter", "usb", "hdmi", "bluetooth",
                "smart watch", "fitbit", "garmin", "router", "hard drive", "ssd",
                "gpu", "cpu", "ram", "motherboard", "power supply",
            ],
            "Collectibles": [
                "vintage", "antique", "rare", "collectible", "collector",
                "limited edition", "signed", "autograph", "memorabilia",
                "coin", "stamp", "card", "figure", "figurine", "toy",
            ],
            "Books & Media": [
                "book", "novel", "dvd", "blu-ray", "vinyl", "record", "cd",
                "vhs", "comic", "manga", "magazine",
            ],
            "Clothing & Accessories": [
                "shirt", "jacket", "coat", "jeans", "dress", "shoes", "boots",
                "sneakers", "hat", "watch", "jewelry", "ring", "necklace",
                "bracelet", "bag", "purse", "wallet", "sunglasses",
            ],
            "Home & Garden": [
                "lamp", "rug", "curtain", "pillow", "kitchen", "cookware",
                "garden", "tool", "drill", "saw", "wrench",
            ],
            "Sports & Outdoors": [
                "bike", "bicycle", "golf", "fishing", "camping", "hiking",
                "football", "basketball", "baseball", "tennis", "soccer",
                "weights", "fitness", "yoga",
            ],
        }

        for category, keywords in categories.items():
            if any(kw in title_lower for kw in keywords):
                return category

        return "General"

    # ─────────────────────────────────────────
    # Persistence
    # ─────────────────────────────────────────

    @staticmethod
    def save_listings(result: ScrapeResult, output_path: Optional[Path] = None) -> Path:
        """Save scraped listings to a JSON file."""
        if output_path is None:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            output_path = config.DATA_DIR / "listings.json"

        data = {
            "seller_name": result.seller_name,
            "total_listings": result.total_listings,
            "pages_scraped": result.pages_scraped,
            "listings": [asdict(listing) for listing in result.listings],
            "errors": result.errors,
        }

        output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Saved %d listings to %s", result.total_listings, output_path)
        return output_path

    @staticmethod
    def load_listings(input_path: Optional[Path] = None) -> ScrapeResult:
        """Load previously scraped listings from JSON."""
        if input_path is None:
            input_path = config.DATA_DIR / "listings.json"

        data = json.loads(input_path.read_text(encoding="utf-8"))

        result = ScrapeResult(
            seller_name=data["seller_name"],
            total_listings=data["total_listings"],
            pages_scraped=data["pages_scraped"],
            errors=data.get("errors", []),
        )
        result.listings = [EbayListing(**item) for item in data["listings"]]
        return result


# ─────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────

def main():
    """Run the scraper standalone for testing."""
    logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)

    if config.EBAY_SELLER_NAME == "YOUR_SELLER_NAME":
        logger.error(
            "Set your eBay seller name! Either:\n"
            "  - Edit config.py  EBAY_SELLER_NAME\n"
            "  - Set env var:    EBAY_SELLER_NAME=yoursellername"
        )
        return

    with EbayScraper() as scraper:
        result = scraper.scrape_all_listings()
        output = EbayScraper.save_listings(result)

        print(f"\n✅ Scraped {result.total_listings} listings from '{result.seller_name}'")
        print(f"   Saved to: {output}")

        if result.errors:
            print(f"   ⚠️  {len(result.errors)} errors occurred")

        # Print category breakdown
        categories: dict[str, int] = {}
        for listing in result.listings:
            cat = listing.category_hint or "Uncategorized"
            categories[cat] = categories.get(cat, 0) + 1

        print("\n📊 Category breakdown:")
        for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
            print(f"   {cat}: {count}")


if __name__ == "__main__":
    main()
