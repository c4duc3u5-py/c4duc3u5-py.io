"""
Pinterest Auto-Pinner
======================
Creates Pinterest pins from generated blog posts.

For each new blog post, this module:
1. Takes the featured product image
2. Adds a text overlay (title + price range) using Pillow
3. Uploads the pin to a Pinterest board via API v5
4. Links the pin back to the live blog post URL

The result is search-indexed pins on Pinterest that drive traffic
to your blog posts, which in turn link to your eBay listings.

Requirements:
    - Pinterest Business account (free)
    - App created at https://developers.pinterest.com
    - Access token with pins:write + boards:read scopes
    - Board ID (use list_boards() to find yours)

Setup:
    1. Create a Pinterest Business account
    2. Go to https://developers.pinterest.com/apps/
    3. Create an app → generate an access token
    4. Set PINTEREST_TOKEN and PINTEREST_BOARD_ID in your .env
"""

import base64
import io
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import httpx

import config
from ai_writer import GeneratedPost

logger = logging.getLogger(__name__)

# Optional Pillow import — overlays are skipped if not installed
try:
    from PIL import Image, ImageDraw, ImageFont

    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False
    logger.info(
        "Pillow not installed — pin image overlays disabled. "
        "Install with: pip install Pillow"
    )

if TYPE_CHECKING:
    from PIL import Image, ImageDraw, ImageFont


# ─────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────


@dataclass
class PinResult:
    """Result of a single pin creation attempt."""

    post_slug: str = ""
    pin_id: str = ""
    pin_url: str = ""
    board_id: str = ""
    success: bool = False
    error: str = ""
    created_at: str = ""


@dataclass
class PinBatchResult:
    """Result of pinning a batch of posts."""

    total_attempted: int = 0
    total_created: int = 0
    total_failed: int = 0
    results: list[PinResult] = field(default_factory=list)


# ─────────────────────────────────────────────
# Image Overlay
# ─────────────────────────────────────────────


