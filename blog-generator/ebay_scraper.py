"""
eBay Seller Listings Scraper
==============================
Scrapes a seller's active eBay listings from their public seller page.
No API keys required — uses the publicly accessible seller search page.

Extracts: title, price, image URL, listing URL, condition, shipping info.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urljoin

import httpx
from selectolax.parser import HTMLParser

import config

logger = logging.getLogger(__name__)


@dataclass
class EbayListing:
    """Represents a single eBay listing scraped from the seller page."""

    item_id: str = ""
    title: str = ""
    price: float = 0.0
    currency: str = "USD"
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

    Uses selectolax (fast HTML parser) and httpx (modern HTTP client).
    Handles pagination, rate-limiting, and error recovery.
    """

    # Using /sch/m.html (seller search) instead of /sch/i.html
    # because i.html triggers eBay's bot detection more aggressively
    SEARCH_URL = "https://www.ebay.com/sch/m.html"

    def __init__(
        self,
        seller_name: str | None = None,
        max_listings: int | None = None,
        request_delay: float | None = None,
    ):
        self.seller_name = seller_name or config.EBAY_SELLER_NAME
        self.max_listings = max_listings if max_listings is not None else config.EBAY_MAX_LISTINGS
        self.request_delay = request_delay if request_delay is not None else config.EBAY_REQUEST_DELAY
        self._client = httpx.Client(
            headers={
                "User-Agent": config.HTTP_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            },
            follow_redirects=True,
            timeout=30.0,
        )

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    def scrape_all_listings(self) -> ScrapeResult:
        """
        Scrape all active listings from the seller's page.
        Handles pagination automatically.
        """
        result = ScrapeResult(seller_name=self.seller_name)
        page_number = 1

        logger.info("Starting scrape for seller: %s", self.seller_name)

        while len(result.listings) < self.max_listings:
            try:
                html = self._fetch_page(page_number)
                if not html:
                    logger.warning("Empty response for page %d, stopping.", page_number)
                    break

                page_listings = self._parse_listings_page(html)

                if not page_listings:
                    logger.info("No listings found on page %d, reached end.", page_number)
                    break

                result.listings.extend(page_listings)
                result.pages_scraped += 1

                logger.info(
                    "Page %d: found %d listings (total: %d)",
                    page_number,
                    len(page_listings),
                    len(result.listings),
                )

                # Check if there's a next page
                if not self._has_next_page(html):
                    logger.info("No next page link found, scrape complete.")
                    break

                page_number += 1
                time.sleep(self.request_delay)

            except Exception as e:
                error_msg = f"Error on page {page_number}: {e}"
                logger.error(error_msg)
                result.errors.append(error_msg)
                break

        # Trim to max
        result.listings = result.listings[: self.max_listings]
        result.total_listings = len(result.listings)

        logger.info(
            "Scrape complete: %d listings from %d pages (%d errors)",
            result.total_listings,
            result.pages_scraped,
            len(result.errors),
        )

        return result

    # ─────────────────────────────────────────
    # HTTP
    # ─────────────────────────────────────────

    def _fetch_page(self, page_number: int) -> Optional[str]:
        """Fetch a single page of seller listings."""
        params = {
            "_ssn": self.seller_name,
            "_pgn": str(page_number),
            "_sop": "10",  # sort by newly listed
        }

        url = f"{self.SEARCH_URL}?{urlencode(params)}"
        logger.debug("Fetching: %s", url)

        response = self._client.get(url)
        response.raise_for_status()

        # Detect eBay's bot protection page
        if "Pardon Our Interruption" in response.text[:500]:
            logger.warning("eBay returned bot detection page. Retrying after delay...")
            time.sleep(5)
            response = self._client.get(url)
            response.raise_for_status()
            if "Pardon Our Interruption" in response.text[:500]:
                logger.error("eBay bot detection persists — cannot scrape.")
                return None

        return response.text

    # ─────────────────────────────────────────
    # HTML Parsing
    # ─────────────────────────────────────────

    def _parse_listings_page(self, html: str) -> list[EbayListing]:
        """Parse listing cards from a search results page."""
        tree = HTMLParser(html)
        listings = []

        # eBay search results use .s-item containers
        items = tree.css(".s-item")

        for item in items:
            try:
                listing = self._parse_single_item(item)
                if listing and listing.title and listing.title != "Shop on eBay":
                    listings.append(listing)
            except Exception as e:
                logger.debug("Failed to parse item: %s", e)
                continue

        return listings

    def _parse_single_item(self, item) -> Optional[EbayListing]:
        """Parse a single .s-item element into an EbayListing."""
        listing = EbayListing()

        # ── Title & URL ──
        title_el = item.css_first(".s-item__title")
        if title_el:
            # Remove <span> child elements that say "New Listing" etc.
            listing.title = title_el.text(strip=True)
            # Clean common prefixes
            for prefix in ["New Listing", "SPONSORED"]:
                listing.title = listing.title.replace(prefix, "").strip()

        link_el = item.css_first(".s-item__link")
        if link_el:
            listing.listing_url = link_el.attributes.get("href", "")
            # Extract item ID from URL
            item_id_match = re.search(r"/itm/(\d+)", listing.listing_url)
            if item_id_match:
                listing.item_id = item_id_match.group(1)
            # Clean tracking params from URL
            if "?" in listing.listing_url:
                listing.listing_url = listing.listing_url.split("?")[0]

        # ── Price ──
        price_el = item.css_first(".s-item__price")
        if price_el:
            price_text = price_el.text(strip=True)
            listing.price = self._parse_price(price_text)
            if "GBP" in price_text or "£" in price_text:
                listing.currency = "GBP"
            elif "EUR" in price_text or "€" in price_text:
                listing.currency = "EUR"

        # ── Image ──
        img_el = item.css_first(".s-item__image-wrapper img")
        if img_el:
            listing.image_url = (
                img_el.attributes.get("src", "")
                or img_el.attributes.get("data-src", "")
            )

        # ── Condition ──
        condition_el = item.css_first(".SECONDARY_INFO")
        if condition_el:
            listing.condition = condition_el.text(strip=True)

        # ── Shipping ──
        shipping_el = item.css_first(".s-item__shipping, .s-item__freeXDays")
        if shipping_el:
            listing.shipping = shipping_el.text(strip=True)

        # ── Listing type (auction vs fixed) ──
        bid_el = item.css_first(".s-item__bidCount")
        if bid_el:
            listing.listing_type = "auction"
            bid_text = bid_el.text(strip=True)
            bid_match = re.search(r"(\d+)", bid_text)
            if bid_match:
                listing.bids = int(bid_match.group(1))

        # ── Category hint from title ──
        listing.category_hint = self._guess_category(listing.title)

        # ── Timestamp ──
        from datetime import datetime, timezone

        listing.scraped_at = datetime.now(timezone.utc).isoformat()

        return listing

    def _has_next_page(self, html: str) -> bool:
        """Check if there's a next page of results."""
        tree = HTMLParser(html)
        next_btn = tree.css_first(
            "a.pagination__next, "
            "a[aria-label='Go to next search page'], "
            ".pagination__items a[aria-current] + a"
        )
        return next_btn is not None

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
