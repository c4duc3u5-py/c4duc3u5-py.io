"""
Content Planner
================
Takes scraped eBay listings and groups them into blog post ideas.
Decides what type of post to write, which listings to feature,
and generates a structured brief for the AI writer.
"""

import hashlib
import json
import logging
import random
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
from ebay_scraper import EbayListing, ScrapeResult

logger = logging.getLogger(__name__)


@dataclass
class PostBrief:
    """
    A structured brief describing a blog post to be written.
    Passed to the AI writer as context.
    """

    post_id: str = ""
    post_type: str = ""             # buyers_guide, product_roundup, how_to, comparison, deals
    suggested_title: str = ""
    category: str = ""
    target_keywords: list[str] = field(default_factory=list)
    listings: list[dict] = field(default_factory=list)  # serialized EbayListing dicts
    price_range: str = ""           # e.g. "$10 - $50"
    listing_count: int = 0
    instructions: str = ""          # specific guidance for the AI writer
    created_at: str = ""

    @property
    def slug(self) -> str:
        """URL-friendly slug derived from the post ID."""
        return self.post_id


@dataclass
class ContentPlan:
    """A batch of post briefs generated in a single planning run."""

    generated_at: str = ""
    total_briefs: int = 0
    briefs: list[PostBrief] = field(default_factory=list)