class PinImageCreator:
    """
    Creates Pinterest-optimized images with text overlays.

    Pinterest best practices:
    - 2:3 aspect ratio (1000×1500 pixels ideal)
    - Bold, readable text overlay
    - High contrast for mobile readability
    """

    # Pinterest recommended pin size
    PIN_WIDTH = 1000
    PIN_HEIGHT = 1500

    # Overlay styling
    OVERLAY_COLOR = (0, 0, 0, 160)  # semi-transparent black
    TEXT_COLOR = (255, 255, 255)     # white text
    ACCENT_COLOR = (255, 193, 7)    # gold accent for price/CTA

    def __init__(self):
        if not PILLOW_AVAILABLE:
            logger.warning("Pillow not available — image overlays disabled.")

    def create_pin_image(
        self,
        source_image_path: Optional[Path] = None,
        source_image_url: Optional[str] = None,
        title: str = "",
        subtitle: str = "",
        output_path: Optional[Path] = None,
    ) -> Optional[Path]:
        """
        Create a Pinterest-optimized image with text overlay.

        Args:
            source_image_path: Local path to the product image
            source_image_url: URL to download the product image from
            title: Main overlay text (e.g. "Best Vintage Cameras")
            subtitle: Secondary text (e.g. "Under $100 • Free Shipping")
            output_path: Where to save the result

        Returns:
            Path to the created image, or None if creation failed.
        """
        if not PILLOW_AVAILABLE:
            return None

        try:
            # Load the source image
            img = self._load_image(source_image_path, source_image_url)
            if img is None:
                # Create a gradient placeholder if no image available
                img = self._create_gradient_background()

            # Resize and crop to Pinterest's 2:3 ratio
            img = self._fit_to_pin_size(img)

            # Add the text overlay
            img = self._add_text_overlay(img, title, subtitle)

            # Save the result
            if output_path is None:
                output_path = Path(config.CACHE_DIR / "pins")
                output_path.mkdir(parents=True, exist_ok=True)
                safe_name = re.sub(r"[^a-zA-Z0-9]", "-", title)[:50].strip("-")
                output_path = output_path / f"{safe_name}.jpg"

            output_path.parent.mkdir(parents=True, exist_ok=True)
            img.convert("RGB").save(output_path, "JPEG", quality=90)
            logger.info("Created pin image: %s", output_path)
            return output_path

        except Exception as e:
            logger.warning("Failed to create pin image: %s", e)
            return None

    def _load_image(
        self,
        path: Optional[Path] = None,
        url: Optional[str] = None,
    ) -> Optional[Image.Image]:
        """Load an image from a local path or URL."""
        if path and path.exists():
            return Image.open(path)

        if url and url.startswith("http"):
            try:
                response = httpx.get(
                    url,
                    headers={"User-Agent": config.HTTP_USER_AGENT},
                    follow_redirects=True,
                    timeout=15.0,
                )
                response.raise_for_status()
                return Image.open(io.BytesIO(response.content))
            except Exception as e:
                logger.warning("Failed to download image from %s: %s", url[:80], e)

        return None

    def _create_gradient_background(self) -> Image.Image:
        """Create a dark gradient background as fallback."""
        img = Image.new("RGB", (self.PIN_WIDTH, self.PIN_HEIGHT))
        draw = ImageDraw.Draw(img)

        # Dark blue to darker blue gradient
        for y in range(self.PIN_HEIGHT):
            ratio = y / self.PIN_HEIGHT
            r = int(15 + ratio * 10)
            g = int(23 + ratio * 15)
            b = int(42 + ratio * 30)
            draw.line([(0, y), (self.PIN_WIDTH, y)], fill=(r, g, b))

        return img

    def _fit_to_pin_size(self, img: Image.Image) -> Image.Image:
        """
        Resize and crop the image to Pinterest's 2:3 aspect ratio.
        Centers the crop on the image.
        """
        target_ratio = self.PIN_WIDTH / self.PIN_HEIGHT  # 0.667

        # Current aspect ratio
        current_ratio = img.width / img.height

        if current_ratio > target_ratio:
            # Image is too wide — crop sides
            new_width = int(img.height * target_ratio)
            left = (img.width - new_width) // 2
            img = img.crop((left, 0, left + new_width, img.height))
        elif current_ratio < target_ratio:
            # Image is too tall — crop top/bottom
            new_height = int(img.width / target_ratio)
            top = (img.height - new_height) // 2
            img = img.crop((0, top, img.width, top + new_height))

        # Resize to target dimensions
        img = img.resize((self.PIN_WIDTH, self.PIN_HEIGHT), Image.LANCZOS)
        return img

    def _add_text_overlay(
        self,
        img: Image.Image,
        title: str,
        subtitle: str,
    ) -> Image.Image:
        """Add a semi-transparent overlay with title and subtitle text."""
        # Convert to RGBA for transparency support
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        # Create overlay layer
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Draw semi-transparent gradient overlay at bottom
        overlay_height = self.PIN_HEIGHT // 3
        overlay_top = self.PIN_HEIGHT - overlay_height

        for y in range(overlay_top, self.PIN_HEIGHT):
            progress = (y - overlay_top) / overlay_height
            alpha = int(40 + progress * 180)
            draw.line(
                [(0, y), (self.PIN_WIDTH, y)],
                fill=(0, 0, 0, alpha),
            )

        # Also add a lighter overlay at the top for branding
        for y in range(0, 80):
            alpha = int(120 * (1 - y / 80))
            draw.line(
                [(0, y), (self.PIN_WIDTH, y)],
                fill=(0, 0, 0, alpha),
            )

        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)

        # Load fonts (fall back to default if system fonts unavailable)
        title_font = self._get_font(size=config.PINTEREST_OVERLAY_FONT_SIZE)
        subtitle_font = self._get_font(size=config.PINTEREST_OVERLAY_FONT_SIZE - 12)
        brand_font = self._get_font(size=22)

        # Draw brand name at top
        brand_text = config.SITE_TITLE
        draw.text(
            (40, 25),
            brand_text,
            font=brand_font,
            fill=self.ACCENT_COLOR,
        )

        # Draw title text (word-wrapped)
        margin = 50
        max_text_width = self.PIN_WIDTH - (margin * 2)
        title_lines = self._wrap_text(title, title_font, max_text_width)

        # Position title near the bottom
        line_height = config.PINTEREST_OVERLAY_FONT_SIZE + 8
        total_text_height = len(title_lines) * line_height + 50  # +50 for subtitle
        text_y = self.PIN_HEIGHT - total_text_height - 80

        for line in title_lines:
            draw.text(
                (margin, text_y),
                line,
                font=title_font,
                fill=self.TEXT_COLOR,
            )
            text_y += line_height

        # Draw subtitle (price range, CTA)
        if subtitle:
            text_y += 15
            draw.text(
                (margin, text_y),
                subtitle,
                font=subtitle_font,
                fill=self.ACCENT_COLOR,
            )

        return img

    def _get_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """Load a font, falling back to default if system fonts aren't available."""
        # Try common Windows fonts first, then Linux, then default
        font_paths = [
            "C:/Windows/Fonts/arialbd.ttf",       # Windows bold
            "C:/Windows/Fonts/arial.ttf",          # Windows regular
            "C:/Windows/Fonts/segoeui.ttf",        # Windows Segoe
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",              # Arch
            "/System/Library/Fonts/Helvetica.ttc",                    # macOS
        ]

        for font_path in font_paths:
            try:
                return ImageFont.truetype(font_path, size)
            except (OSError, IOError):
                continue

        # Last resort: Pillow's built-in font (small, but works)
        logger.debug("No system fonts found — using Pillow default font.")
        return ImageFont.load_default()

    @staticmethod
    def _wrap_text(
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
    ) -> list[str]:
        """Word-wrap text to fit within max_width pixels."""
        words = text.split()
        lines = []
        current_line = ""

        for word in words:
            test_line = f"{current_line} {word}".strip()
            # Use getbbox for modern Pillow, fall back to getlength
            try:
                bbox = font.getbbox(test_line)
                text_width = bbox[2] - bbox[0]
            except AttributeError:
                text_width = font.getlength(test_line)

            if text_width <= max_width:
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word

        if current_line:
            lines.append(current_line)

        return lines or [text]  # fallback to unsplit text


