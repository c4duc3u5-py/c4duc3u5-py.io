"""
IndexNow Search Engine Notifier
=================================
Pings search engines immediately when new blog posts are published.

Supports:
  - **IndexNow API** (Bing, Yandex, DuckDuckGo, Seznam, Naver)
  - **Google Indexing API** (optional, requires service account)

IndexNow is a simple protocol: POST a list of URLs and a key.
The key must also be hosted as a static file on the site.

Usage:
    # As part of the pipeline (called from main.py):
    notifier = IndexNowNotifier()
    result = notifier.notify_urls(["https://example.com/my-post/"])

    # Standalone:
    python index_now.py --urls https://example.com/post1/ https://example.com/post2/
    python index_now.py --sitemap  # Submit all URLs from sitemap.xml

Setup:
    1. The pipeline auto-generates an IndexNow API key
    2. The key file is placed in site/static/ so it's hosted at your domain root
    3. No registration needed — IndexNow is free and open
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────


@dataclass
class NotifyResult:
    """Result of a search engine notification batch."""

    total_urls: int = 0
    indexnow_submitted: int = 0
    indexnow_errors: list[str] = field(default_factory=list)
    google_submitted: int = 0
    google_errors: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────
# IndexNow Notifier
# ─────────────────────────────────────────────


class IndexNowNotifier:
    """
    Notifies search engines about new or updated URLs using the IndexNow protocol.

    IndexNow endpoints (any one will fan out to all participating engines):
      - https://api.indexnow.org/indexnow  (canonical)
      - https://www.bing.com/indexnow
      - https://yandex.com/indexnow

    The API key must be hosted at: {site_base_url}/{key}.txt
    This class auto-generates and places the key file.
    """

    INDEXNOW_ENDPOINTS = [
        "https://api.indexnow.org/indexnow",
    ]

    # Max URLs per IndexNow batch request
    MAX_URLS_PER_BATCH = 10_000

    def __init__(
        self,
        site_base_url: str = "",
        api_key: str = "",
    ):
        self.site_base_url = (site_base_url or config.SITE_BASE_URL).rstrip("/")
        self.api_key = api_key or config.INDEXNOW_API_KEY
        self._client = httpx.Client(
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=30.0,
        )

        # Auto-generate key if not set
        if not self.api_key:
            self.api_key = self._generate_api_key()

        # Ensure the key file exists in the site's static directory
        self._ensure_key_file()

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    def notify_new_posts(self, post_slugs: list[str]) -> NotifyResult:
        """
        Notify search engines about newly published blog posts.

        Args:
            post_slugs: List of post slugs (e.g., ["my-new-post", "another-post"])

        Returns:
            NotifyResult with submission counts and any errors.
        """
        if not post_slugs:
            logger.info("No new posts to notify search engines about.")
            return NotifyResult()

        # Build full URLs from slugs
        urls = [f"{self.site_base_url}/{slug}/" for slug in post_slugs]

        # Also notify about the homepage and sitemap (they link to new content)
        urls.append(f"{self.site_base_url}/")
        urls.append(f"{self.site_base_url}/posts/")
        urls.append(f"{self.site_base_url}/sitemap.xml")

        return self.notify_urls(urls)

    def notify_urls(self, urls: list[str]) -> NotifyResult:
        """
        Notify search engines about a list of URLs.

        Args:
            urls: Full URLs to submit.

        Returns:
            NotifyResult with submission counts and any errors.
        """
        result = NotifyResult(total_urls=len(urls))

        if not urls:
            return result

        # Deduplicate
        urls = list(dict.fromkeys(urls))
        result.total_urls = len(urls)

        logger.info("Notifying search engines about %d URLs...", len(urls))

        # ── IndexNow ──
        if config.INDEXNOW_ENABLED:
            self._submit_indexnow(urls, result)
        else:
            logger.info("IndexNow disabled (set INDEXNOW_ENABLED=true in .env)")

        # ── Google Indexing API (optional) ──
        if config.GOOGLE_INDEXING_ENABLED:
            self._submit_google(urls, result)
        else:
            logger.debug("Google Indexing API disabled (optional — requires service account)")

        return result

    def notify_sitemap(self) -> NotifyResult:
        """
        Submit all URLs from the sitemap.xml to search engines.
        Useful for initial indexing or re-indexing after major changes.
        """
        sitemap_url = f"{self.site_base_url}/sitemap.xml"
        logger.info("Fetching sitemap: %s", sitemap_url)

        try:
            response = self._client.get(sitemap_url)
            response.raise_for_status()

            # Parse URLs from sitemap XML
            import re
            urls = re.findall(r"<loc>([^<]+)</loc>", response.text)
            logger.info("Found %d URLs in sitemap.", len(urls))

            return self.notify_urls(urls)

        except Exception as e:
            logger.error("Failed to fetch sitemap: %s", e)
            return NotifyResult()

    # ─────────────────────────────────────────
    # IndexNow Submission
    # ─────────────────────────────────────────

    def _submit_indexnow(self, urls: list[str], result: NotifyResult):
        """Submit URLs via the IndexNow protocol."""
        # IndexNow supports batch submission (up to 10,000 URLs)
        for i in range(0, len(urls), self.MAX_URLS_PER_BATCH):
            batch = urls[i : i + self.MAX_URLS_PER_BATCH]

            payload = {
                "host": self._extract_host(self.site_base_url),
                "key": self.api_key,
                "keyLocation": f"{self.site_base_url}/{self.api_key}.txt",
                "urlList": batch,
            }

            for endpoint in self.INDEXNOW_ENDPOINTS:
                try:
                    logger.info(
                        "  Submitting %d URLs to %s...",
                        len(batch),
                        endpoint.split("/")[2],
                    )

                    response = self._client.post(endpoint, json=payload)

                    # IndexNow returns:
                    #   200 = URLs submitted successfully
                    #   202 = Accepted, will be processed later
                    #   400 = Bad request (invalid key or format)
                    #   403 = Key not valid (key file not found at keyLocation)
                    #   422 = Unprocessable (URLs don't match host)
                    #   429 = Too many requests

                    if response.status_code in (200, 202):
                        result.indexnow_submitted += len(batch)
                        logger.info(
                            "  ✅ IndexNow accepted %d URLs (HTTP %d)",
                            len(batch),
                            response.status_code,
                        )
                        break  # Only need one endpoint to succeed
                    else:
                        error = (
                            f"IndexNow {endpoint.split('/')[2]} returned "
                            f"HTTP {response.status_code}: {response.text[:200]}"
                        )
                        result.indexnow_errors.append(error)
                        logger.warning("  ⚠️  %s", error)

                except Exception as e:
                    error = f"IndexNow {endpoint.split('/')[2]} error: {e}"
                    result.indexnow_errors.append(error)
                    logger.warning("  ⚠️  %s", error)

    # ─────────────────────────────────────────
    # Google Indexing API (optional)
    # ─────────────────────────────────────────

    def _submit_google(self, urls: list[str], result: NotifyResult):
        """
        Submit URLs to Google's Indexing API.

        Requires:
        - Google Cloud service account with Indexing API enabled
        - Service account JSON key file path in GOOGLE_SERVICE_ACCOUNT_PATH
        - Site verified in Google Search Console with the service account

        This is OPTIONAL — IndexNow alone covers Bing, Yandex, DuckDuckGo, etc.
        Google's own Indexing API is the only way to ping Google directly.
        """
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError:
            logger.info(
                "Google API client not installed — skipping Google Indexing API. "
                "Install with: pip install google-api-python-client google-auth"
            )
            return

        sa_path = config.GOOGLE_SERVICE_ACCOUNT_PATH
        if not sa_path or not Path(sa_path).exists():
            logger.info(
                "Google service account not configured — skipping Google Indexing API."
            )
            return

        try:
            credentials = service_account.Credentials.from_service_account_file(
                sa_path,
                scopes=["https://www.googleapis.com/auth/indexing"],
            )
            service = build("indexing", "v3", credentials=credentials)

            for url in urls[:200]:  # Google has a daily quota (~200/day)
                try:
                    body = {"url": url, "type": "URL_UPDATED"}
                    service.urlNotifications().publish(body=body).execute()
                    result.google_submitted += 1
                    logger.debug("  Google Indexing API: submitted %s", url)

                    # Small delay to respect rate limits
                    time.sleep(0.5)

                except Exception as e:
                    error = f"Google Indexing API error for {url}: {e}"
                    result.google_errors.append(error)
                    logger.warning("  %s", error)

            logger.info(
                "  ✅ Google Indexing API: submitted %d URLs",
                result.google_submitted,
            )

        except Exception as e:
            error = f"Google Indexing API setup error: {e}"
            result.google_errors.append(error)
            logger.error("  ❌ %s", error)

    # ─────────────────────────────────────────
    # Key Management
    # ─────────────────────────────────────────

    def _generate_api_key(self) -> str:
        """
        Generate a deterministic IndexNow API key from the site URL.
        This ensures the same key is generated every run without storing state.
        """
        raw = f"indexnow-{self.site_base_url}-v1"
        key = hashlib.sha256(raw.encode()).hexdigest()[:32]
        logger.debug("Generated IndexNow API key: %s", key)
        return key

    def _ensure_key_file(self):
        """
        Place the IndexNow API key file in site/static/ so it's served
        at {site_base_url}/{key}.txt after Hugo builds.

        IndexNow requires this file to verify ownership.
        """
        key_file = config.SITE_DIR / "static" / f"{self.api_key}.txt"

        if key_file.exists():
            # Verify the content matches
            existing = key_file.read_text(encoding="utf-8").strip()
            if existing == self.api_key:
                logger.debug("IndexNow key file already exists: %s", key_file)
                return

        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(self.api_key, encoding="utf-8")
        logger.info("Created IndexNow key file: %s", key_file.name)

    @staticmethod
    def _extract_host(url: str) -> str:
        """Extract the hostname from a URL."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.hostname or ""


