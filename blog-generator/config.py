"""
Blog Generator Configuration
=============================
Central configuration for the eBay-to-Blog AI pipeline.
All settings can be overridden via environment variables.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (Promotion/.env)
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")

# ─────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────
PROJECT_ROOT = _project_root
BLOG_GENERATOR_DIR = Path(__file__).resolve().parent
SITE_DIR = PROJECT_ROOT / "site"
CONTENT_DIR = SITE_DIR / "content" / "posts"
STATIC_IMAGES_DIR = SITE_DIR / "static" / "images" / "products"
DATA_DIR = BLOG_GENERATOR_DIR / "data"
CACHE_DIR = BLOG_GENERATOR_DIR / ".cache"

# ─────────────────────────────────────────────
# eBay Seller Settings
# ─────────────────────────────────────────────
EBAY_SELLER_NAME = os.getenv("EBAY_SELLER_NAME", "YOUR_SELLER_NAME")
EBAY_STORE_URL = os.getenv(
    "EBAY_STORE_URL",
    f"https://www.ebay.co.uk/sch/{EBAY_SELLER_NAME}/m.html",
)

# Maximum number of listings to scrape per run
EBAY_MAX_LISTINGS = int(os.getenv("EBAY_MAX_LISTINGS", "200"))

# Delay between page requests (seconds) — be polite to eBay's servers
EBAY_REQUEST_DELAY = float(os.getenv("EBAY_REQUEST_DELAY", "2.0"))

# User-Agent for HTTP requests
HTTP_USER_AGENT = os.getenv(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

# ─────────────────────────────────────────────
# LLM / AI Settings (copilot-api on Docker)
# ─────────────────────────────────────────────
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:4141")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5-mini")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")  # empty if copilot-api doesn't require one
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.7"))
LLM_REQUEST_TIMEOUT = int(os.getenv("LLM_REQUEST_TIMEOUT", "120"))

# ─────────────────────────────────────────────
# Content Generation Settings
# ─────────────────────────────────────────────
# Minimum listings in a category to justify a blog post
MIN_LISTINGS_PER_POST = int(os.getenv("MIN_LISTINGS_PER_POST", "2"))

# Maximum blog posts to generate per pipeline run
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "5"))

# Target word count range for generated articles
TARGET_WORD_COUNT_MIN = int(os.getenv("TARGET_WORD_COUNT_MIN", "800"))
TARGET_WORD_COUNT_MAX = int(os.getenv("TARGET_WORD_COUNT_MAX", "1500"))

# Blog post types the AI can generate
POST_TYPES = [
    "buyers_guide",       # "Best X Under £Y" / "Top 10 Z for Beginners"
    "product_roundup",    # "New Arrivals: Vintage Tech This Week"
    "how_to",             # "How to Identify Rare Stamps" / "Beginner's Guide to X"
    "comparison",         # "X vs Y: Which Should You Buy?"
    "deals",              # "This Week's Best Deals on Collectibles"
]

# ─────────────────────────────────────────────
# Hugo / Site Settings
# ─────────────────────────────────────────────
SITE_TITLE = os.getenv("SITE_TITLE", "Deals & Finds")
SITE_DESCRIPTION = os.getenv(
    "SITE_DESCRIPTION",
    "Curated finds, buyer guides, and deals on collectibles, tech, games & more. UK-based deals and honest reviews.",
)
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://c4duc3u5-py.github.io/c4duc3u5-py.io/")
SITE_LANGUAGE = os.getenv("SITE_LANGUAGE", "en-GB")
SITE_AUTHOR = os.getenv("SITE_AUTHOR", "Deals & Finds")

# ─────────────────────────────────────────────
# Pinterest Auto-Pinner
# ─────────────────────────────────────────────
# Enable/disable Pinterest pinning (set to "true" to enable)
PINTEREST_ENABLED = os.getenv("PINTEREST_ENABLED", "false").lower() == "true"

# Pinterest API access token (from https://developers.pinterest.com)
PINTEREST_TOKEN = os.getenv("PINTEREST_TOKEN", "")

# Pinterest board ID to pin to (use `python pinterest_pinner.py --list-boards` to find yours)
PINTEREST_BOARD_ID = os.getenv("PINTEREST_BOARD_ID", "")

# Enable image text overlays (requires Pillow)
PINTEREST_OVERLAY_ENABLED = os.getenv("PINTEREST_OVERLAY_ENABLED", "true").lower() == "true"

# Font size for pin overlay text
PINTEREST_OVERLAY_FONT_SIZE = int(os.getenv("PINTEREST_OVERLAY_FONT_SIZE", "48"))

# Delay between pin API calls (seconds) to respect rate limits
PINTEREST_REQUEST_DELAY = float(os.getenv("PINTEREST_REQUEST_DELAY", "3.0"))

# ─────────────────────────────────────────────
# Scheduling / Automation
# ─────────────────────────────────────────────
# Cron expression for GitHub Actions (default: daily at 6 AM UTC)
SCHEDULE_CRON = os.getenv("SCHEDULE_CRON", "0 6 * * *")

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