# ─────────────────────────────────────────────
# Pinterest API Client
# ─────────────────────────────────────────────


class PinterestPinner:
    """
    Creates Pinterest pins from generated blog posts.

    Uses Pinterest API v5 to:
    1. Create pin images with text overlays (optional)
    2. Upload pins with links back to blog posts
    3. Track what's been pinned to avoid duplicates
    """

    API_BASE = "https://api.pinterest.com/v5"

    def __init__(
        self,
        access_token: str = "",
        board_id: str = "",
    ):
        self.access_token = access_token or config.PINTEREST_TOKEN
        self.board_id = board_id or config.PINTEREST_BOARD_ID
        self.image_creator = PinImageCreator()

        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

        # Track already-pinned posts to avoid duplicates
        self._pinned_slugs: set[str] = set()
        self._load_pin_history()

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    def pin_post(self, post: GeneratedPost) -> PinResult:
        """
        Create a Pinterest pin for a single blog post.

        Steps:
        1. Check if already pinned (skip if so)
        2. Create an overlay image (if Pillow available)
        3. Upload to Pinterest via API v5
        4. Record in pin history
        """
        result = PinResult(
            post_slug=post.slug,
            board_id=self.board_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Skip if already pinned
        if post.slug in self._pinned_slugs:
            logger.info("Already pinned: '%s' — skipping.", post.title)
            result.error = "already_pinned"
            return result

        # Validate we have credentials
        if not self.access_token or not self.board_id:
            result.error = "missing_credentials"
            logger.warning(
                "Pinterest credentials not configured. "
                "Set PINTEREST_TOKEN and PINTEREST_BOARD_ID in .env"
            )
            return result

        try:
            # Build the blog post URL
            post_url = self._build_post_url(post.slug)

            # Generate pin description
            description = self._build_pin_description(post)

            # Try to create an overlay image
            pin_image_path = None
            if PILLOW_AVAILABLE and config.PINTEREST_OVERLAY_ENABLED:
                subtitle = self._build_subtitle(post)
                pin_image_path = self.image_creator.create_pin_image(
                    source_image_url=post.featured_image,
                    title=post.title,
                    subtitle=subtitle,
                    output_path=config.CACHE_DIR / "pins" / f"{post.slug}.jpg",
                )

            # Create the pin via API
            if pin_image_path and pin_image_path.exists():
                # Upload the overlay image as base64
                pin_data = self._create_pin_with_image_file(
                    board_id=self.board_id,
                    title=post.title[:100],  # Pinterest 100 char limit
                    description=description,
                    link=post_url,
                    image_path=pin_image_path,
                )
            elif post.featured_image and post.featured_image.startswith("http"):
                # Fall back to using the original image URL directly
                pin_data = self._create_pin_with_image_url(
                    board_id=self.board_id,
                    title=post.title[:100],
                    description=description,
                    link=post_url,
                    image_url=post.featured_image,
                )
            else:
                result.error = "no_image_available"
                logger.warning(
                    "No image available for pin '%s' — skipping.", post.title
                )
                return result

            # Parse the API response
            result.pin_id = pin_data.get("id", "")
            result.success = True
            result.pin_url = f"https://www.pinterest.com/pin/{result.pin_id}/"

            # Record in history
            self._pinned_slugs.add(post.slug)
            self._save_pin_history()

            logger.info(
                "📌 Pinned: '%s' → %s", post.title, result.pin_url
            )

        except httpx.HTTPStatusError as e:
            result.error = f"API error {e.response.status_code}: {e.response.text[:200]}"
            logger.error("Pinterest API error for '%s': %s", post.title, result.error)
        except Exception as e:
            result.error = str(e)
            logger.error("Failed to pin '%s': %s", post.title, e)

        return result

    def pin_batch(self, posts: list[GeneratedPost]) -> PinBatchResult:
        """
        Pin multiple blog posts to Pinterest.

        Includes a delay between pins to respect rate limits.
        """
        batch = PinBatchResult(total_attempted=len(posts))

        for i, post in enumerate(posts, 1):
            logger.info(
                "  Pinning %d/%d: '%s'...", i, len(posts), post.title
            )

            result = self.pin_post(post)
            batch.results.append(result)

            if result.success:
                batch.total_created += 1
            elif result.error != "already_pinned":
                batch.total_failed += 1

            # Rate limit: wait between pin creations
            if i < len(posts):
                time.sleep(config.PINTEREST_REQUEST_DELAY)

        logger.info(
            "Pinterest batch complete: %d created, %d failed, %d skipped",
            batch.total_created,
            batch.total_failed,
            batch.total_attempted - batch.total_created - batch.total_failed,
        )

        return batch

    def list_boards(self) -> list[dict]:
        """
        List all Pinterest boards for the authenticated user.
        Useful for finding your board_id during setup.
        """
        response = self._client.get(f"{self.API_BASE}/boards")
        response.raise_for_status()
        boards = response.json().get("items", [])

        logger.info("Found %d Pinterest boards:", len(boards))
        for board in boards:
            logger.info(
                "  Board: '%s' (id: %s)", board.get("name"), board.get("id")
            )

        return boards

    # ─────────────────────────────────────────
    # Pinterest API Calls
    # ─────────────────────────────────────────

    def _create_pin_with_image_url(
        self,
        board_id: str,
        title: str,
        description: str,
        link: str,
        image_url: str,
    ) -> dict:
        """Create a pin using an external image URL."""
        payload = {
            "board_id": board_id,
            "title": title,
            "description": description,
            "link": link,
            "alt_text": title,
            "media_source": {
                "source_type": "image_url",
                "url": image_url,
                "is_standard": True,
            },
        }

        response = self._client.post(f"{self.API_BASE}/pins", json=payload)
        response.raise_for_status()
        return response.json()

    def _create_pin_with_image_file(
        self,
        board_id: str,
        title: str,
        description: str,
        link: str,
        image_path: Path,
    ) -> dict:
        """Create a pin by uploading a local image file as base64."""
        image_data = base64.b64encode(image_path.read_bytes()).decode("utf-8")

        # Determine content type
        suffix = image_path.suffix.lower()
        content_type_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }
        content_type = content_type_map.get(suffix, "image/jpeg")

        payload = {
            "board_id": board_id,
            "title": title,
            "description": description,
            "link": link,
            "alt_text": title,
            "media_source": {
                "source_type": "image_base64",
                "content_type": content_type,
                "data": image_data,
            },
        }

        response = self._client.post(f"{self.API_BASE}/pins", json=payload)
        response.raise_for_status()
        return response.json()

    # ─────────────────────────────────────────
    # Content Helpers
    # ─────────────────────────────────────────

    def _build_post_url(self, slug: str) -> str:
        """Build the full URL to the blog post."""
        base = config.SITE_BASE_URL.rstrip("/")
        return f"{base}/{slug}/"

    @staticmethod
    def _build_pin_description(post: GeneratedPost) -> str:
        """
        Build a Pinterest-optimized description.
        Max 800 chars. Includes hashtags for discoverability.
        """
        # Start with the post's SEO description
        desc = post.description or post.title

        # Add hashtags from tags (Pinterest indexes these for search)
        hashtags = []
        for tag in post.tags[:5]:
            # Clean tag into a hashtag
            clean = re.sub(r"[^a-zA-Z0-9]", "", tag.title())
            if clean:
                hashtags.append(f"#{clean}")

        # Always add some universal deal-related hashtags
        hashtags.extend(["#Deals", "#OnlineShopping", "#BestDeals"])

        # Deduplicate
        hashtags = list(dict.fromkeys(hashtags))

        hashtag_str = " ".join(hashtags[:8])

        full_desc = f"{desc}\n\n{hashtag_str}"

        # Trim to Pinterest's 800 char limit
        return full_desc[:800]

    @staticmethod
    def _build_subtitle(post: GeneratedPost) -> str:
        """Build the overlay subtitle text (price range + CTA)."""
        parts = []

        # Try to extract price range from content
        price_match = re.search(
            r"£[\d,.]+\s*[-–—to]+\s*£[\d,.]+", post.content_markdown
        )
        if price_match:
            parts.append(price_match.group())

        # Add category
        if post.categories:
            parts.append(post.categories[0])

        # Add CTA
        parts.append("Shop Now →")

        return " • ".join(parts)

    # ─────────────────────────────────────────
    # Pin History (deduplication)
    # ─────────────────────────────────────────

    def _load_pin_history(self):
        """Load the set of already-pinned post slugs."""
        history_file = config.DATA_DIR / "pinned_posts.txt"
        if history_file.exists():
            lines = history_file.read_text(encoding="utf-8").strip().splitlines()
            self._pinned_slugs = {line.strip() for line in lines if line.strip()}
            logger.info("Loaded %d pinned post slugs.", len(self._pinned_slugs))

    def _save_pin_history(self):
        """Persist the set of pinned post slugs."""
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        history_file = config.DATA_DIR / "pinned_posts.txt"
        history_file.write_text(
            "\n".join(sorted(self._pinned_slugs)) + "\n",
            encoding="utf-8",
        )


