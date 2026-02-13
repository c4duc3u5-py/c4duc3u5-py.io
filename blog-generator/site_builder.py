"""
Site Builder
=============
Takes generated blog posts and writes them as Hugo-compatible
markdown files into the site content directory.

Also handles:
- Image downloading and local caching
- Sitemap hints
- Index page regeneration
- Cleanup of stale posts for delisted items
"""

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

import config
from ai_writer import GeneratedPost

logger = logging.getLogger(__name__)


class SiteBuilder:
    """
    Manages the Hugo static site content directory.

    Writes AI-generated posts as .md files, downloads product images,
    and maintains the site structure.
    """

    def __init__(self, site_dir: Path = config.SITE_DIR):
        self.site_dir = site_dir
        self.content_dir = site_dir / "content" / "posts"
        self.static_images_dir = site_dir / "static" / "images" / "products"
        self._http = httpx.Client(
            headers={"User-Agent": config.HTTP_USER_AGENT},
            follow_redirects=True,
            timeout=30.0,
        )

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    def publish_post(self, post: GeneratedPost, download_images: bool = True) -> Path:
        """
        Write a generated post to the Hugo content directory.

        Returns the path to the created markdown file.
        """
        self.content_dir.mkdir(parents=True, exist_ok=True)

        # Optionally download and localize images
        markdown = post.content_markdown
        if download_images:
            markdown = self._localize_images(markdown, post.slug)
            # Also localize the featured image
            if post.featured_image:
                post.featured_image = self._download_single_image(
                    post.featured_image, post.slug, "featured"
                )

        # Update the post content with localized images
        post.content_markdown = markdown

        # Write the Hugo markdown file
        output_path = self.content_dir / f"{post.slug}.md"
        hugo_content = post.to_hugo_markdown()
        output_path.write_text(hugo_content, encoding="utf-8")

        logger.info("Published post: %s (%d words)", output_path.name, post.word_count)
        return output_path

    def publish_batch(self, posts: list[GeneratedPost]) -> list[Path]:
        """Publish multiple posts. Returns list of created file paths."""
        paths = []
        for post in posts:
            try:
                path = self.publish_post(post)
                paths.append(path)
            except Exception as e:
                logger.error("Failed to publish '%s': %s", post.title, e)
        return paths

    def get_published_posts(self) -> list[str]:
        """Return slugs of all published posts."""
        if not self.content_dir.exists():
            return []
        return [f.stem for f in self.content_dir.glob("*.md")]

    def get_published_count(self) -> int:
        """Return the number of published posts."""
        return len(self.get_published_posts())

    def cleanup_stale_posts(
        self, active_item_ids: set[str], max_age_days: int = 30
    ) -> list[Path]:
        """
        Remove posts that reference items no longer active on eBay,
        or that are older than max_age_days.

        Returns list of removed file paths.
        """
        removed = []
        if not self.content_dir.exists():
            return removed

        now = datetime.now(timezone.utc)

        for md_file in self.content_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")

                # Check if the post references any active items
                has_active_items = any(
                    item_id in content for item_id in active_item_ids
                )

                # Parse the date from front matter
                date_match = re.search(r"^date:\s*(.+)$", content, re.MULTILINE)
                post_age_days = 0
                if date_match:
                    try:
                        post_date = datetime.fromisoformat(
                            date_match.group(1).strip()
                        )
                        post_age_days = (now - post_date).days
                    except ValueError:
                        pass

                # Remove if no active items AND older than threshold
                if not has_active_items and post_age_days > max_age_days:
                    md_file.unlink()
                    removed.append(md_file)
                    logger.info(
                        "Removed stale post: %s (age: %d days, no active items)",
                        md_file.name,
                        post_age_days,
                    )

            except Exception as e:
                logger.warning("Error checking post %s: %s", md_file.name, e)

        return removed

    # ─────────────────────────────────────────
    # Image Handling
    # ─────────────────────────────────────────

    def _localize_images(self, markdown: str, post_slug: str) -> str:
        """
        Find image URLs in markdown, download them locally,
        and replace URLs with local paths.
        """
        # Match markdown images: ![alt](url)
        img_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

        def replace_image(match):
            alt_text = match.group(1)
            url = match.group(2)

            # Only download external images
            if not url.startswith("http"):
                return match.group(0)

            local_path = self._download_single_image(url, post_slug, alt_text)
            if local_path:
                return f"![{alt_text}]({local_path})"
            return match.group(0)

        return img_pattern.sub(replace_image, markdown)

    def _download_single_image(
        self, url: str, post_slug: str, name_hint: str
    ) -> Optional[str]:
        """
        Download a single image and return its local path relative to /static/.
        Returns None if download fails.
        """
        if not url or not url.startswith("http"):
            return url

        try:
            # Create directory for this post's images
            post_images_dir = self.static_images_dir / post_slug
            post_images_dir.mkdir(parents=True, exist_ok=True)

            # Generate a clean filename
            safe_name = re.sub(r"[^a-zA-Z0-9]", "-", name_hint)[:40].strip("-")
            if not safe_name:
                safe_name = "image"

            # Determine extension from URL
            ext = ".jpg"
            if ".png" in url.lower():
                ext = ".png"
            elif ".webp" in url.lower():
                ext = ".webp"
            elif ".gif" in url.lower():
                ext = ".gif"

            filename = f"{safe_name}{ext}"
            local_file = post_images_dir / filename

            # Skip if already downloaded
            if local_file.exists():
                return f"/images/products/{post_slug}/{filename}"

            # Download the image
            response = self._http.get(url)
            response.raise_for_status()

            local_file.write_bytes(response.content)
            logger.debug("Downloaded image: %s -> %s", url[:80], local_file)

            # Return path relative to Hugo's /static/ directory
            return f"/images/products/{post_slug}/{filename}"

        except Exception as e:
            logger.warning("Failed to download image %s: %s", url[:80], e)
            return None

    # ─────────────────────────────────────────
    # Site Initialization
    # ─────────────────────────────────────────

    def ensure_site_structure(self):
        """
        Ensure the Hugo site has the minimum required directory structure.
        Creates directories and default files if they don't exist.
        """
        dirs = [
            self.site_dir / "content" / "posts",
            self.site_dir / "static" / "images" / "products",
            self.site_dir / "layouts",
            self.site_dir / "themes",
        ]

        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

        # Create _index.md for the posts section if it doesn't exist
        posts_index = self.site_dir / "content" / "posts" / "_index.md"
        if not posts_index.exists():
            posts_index.write_text(
                "---\n"
                "title: \"Latest Posts\"\n"
                "description: \"Browse our latest buyer guides, deals, and product roundups.\"\n"
                "---\n",
                encoding="utf-8",
            )

        # Create homepage _index.md if it doesn't exist
        home_index = self.site_dir / "content" / "_index.md"
        if not home_index.exists():
            home_index.write_text(
                "---\n"
                f"title: \"{config.SITE_TITLE}\"\n"
                f"description: \"{config.SITE_DESCRIPTION}\"\n"
                "---\n",
                encoding="utf-8",
            )

        logger.info("Site structure verified at %s", self.site_dir)
