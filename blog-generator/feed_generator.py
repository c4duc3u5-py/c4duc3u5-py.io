"""
Google Shopping Feed Generator
================================
Generates a Google Merchant Center-compatible product feed from
scraped eBay listings.

The feed is output as an XML file (RSS 2.0 with Google Shopping namespace)
to site/static/shopping-feed.xml so it's hosted on GitHub Pages.

Submit the feed URL to Google Merchant Center (one-time manual step):
  https://c4duc3u5-py.github.io/c4duc3u5-py.io/shopping-feed.xml

Google Merchant Center free listings:
  - Products appear in Google Shopping results for free
  - High purchase-intent traffic
  - Auto-synced every time the pipeline runs

Required fields per Google Merchant Center spec:
  id, title, description, link, image_link, price, availability, condition

Usage:
    # As part of the pipeline (called from main.py):
    generator = ShoppingFeedGenerator()
    result = generator.generate_feed(scrape_result)

    # Standalone:
    python feed_generator.py                     # Generate from cached listings
    python feed_generator.py --validate          # Validate existing feed
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

import config
from ebay_scraper import EbayListing, ScrapeResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────


@dataclass
class FeedResult:
    """Result of feed generation."""

    total_items: int = 0
    items_included: int = 0
    items_skipped: int = 0
    feed_path: Optional[Path] = None
    feed_url: str = ""
    errors: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────
# Google Shopping Feed Generator
# ─────────────────────────────────────────────


class ShoppingFeedGenerator:
    """
    Generates a Google Merchant Center product feed from eBay listings.

    Feed format: RSS 2.0 with Google Shopping (g:) namespace.
    This is the most widely supported format for Merchant Center.

    Google Merchant Center docs:
    https://support.google.com/merchants/answer/7052112
    """

    # Google Shopping namespace
    GOOGLE_NS = "http://base.google.com/ns/1.0"

    # Condition mapping: eBay condition text → Google Shopping condition
    CONDITION_MAP = {
        "new": "new",
        "brand new": "new",
        "new with tags": "new",
        "new with box": "new",
        "new without tags": "new",
        "new without box": "new",
        "new other": "new",
        "zcela nový": "new",
        "nový": "new",
        "refurbished": "refurbished",
        "seller refurbished": "refurbished",
        "certified refurbished": "refurbished",
        "manufacturer refurbished": "refurbished",
        "remanufactured": "refurbished",
        "used": "used",
        "pre-owned": "used",
        "good": "used",
        "very good": "used",
        "acceptable": "used",
        "for parts": "used",
        "použitý": "used",
        "open box": "used",
    }

    def __init__(
        self,
        site_base_url: str = "",
        output_dir: Optional[Path] = None,
    ):
        self.site_base_url = (site_base_url or config.SITE_BASE_URL).rstrip("/")
        self.output_dir = output_dir or (config.SITE_DIR / "static")

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    def generate_feed(self, scrape_result: ScrapeResult) -> FeedResult:
        """
        Generate a Google Shopping XML feed from scraped listings.

        Args:
            scrape_result: The scraped eBay listings data.

        Returns:
            FeedResult with feed path and item counts.
        """
        result = FeedResult(total_items=len(scrape_result.listings))

        logger.info(
            "Generating Google Shopping feed from %d listings...",
            result.total_items,
        )

        # Build the XML feed
        rss = self._build_feed_xml(scrape_result)

        # Write the feed file
        self.output_dir.mkdir(parents=True, exist_ok=True)
        feed_path = self.output_dir / "shopping-feed.xml"

        tree = ElementTree(rss)
        indent(tree, space="  ")

        with open(feed_path, "wb") as f:
            tree.write(f, encoding="utf-8", xml_declaration=True)

        result.feed_path = feed_path
        result.feed_url = f"{self.site_base_url}/shopping-feed.xml"
        result.items_included = self._count_items(rss)
        result.items_skipped = result.total_items - result.items_included

        logger.info(
            "✅ Google Shopping feed generated: %d items (%d skipped)",
            result.items_included,
            result.items_skipped,
        )
        logger.info("   Feed path: %s", feed_path)
        logger.info("   Feed URL:  %s", result.feed_url)

        return result

    def generate_from_cached(self) -> FeedResult:
        """
        Generate a feed from the cached listings.json file.
        Convenience method for standalone use.
        """
        from ebay_scraper import EbayScraper

        try:
            scrape_result = EbayScraper.load_listings()
            return self.generate_feed(scrape_result)
        except FileNotFoundError:
            logger.error("No cached listings found. Run the scraper first.")
            return FeedResult(errors=["No cached listings found"])

    # ─────────────────────────────────────────
    # XML Feed Builder
    # ─────────────────────────────────────────

    def _build_feed_xml(self, scrape_result: ScrapeResult) -> Element:
        """Build the RSS 2.0 XML feed with Google Shopping namespace."""
        # Root RSS element with Google namespace
        rss = Element("rss")
        rss.set("version", "2.0")
        rss.set("xmlns:g", self.GOOGLE_NS)

        channel = SubElement(rss, "channel")

        # Channel metadata
        SubElement(channel, "title").text = f"{config.SITE_TITLE} — Product Feed"
        SubElement(channel, "link").text = self.site_base_url
        SubElement(channel, "description").text = (
            f"Product feed for Google Merchant Center. "
            f"Curated items from {config.EBAY_SELLER_NAME} on eBay UK."
        )

        # Add each listing as an <item>
        for listing in scrape_result.listings:
            item_el = self._build_item_xml(listing, channel)
            if item_el is None:
                continue  # Skipped (missing required fields)

        return rss

    def _build_item_xml(
        self, listing: EbayListing, parent: Element
    ) -> Optional[Element]:
        """
        Build a single <item> element for the Google Shopping feed.

        Required Google fields:
          g:id, g:title, g:description, g:link, g:image_link,
          g:price, g:availability, g:condition

        Returns None if the listing is missing required data.
        """
        # Validate required fields
        if not listing.item_id or not listing.title:
            return None
        if not listing.listing_url:
            return None
        if listing.price <= 0:
            return None

        item = SubElement(parent, "item")

        # ── Required Fields ──

        # g:id — Unique product identifier
        self._g_sub(item, "id", listing.item_id)

        # g:title — Product title (max 150 chars)
        clean_title = self._clean_text(listing.title)[:150]
        self._g_sub(item, "title", clean_title)

        # g:description — Product description (use title + condition + category as fallback)
        description = self._build_description(listing)
        self._g_sub(item, "description", description)

        # g:link — URL to the product page
        # Link to our blog post if one exists, otherwise direct to eBay
        blog_url = self._find_blog_post_url(listing)
        self._g_sub(item, "link", blog_url or listing.listing_url)

        # g:image_link — Product image URL
        if listing.image_url and listing.image_url.startswith("http"):
            # Upgrade to higher resolution eBay image
            image_url = self._upgrade_image_url(listing.image_url)
            self._g_sub(item, "image_link", image_url)
        else:
            return None  # Google requires an image

        # g:price — Price with currency
        currency = listing.currency or "GBP"
        self._g_sub(item, "price", f"{listing.price:.2f} {currency}")

        # g:availability — In stock / out of stock
        self._g_sub(item, "availability", "in_stock")

        # g:condition — new / used / refurbished
        condition = self._map_condition(listing.condition)
        self._g_sub(item, "condition", condition)

        # ── Recommended Fields ──

        # g:brand — Use the site title as a brand (we're a curated store)
        self._g_sub(item, "brand", config.SITE_TITLE)

        # g:product_type — Category path for Google
        if listing.category_hint and listing.category_hint != "General":
            self._g_sub(item, "product_type", listing.category_hint)

        # g:identifier_exists — No GTINs/MPNs for most secondhand/niche items
        self._g_sub(item, "identifier_exists", "false")

        # g:shipping — Free shipping or cost info
        if listing.shipping:
            shipping_el = SubElement(item, f"{{{self.GOOGLE_NS}}}shipping")
            self._g_sub(shipping_el, "country", "GB")
            if "free" in listing.shipping.lower():
                self._g_sub(shipping_el, "price", "0.00 GBP")
            else:
                # Try to extract shipping cost
                price_match = re.search(r"[\d,.]+", listing.shipping)
                if price_match:
                    try:
                        ship_price = float(price_match.group().replace(",", ""))
                        self._g_sub(shipping_el, "price", f"{ship_price:.2f} GBP")
                    except ValueError:
                        pass

        return item

    # ─────────────────────────────────────────
    # Helper Methods
    # ─────────────────────────────────────────

    def _g_sub(self, parent: Element, tag: str, text: str) -> Element:
        """Create a sub-element in the Google Shopping namespace."""
        el = SubElement(parent, f"{{{self.GOOGLE_NS}}}{tag}")
        el.text = text
        return el

    @staticmethod
    def _clean_text(text: str) -> str:
        """Clean text for XML output — remove HTML entities and control chars."""
        # Unescape any HTML entities
        text = html.unescape(text)
        # Remove control characters
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        # Normalize whitespace
        text = " ".join(text.split())
        return text.strip()

    def _build_description(self, listing: EbayListing) -> str:
        """
        Build a product description for the feed.
        Google requires at least a minimal description.
        """
        parts = [self._clean_text(listing.title)]

        if listing.condition:
            parts.append(f"Condition: {listing.condition}")

        if listing.category_hint and listing.category_hint != "General":
            parts.append(f"Category: {listing.category_hint}")

        if listing.shipping:
            parts.append(listing.shipping)

        desc = ". ".join(parts)

        # Google recommends 500-5000 chars; minimum is ~1 sentence
        if len(desc) < 50:
            desc += f". Available from {config.SITE_TITLE} — curated deals on eBay UK."

        return desc[:5000]

    def _map_condition(self, condition_text: str) -> str:
        """Map eBay condition text to Google Shopping condition values."""
        if not condition_text:
            return "used"  # Default to 'used' for safety (Google prefers specificity)

        clean = condition_text.lower().strip()

        # Direct lookup
        if clean in self.CONDITION_MAP:
            return self.CONDITION_MAP[clean]

        # Partial matching
        for key, value in self.CONDITION_MAP.items():
            if key in clean:
                return value

        return "used"  # Safe default

    def _find_blog_post_url(self, listing: EbayListing) -> Optional[str]:
        """
        Check if we have a blog post for this listing.
        If so, return the blog post URL (better for SEO than direct eBay link).
        """
        content_dir = config.CONTENT_DIR
        if not content_dir.exists():
            return None

        # Check if any post contains this item's eBay URL
        if not listing.item_id:
            return None

        for md_file in content_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                if listing.item_id in content:
                    slug = md_file.stem
                    return f"{self.site_base_url}/{slug}/"
            except Exception:
                continue

        return None

    @staticmethod
    def _upgrade_image_url(url: str) -> str:
        """Upgrade eBay image URL to a higher resolution version."""
        # eBay images support different sizes via URL patterns
        url = url.replace("s-l225.", "s-l500.")
        url = url.replace("s-l140.", "s-l500.")
        url = url.replace("s-l96.", "s-l500.")
        url = url.replace("s-l64.", "s-l500.")
        return url

    @staticmethod
    def _count_items(rss: Element) -> int:
        """Count the number of <item> elements in the feed."""
        channel = rss.find("channel")
        if channel is None:
            return 0
        return len(channel.findall("item"))


# ─────────────────────────────────────────────
# Convenience function for pipeline integration
# ─────────────────────────────────────────────


def generate_shopping_feed(scrape_result: ScrapeResult) -> FeedResult:
    """
    One-call convenience function to generate the Google Shopping feed.
    Called from main.py as part of the pipeline.
    """
    generator = ShoppingFeedGenerator()
    return generator.generate_feed(scrape_result)


# ─────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────


def main():
    """Run the feed generator standalone."""
    import argparse

    logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)

    parser = argparse.ArgumentParser(
        description="Google Shopping Feed Generator — create a Merchant Center product feed."
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate an existing feed file",
    )

    args = parser.parse_args()

    if args.validate:
        feed_path = config.SITE_DIR / "static" / "shopping-feed.xml"
        if not feed_path.exists():
            print("❌ No feed file found. Generate one first.")
            return

        # Basic validation: parse and count items
        from xml.etree.ElementTree import parse
        tree = parse(feed_path)
        root = tree.getroot()
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else []
        print(f"\n✅ Feed is valid XML with {len(items)} items.")
        print(f"   Path: {feed_path}")
        print(f"   URL:  {config.SITE_BASE_URL}shopping-feed.xml")

    else:
        generator = ShoppingFeedGenerator()
        result = generator.generate_from_cached()

        if result.feed_path:
            print(f"\n✅ Generated feed: {result.items_included} items")
            print(f"   Skipped: {result.items_skipped} items (missing required data)")
            print(f"   Path: {result.feed_path}")
            print(f"   URL:  {result.feed_url}")
            print(
                f"\n📋 Next step: Submit this URL to Google Merchant Center:\n"
                f"   {result.feed_url}"
            )
        else:
            print("❌ Feed generation failed.")
            for error in result.errors:
                print(f"   {error}")


if __name__ == "__main__":
    main()