# ─────────────────────────────────────────────
# CLI entry point (for testing)
# ─────────────────────────────────────────────


def main():
    """Run the pinner standalone to list boards or test pinning."""
    import argparse

    logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)

    parser = argparse.ArgumentParser(
        description="Pinterest Auto-Pinner — create pins from blog posts."
    )
    parser.add_argument(
        "--list-boards",
        action="store_true",
        help="List your Pinterest boards and their IDs",
    )
    parser.add_argument(
        "--test-overlay",
        action="store_true",
        help="Create a test overlay image without posting to Pinterest",
    )

    args = parser.parse_args()

    if args.list_boards:
        with PinterestPinner() as pinner:
            boards = pinner.list_boards()
            if not boards:
                print(
                    "\nNo boards found. Make sure your PINTEREST_TOKEN is set "
                    "and has boards:read scope."
                )
            else:
                print(f"\n✅ Found {len(boards)} boards:")
                for b in boards:
                    print(f"   {b['name']} → id: {b['id']}")

    elif args.test_overlay:
        creator = PinImageCreator()
        output = creator.create_pin_image(
            title="Best Vintage Cameras Under $100",
            subtitle="$15 – $95 • Free Shipping • Shop Now →",
            output_path=config.CACHE_DIR / "pins" / "test-overlay.jpg",
        )
        if output:
            print(f"\n✅ Test overlay saved to: {output}")
        else:
            print("\n❌ Failed — make sure Pillow is installed: pip install Pillow")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