class ContentPlanner:
    """
    Analyzes scraped listings and produces blog post briefs.

    Strategy:
    1. Group listings by category
    2. For each viable category, generate post ideas using templates
    3. Prioritize categories with more listings and wider price ranges
    4. Avoid duplicating posts already generated (checks existing content)
    """

    # ── Post title templates per post type ──
    TITLE_TEMPLATES = {
        "buyers_guide": [
            "Best {category} Under £{max_price} — A Buyer's Guide",
            "Top {count} {category} Picks for Every Budget",
            "{category}: What to Look for Before You Buy",
            "The Ultimate Guide to Buying {category} Online",
            "Best Affordable {category} You Can Buy Right Now",
        ],
        "product_roundup": [
            "New Arrivals: {category} Worth Checking Out",
            "Fresh Finds: {count} {category} Just Listed",
            "This Week's Best {category} Picks",
            "What's New in {category} — Latest Listings",
            "{count} {category} You Don't Want to Miss",
        ],
        "how_to": [
            "How to Spot a Great Deal on {category}",
            "Beginner's Guide to {category} — Everything You Need to Know",
            "How to Choose the Right {category} for You",
            "{category} 101: Tips for First-Time Buyers",
            "What to Look for When Buying Used {category}",
        ],
        "comparison": [
            "Budget vs Premium {category}: Is It Worth Paying More?",
            "New vs Used {category}: Which Is the Better Deal?",
            "Comparing the Best {category} at Every Price Point",
        ],
        "deals": [
            "Best {category} Deals This Week",
            "{category} on Sale: Top Picks Under £{max_price}",
            "Deal Alert: {count} {category} at Great Prices",
            "Price Drops on {category} You'll Love",
        ],
    }

    def __init__(self, max_posts: int = config.MAX_POSTS_PER_RUN):
        self.max_posts = max_posts
        self._existing_post_ids: set[str] = set()
        self._load_existing_posts()

    def _load_existing_posts(self):
        """Scan the Hugo content directory for already-generated posts."""
        content_dir = config.CONTENT_DIR
        if not content_dir.exists():
            return

        for md_file in content_dir.glob("*.md"):
            # Use the filename stem as the post ID
            self._existing_post_ids.add(md_file.stem)

        logger.info("Found %d existing posts to avoid duplicating.", len(self._existing_post_ids))

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    def generate_plan(self, scrape_result: ScrapeResult) -> ContentPlan:
        """
        Generate a content plan from scraped listings.

        Returns a ContentPlan with up to `max_posts` PostBriefs.
        """
        plan = ContentPlan(
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        # Step 1: Group listings by category
        category_groups = self._group_by_category(scrape_result.listings)

        logger.info(
            "Grouped %d listings into %d categories: %s",
            len(scrape_result.listings),
            len(category_groups),
            list(category_groups.keys()),
        )

        # Step 2: Generate candidate post briefs for each category
        candidates: list[PostBrief] = []
        for category, listings in category_groups.items():
            if len(listings) < config.MIN_LISTINGS_PER_POST:
                logger.debug(
                    "Skipping '%s' — only %d listings (min: %d)",
                    category, len(listings), config.MIN_LISTINGS_PER_POST,
                )
                continue

            category_briefs = self._generate_briefs_for_category(category, listings)
            candidates.extend(category_briefs)

        # Step 3: Filter out already-generated posts
        novel_candidates = [
            b for b in candidates if b.post_id not in self._existing_post_ids
        ]

        logger.info(
            "Generated %d candidate briefs, %d are new (not already published).",
            len(candidates),
            len(novel_candidates),
        )

        # Step 4: Prioritize and select top N
        selected = self._prioritize(novel_candidates)[: self.max_posts]

        plan.briefs = selected
        plan.total_briefs = len(selected)

        logger.info("Content plan ready: %d posts to generate.", plan.total_briefs)
        return plan

    # ─────────────────────────────────────────
    # Grouping
    # ─────────────────────────────────────────

    def _group_by_category(
        self, listings: list[EbayListing]
    ) -> dict[str, list[EbayListing]]:
        """Group listings by their category hint."""
        groups: dict[str, list[EbayListing]] = defaultdict(list)
        for listing in listings:
            category = listing.category_hint or "General"
            groups[category].append(listing)
        return dict(groups)

    # ─────────────────────────────────────────
    # Brief Generation
    # ─────────────────────────────────────────

    def _generate_briefs_for_category(
        self, category: str, listings: list[EbayListing]
    ) -> list[PostBrief]:
        """Generate multiple post brief candidates for a single category."""
        briefs = []
        prices = [l.price for l in listings if l.price > 0]
        min_price = min(prices) if prices else 0
        max_price = max(prices) if prices else 0
        price_range = f"£{min_price:.0f} - £{max_price:.0f}"

        # Pick 2-3 post types that make sense for this category
        viable_types = self._pick_post_types(category, listings)

        for post_type in viable_types:
            title = self._generate_title(post_type, category, len(listings), max_price)
            post_id = self._make_post_id(title)

            # Select the most interesting listings to feature (up to 8)
            featured = self._select_featured_listings(listings, max_featured=8)

            instructions = self._build_writer_instructions(
                post_type, category, len(listings), price_range
            )

            keywords = self._extract_keywords(category, listings)

            brief = PostBrief(
                post_id=post_id,
                post_type=post_type,
                suggested_title=title,
                category=category,
                target_keywords=keywords,
                listings=[asdict(l) for l in featured],
                price_range=price_range,
                listing_count=len(listings),
                instructions=instructions,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            briefs.append(brief)

        return briefs

    def _pick_post_types(
        self, category: str, listings: list[EbayListing]
    ) -> list[str]:
        """Decide which post types are viable for a given category and listing set."""
        types = []

        # Always viable
        types.append("buyers_guide")

        if len(listings) >= 3:
            types.append("product_roundup")

        # How-to works for most categories
        types.append("how_to")

        # Comparison needs varied prices
        prices = [l.price for l in listings if l.price > 0]
        if prices and (max(prices) - min(prices)) > 20:
            types.append("comparison")

        # Deals if there are well-priced items
        if len(listings) >= 3:
            types.append("deals")

        # Return 2 types max per category per run to avoid flooding
        random.shuffle(types)
        return types[:2]

    def _generate_title(
        self, post_type: str, category: str, count: int, max_price: float
    ) -> str:
        """Pick and fill a title template."""
        templates = self.TITLE_TEMPLATES.get(post_type, self.TITLE_TEMPLATES["buyers_guide"])
        template = random.choice(templates)

        return template.format(
            category=category,
            count=min(count, 10),
            max_price=int(max_price) if max_price > 0 else 100,
        )

    def _select_featured_listings(
        self, listings: list[EbayListing], max_featured: int = 8
    ) -> list[EbayListing]:
        """
        Pick the best listings to feature in a blog post.
        Prioritizes: has image, has good title length, reasonable price.
        """
        scored: list[tuple[float, EbayListing]] = []

        for listing in listings:
            score = 0.0

            # Has an image
            if listing.image_url and "s-l" in listing.image_url:
                score += 3.0

            # Good title length (30-70 chars)
            if 30 <= len(listing.title) <= 70:
                score += 2.0
            elif len(listing.title) > 15:
                score += 1.0

            # Has a price
            if listing.price > 0:
                score += 2.0

            # Has condition info
            if listing.condition:
                score += 1.0

            # Free shipping bonus
            if listing.shipping and "free" in listing.shipping.lower():
                score += 1.5

            # Small random factor to avoid identical selections
            score += random.uniform(0, 0.5)

            scored.append((score, listing))

        scored.sort(key=lambda x: -x[0])
        return [listing for _, listing in scored[:max_featured]]

    # ─────────────────────────────────────────
    # Writer Instructions
    # ─────────────────────────────────────────

    def _build_writer_instructions(
        self, post_type: str, category: str, count: int, price_range: str
    ) -> str:
        """Build specific writing instructions for the AI writer."""
        base = (
            f"Write an SEO-optimized blog post about {category}. "
            f"There are {count} listings available in the {price_range} range. "
            "Each product mentioned MUST include its eBay link so readers can buy. "
            "Write in a friendly, knowledgeable tone — like a trusted friend who's done the homework. "
            "Use short paragraphs (2-3 sentences max) and descriptive subheadings for scannability. "
            "Do NOT mention that this is AI-generated. "
            "Do NOT use excessive exclamation marks, hype language, or filler phrases. "
            "Include a clear call-to-action encouraging readers to check out the listings. "
            "Add genuine buying advice — what to look for, what to avoid, care tips, or sizing guidance. "
            "Mention specific details about each product (materials, dimensions, features) that show real expertise."
        )

        type_specific = {
            "buyers_guide": (
                " Structure the post as: hook intro explaining why these items are worth buying, "
                "individual product highlights with specific pros and practical use cases, "
                "a 'what to look for when buying' tips section with genuine expertise, "
                "and a conclusion naming your top pick with reasoning."
            ),
            "product_roundup": (
                " Structure as a listicle — strong intro paragraph, then each product with its "
                "own ### subheading, price, condition, and 2-3 sentences on why it's notable. "
                "Include a comparison angle (best for X, best for Y). "
                "End with a 'best overall' pick and summary table if 4+ products."
            ),
            "how_to": (
                " Structure as an educational guide — start with why this knowledge saves money or time, "
                "give practical step-by-step advice based on real buying experience, "
                "weave in specific product recommendations with links as examples, "
                "and include a 'common mistakes to avoid' section."
            ),
            "comparison": (
                " Compare products at different price points with honesty. "
                "Use a clear structure: budget pick, mid-range pick, best value pick. "
                "Explain tradeoffs concretely (not just 'this one is better'). "
                "Include a quick comparison summary. Name a winner for different buyer types."
            ),
            "deals": (
                " Focus on value — why each item is a good deal at its current price. "
                "Lead with the best value-for-money item. "
                "For each deal, explain what makes the price good (compare to typical market price if possible). "
                "Create soft urgency without being pushy — focus on limited quantity or seasonal relevance."
            ),
        }

        return base + type_specific.get(post_type, "")

    # ─────────────────────────────────────────
    # SEO Keywords
    # ─────────────────────────────────────────

    def _extract_keywords(
        self, category: str, listings: list[EbayListing]
    ) -> list[str]:
        """Extract target SEO keywords from the category and listing titles."""
        keywords = [category.lower()]

        # Add "buy {category}" and "best {category}" variants
        keywords.append(f"buy {category.lower()}")
        keywords.append(f"best {category.lower()}")
        keywords.append(f"cheap {category.lower()}")
        keywords.append(f"{category.lower()} for sale")
        keywords.append(f"{category.lower()} uk")
        keywords.append(f"buy {category.lower()} online uk")

        # Extract common meaningful words from listing titles
        word_freq: dict[str, int] = defaultdict(int)
        stop_words = {
            "the", "a", "an", "and", "or", "for", "in", "on", "at", "to",
            "of", "is", "it", "with", "new", "used", "lot", "set", "free",
            "shipping", "listing", "great", "good", "nice", "item", "sale",
            "buy", "best", "fast", "ship", "condition",
        }

        for listing in listings:
            words = listing.title.lower().split()
            for word in words:
                cleaned = word.strip(",.!?()[]{}\"'#-_")
                if len(cleaned) > 2 and cleaned not in stop_words:
                    word_freq[cleaned] += 1

        # Add top frequent words as keywords
        top_words = sorted(word_freq.items(), key=lambda x: -x[1])[:5]
        keywords.extend([w for w, _ in top_words])

        return list(dict.fromkeys(keywords))  # deduplicate, preserve order

    # ─────────────────────────────────────────
    # Prioritization
    # ─────────────────────────────────────────

    def _prioritize(self, briefs: list[PostBrief]) -> list[PostBrief]:
        """
        Score and sort briefs by potential impact.
        Higher score = generate this post first.
        """

        def score(brief: PostBrief) -> float:
            s = 0.0
            # More listings = more products to link to = more value
            s += min(brief.listing_count, 20) * 2.0
            # Buyer guides and roundups tend to rank best in SEO
            type_weights = {
                "buyers_guide": 5.0,
                "product_roundup": 4.0,
                "deals": 3.5,
                "how_to": 3.0,
                "comparison": 2.5,
            }
            s += type_weights.get(brief.post_type, 1.0)
            # Categories with actual names rank better than "General"
            if brief.category != "General":
                s += 3.0
            return s

        return sorted(briefs, key=score, reverse=True)

    # ─────────────────────────────────────────
    # Utilities
    # ─────────────────────────────────────────

    @staticmethod
    def _make_post_id(title: str) -> str:
        """Create a URL-safe post ID from a title."""
        slug = title.lower()
        # Remove special chars
        slug = "".join(c if c.isalnum() or c in " -" else "" for c in slug)
        # Replace spaces with hyphens
        slug = "-".join(slug.split())
        # Truncate
        slug = slug[:60].rstrip("-")
        # Add short hash for uniqueness
        title_hash = hashlib.md5(title.encode()).hexdigest()[:6]
        return f"{slug}-{title_hash}"

    # ─────────────────────────────────────────
    # Persistence
    # ─────────────────────────────────────────

    @staticmethod
    def save_plan(plan: ContentPlan, output_path: Optional[Path] = None) -> Path:
        """Save the content plan to JSON."""
        if output_path is None:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            output_path = config.DATA_DIR / "content_plan.json"

        data = {
            "generated_at": plan.generated_at,
            "total_briefs": plan.total_briefs,
            "briefs": [asdict(b) for b in plan.briefs],
        }

        output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Saved content plan with %d briefs to %s", plan.total_briefs, output_path)
        return output_path

    @staticmethod
    def load_plan(input_path: Optional[Path] = None) -> ContentPlan:
        """Load a previously saved content plan."""
        if input_path is None:
            input_path = config.DATA_DIR / "content_plan.json"

        data = json.loads(input_path.read_text(encoding="utf-8"))

        plan = ContentPlan(
            generated_at=data["generated_at"],
            total_briefs=data["total_briefs"],
        )
        plan.briefs = [PostBrief(**b) for b in data["briefs"]]
        return plan
