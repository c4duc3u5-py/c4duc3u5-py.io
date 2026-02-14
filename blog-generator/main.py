"""
Main Pipeline Orchestrator
============================
Runs the complete eBay-to-Blog pipeline:

1. Scrape  → Pull active listings from your eBay seller page
2. Plan    → Group listings into blog post themes
3. Write   → Generate SEO blog posts via AI
4. Build   → Write posts as Hugo markdown files
5. Report  → Summary of what was generated

Usage:
    python main.py                     # full pipeline
    python main.py --scrape-only       # just scrape listings
    python main.py --skip-scrape       # use cached listings
    python main.py --dry-run           # plan without generating
"""

import argparse
import io
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from ai_writer import AIWriter, GeneratedPost
from content_planner import ContentPlanner
from ebay_scraper import EbayScraper, ScrapeResult
from pinterest_pinner import PinterestPinner, PinBatchResult
from site_builder import SiteBuilder

logger = logging.getLogger(__name__)


def setup_logging():
    """Configure logging for the pipeline."""
    # Force UTF-8 output on Windows console (supports emojis)
    utf8_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=config.LOG_LEVEL,
        format=config.LOG_FORMAT,
        handlers=[
            logging.StreamHandler(utf8_stdout),
            logging.FileHandler(
                config.BLOG_GENERATOR_DIR / "pipeline.log",
                encoding="utf-8",
            ),
        ],
    )