# ─────────────────────────────────────────────
# Convenience function for pipeline integration
# ─────────────────────────────────────────────


def notify_search_engines(post_slugs: list[str]) -> NotifyResult:
    """
    One-call convenience function to notify all search engines.
    Called from main.py after posts are published.
    """
    with IndexNowNotifier() as notifier:
        return notifier.notify_new_posts(post_slugs)


# ─────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────


def main():
    """Run the notifier standalone for testing or manual submission."""
    import argparse

    logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)

    parser = argparse.ArgumentParser(
        description="IndexNow — Notify search engines about new/updated URLs."
    )
    parser.add_argument(
        "--urls",
        nargs="+",
        help="Specific URLs to submit",
    )
    parser.add_argument(
        "--sitemap",
        action="store_true",
        help="Submit all URLs from the site's sitemap.xml",
    )
    parser.add_argument(
        "--show-key",
        action="store_true",
        help="Show the current IndexNow API key",
    )

    args = parser.parse_args()

    with IndexNowNotifier() as notifier:
        if args.show_key:
            print(f"\nIndexNow API key: {notifier.api_key}")
            print(f"Key file URL: {notifier.site_base_url}/{notifier.api_key}.txt")
            print(f"Key file path: {config.SITE_DIR / 'static' / f'{notifier.api_key}.txt'}")

        elif args.sitemap:
            result = notifier.notify_sitemap()
            print(f"\n✅ Submitted {result.indexnow_submitted} URLs via IndexNow")
            if result.google_submitted:
                print(f"   + {result.google_submitted} URLs via Google Indexing API")
            if result.indexnow_errors:
                print(f"   ⚠️  {len(result.indexnow_errors)} IndexNow errors")

        elif args.urls:
            result = notifier.notify_urls(args.urls)
            print(f"\n✅ Submitted {result.indexnow_submitted} URLs via IndexNow")

        else:
            parser.print_help()


if __name__ == "__main__":
    main()