def run_pipeline(
    scrape: bool = True,
    generate: bool = True,
    dry_run: bool = False,
    seller_name: str | None = None,
    backend: str = "browser",
    pinterest: bool = True,
) -> dict:
    """
    Run the full pipeline end-to-end.

    Args:
        scrape: Whether to scrape fresh listings (False = use cached data)
        generate: Whether to generate blog posts (False = plan only)
        dry_run: If True, plan posts but don't write or call the LLM
        seller_name: Override the seller name from config

    Returns:
        Summary dict with pipeline results
    """
    start_time = time.time()
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "listings_scraped": 0,
        "posts_planned": 0,
        "posts_generated": 0,
        "posts_published": 0,
        "pins_created": 0,
        "errors": [],
    }

    if seller_name:
        config.EBAY_SELLER_NAME = seller_name

    # Validate configuration
    if config.EBAY_SELLER_NAME == "YOUR_SELLER_NAME":
        logger.error(
            "❌ Set your eBay seller name first!\n"
            "   Option 1: Edit blog-generator/config.py\n"
            "   Option 2: Set env var EBAY_SELLER_NAME=yoursellername\n"
            "   Option 3: python main.py --seller yoursellername"
        )
        return summary

    logger.info("=" * 60)
    logger.info("🚀 Starting blog generation pipeline")
    logger.info("   Seller: %s", config.EBAY_SELLER_NAME)
    logger.info("   Dry run: %s", dry_run)
    logger.info("=" * 60)

    # ── Step 1: Scrape Listings ──
    if scrape:
        logger.info("\n📥 Step 1: Scraping eBay listings...")
        try:
            with EbayScraper(backend=backend) as scraper:
                scrape_result = scraper.scrape_all_listings()
                EbayScraper.save_listings(scrape_result)
                summary["listings_scraped"] = scrape_result.total_listings

                if scrape_result.total_listings == 0:
                    logger.warning("No listings found! Check your seller name.")
                    return summary

        except Exception as e:
            error = f"Scrape failed: {e}"
            logger.error(error)
            summary["errors"].append(error)
            return summary
    else:
        logger.info("\n📥 Step 1: Loading cached listings...")
        try:
            scrape_result = EbayScraper.load_listings()
            summary["listings_scraped"] = scrape_result.total_listings
            logger.info("Loaded %d cached listings.", scrape_result.total_listings)
        except FileNotFoundError:
            logger.error("No cached listings found. Run with --scrape first.")
            return summary

    # Print category summary
    categories: dict[str, int] = {}
    for listing in scrape_result.listings:
        cat = listing.category_hint or "General"
        categories[cat] = categories.get(cat, 0) + 1

    logger.info("\n📊 Inventory breakdown:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        logger.info("   %s: %d listings", cat, count)

    # ── Step 2: Plan Content ──
    logger.info("\n📝 Step 2: Planning blog posts...")
    planner = ContentPlanner()
    plan = planner.generate_plan(scrape_result)
    ContentPlanner.save_plan(plan)
    summary["posts_planned"] = plan.total_briefs

    if plan.total_briefs == 0:
        logger.info("No new posts to generate — all topics already covered!")
        return summary

    logger.info("\n📋 Posts to generate:")
    for i, brief in enumerate(plan.briefs, 1):
        logger.info(
            "   %d. [%s] %s (%d listings)",
            i, brief.post_type, brief.suggested_title, brief.listing_count,
        )

    if dry_run:
        logger.info("\n🏁 Dry run complete — no posts generated.")
        return summary

    if not generate:
        logger.info("\n🏁 Planning complete — skipping generation.")
        return summary

    # ── Step 3: Generate Posts ──
    logger.info("\n✍️  Step 3: Generating blog posts via AI...")
    generated_posts: list[GeneratedPost] = []

    with AIWriter() as writer:
        for i, brief in enumerate(plan.briefs, 1):
            logger.info(
                "\n  Writing post %d/%d: '%s'...",
                i, plan.total_briefs, brief.suggested_title,
            )
            try:
                post = writer.write_post(brief)
                generated_posts.append(post)
                summary["posts_generated"] += 1

                logger.info(
                    "  ✅ Generated: '%s' (%d words)",
                    post.title, post.word_count,
                )

                # Small delay between LLM calls to avoid rate limits
                if i < plan.total_briefs:
                    time.sleep(2)

            except Exception as e:
                error = f"Failed to generate '{brief.suggested_title}': {e}"
                logger.error("  ❌ %s", error)
                summary["errors"].append(error)

    # ── Step 4: Publish to Hugo Site ──
    logger.info("\n📁 Step 4: Publishing to Hugo site...")

    with SiteBuilder() as builder:
        builder.ensure_site_structure()
        published_paths = builder.publish_batch(generated_posts)
        summary["posts_published"] = len(published_paths)

        for path in published_paths:
            logger.info("  📄 Published: %s", path.name)

        # Cleanup stale posts
        active_ids = {l.item_id for l in scrape_result.listings if l.item_id}
        removed = builder.cleanup_stale_posts(active_ids)
        if removed:
            logger.info("  🗑️  Removed %d stale posts", len(removed))

    # ── Step 5: Pin to Pinterest ──
    if pinterest and config.PINTEREST_ENABLED and generated_posts:
        logger.info("\n📌 Step 5: Pinning to Pinterest...")
        try:
            with PinterestPinner() as pinner:
                pin_result = pinner.pin_batch(generated_posts)
                summary["pins_created"] = pin_result.total_created

                if pin_result.total_failed > 0:
                    summary["errors"].append(
                        f"Pinterest: {pin_result.total_failed} pin(s) failed"
                    )
        except Exception as e:
            error = f"Pinterest pinning failed: {e}"
            logger.error("  ❌ %s", error)
            summary["errors"].append(error)
    elif pinterest and not config.PINTEREST_ENABLED:
        logger.info("\n📌 Step 5: Pinterest disabled (set PINTEREST_ENABLED=true in .env)")
    elif not pinterest:
        logger.info("\n📌 Step 5: Pinterest skipped (--no-pinterest)")

    # ── Summary ──
    elapsed = time.time() - start_time
    summary["elapsed_seconds"] = round(elapsed, 1)

    logger.info("\n" + "=" * 60)
    logger.info("✅ Pipeline complete!")
    logger.info("   Listings scraped:  %d", summary["listings_scraped"])
    logger.info("   Posts planned:     %d", summary["posts_planned"])
    logger.info("   Posts generated:   %d", summary["posts_generated"])
    logger.info("   Posts published:   %d", summary["posts_published"])
    logger.info("   Pins created:      %d", summary["pins_created"])
    logger.info("   Errors:            %d", len(summary["errors"]))
    logger.info("   Time:              %.1fs", elapsed)
    logger.info("=" * 60)

    if summary["posts_published"] > 0:
        logger.info(
            "\n🌐 Next steps:\n"
            "   1. cd site/ && hugo server     → Preview locally\n"
            "   2. hugo                        → Build for production\n"
            "   3. Push to GitHub              → Auto-deploys to GitHub Pages\n"
        )

    return summary


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="eBay-to-Blog AI Pipeline — Generate SEO blog posts from your eBay inventory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --seller myebayname          Full pipeline with your seller name
  python main.py --scrape-only                Just scrape and save listings
  python main.py --skip-scrape                Use previously scraped listings
  python main.py --dry-run                    Plan posts without generating
  python main.py --max-posts 3                Generate at most 3 posts
        """,
    )

    parser.add_argument(
        "--seller",
        type=str,
        default=None,
        help="Your eBay seller username (overrides config.py)",
    )
    parser.add_argument(
        "--scrape-only",
        action="store_true",
        help="Only scrape listings, don't generate posts",
    )
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Skip scraping, use cached listing data",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan posts but don't call the AI or write files",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=None,
        help="Maximum number of posts to generate this run",
    )
    parser.add_argument(
        "--backend",
        type=str,
        choices=["browser", "http"],
        default="browser",
        help="Scraping backend: 'browser' (Playwright, default) or 'http' (httpx, often blocked)",
    )
    parser.add_argument(
        "--no-pinterest",
        action="store_true",
        help="Skip Pinterest pinning even if configured",
    )

    args = parser.parse_args()

    setup_logging()

    if args.max_posts:
        config.MAX_POSTS_PER_RUN = args.max_posts

    summary = run_pipeline(
        scrape=not args.skip_scrape,
        generate=not args.scrape_only,
        dry_run=args.dry_run,
        seller_name=args.seller,
        backend=args.backend,
        pinterest=not args.no_pinterest,
    )

    # Exit with error code only if nothing was published at all
    published = summary.get("published", 0)
    errors = summary.get("errors", 0)
    if errors and published == 0:
        sys.exit(1)  # Total failure - nothing published
    elif errors:
        sys.exit(0)  # Partial success - some posts published despite errors


if __name__ == "__main__":
    main()
