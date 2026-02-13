"""
AI Blog Post Writer
====================
Takes a PostBrief from the content planner and generates a full
SEO-optimized blog post using the LLM via copilot-api.

Outputs structured markdown ready for Hugo.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

import config
from content_planner import PostBrief

logger = logging.getLogger(__name__)


@dataclass
class GeneratedPost:
    """A fully generated blog post ready for Hugo."""

    post_id: str = ""
    title: str = ""
    slug: str = ""
    content_markdown: str = ""
    description: str = ""         # meta description for SEO
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    featured_image: str = ""
    word_count: int = 0
    generated_at: str = ""
    post_type: str = ""

    def to_hugo_markdown(self) -> str:
        """
        Render the post as a complete Hugo-compatible markdown file
        with YAML front matter.
        """
        tags_yaml = "\n".join(f'  - "{tag}"' for tag in self.tags)
        categories_yaml = "\n".join(f'  - "{cat}"' for cat in self.categories)

        front_matter = f"""---
title: "{self._escape_yaml(self.title)}"
date: {self.generated_at}
description: "{self._escape_yaml(self.description)}"
tags:
{tags_yaml}
categories:
{categories_yaml}
featured_image: "{self.featured_image}"
draft: false
---

"""
        return front_matter + self.content_markdown

    @staticmethod
    def _escape_yaml(text: str) -> str:
        """Escape characters that would break YAML strings."""
        return text.replace('"', '\\"').replace("\n", " ")


class AIWriter:
    """
    Generates blog posts by calling the LLM via OpenAI-compatible API.

    Connects to copilot-api at localhost:4141 using the GPT-5 mini model.
    """

    def __init__(
        self,
        base_url: str = config.LLM_BASE_URL,
        model: str = config.LLM_MODEL,
        api_key: str = config.LLM_API_KEY,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(
            timeout=config.LLM_REQUEST_TIMEOUT,
            headers=self._build_headers(),
        )

    def _build_headers(self) -> dict[str, str]:
        """Build HTTP headers for the LLM API."""
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    def write_post(self, brief: PostBrief) -> GeneratedPost:
        """
        Generate a complete blog post from a PostBrief.

        Makes two LLM calls:
        1. Generate the blog post body
        2. Generate SEO metadata (title, description, tags)
        """
        logger.info(
            "Writing post: '%s' (type: %s, category: %s)",
            brief.suggested_title,
            brief.post_type,
            brief.category,
        )

        # Step 1: Generate the blog post content
        content = self._generate_content(brief)

        # Step 2: Generate SEO metadata
        metadata = self._generate_metadata(brief, content)

        # Step 3: Assemble the final post
        post = GeneratedPost(
            post_id=brief.post_id,
            title=metadata.get("title", brief.suggested_title),
            slug=brief.slug,
            content_markdown=content,
            description=metadata.get("description", ""),
            tags=metadata.get("tags", [brief.category]),
            categories=[brief.category],
            featured_image=self._pick_featured_image(brief),
            word_count=len(content.split()),
            generated_at=datetime.now(timezone.utc).isoformat(),
            post_type=brief.post_type,
        )

        logger.info(
            "Generated post '%s': %d words",
            post.title,
            post.word_count,
        )

        return post

    # ─────────────────────────────────────────
    # LLM Calls
    # ─────────────────────────────────────────

    def _generate_content(self, brief: PostBrief) -> str:
        """Generate the main blog post content."""
        # Build a structured product list for the prompt
        product_list = self._format_products_for_prompt(brief.listings)

        system_prompt = (
            "You are an expert blog writer specializing in product guides and buyer advice. "
            "You write in a natural, conversational tone — like a knowledgeable friend, "
            "not a salesperson. Your posts are well-structured with clear headings, "
            "short paragraphs, and actionable advice. You always include product links "
            "where the reader can buy the items you mention."
        )

        user_prompt = f"""Write a blog post based on this brief:

**Post Type:** {brief.post_type}
**Suggested Title:** {brief.suggested_title}
**Category:** {brief.category}
**Target Keywords:** {', '.join(brief.target_keywords)}
**Price Range:** {brief.price_range}

**Writing Instructions:**
{brief.instructions}

**Products to Feature:**
{product_list}

**Important Rules:**
1. Write {config.TARGET_WORD_COUNT_MIN}-{config.TARGET_WORD_COUNT_MAX} words.
2. Use markdown formatting with ## and ### headings.
3. For each product mentioned, include a markdown link to its eBay listing URL.
4. Format product links as: [Product Title](ebay_url)
5. Include the product price when mentioning it.
6. Add a brief intro paragraph and a conclusion with a call-to-action.
7. Use natural, SEO-friendly language — avoid keyword stuffing.
8. Do NOT mention AI, automation, or that this is auto-generated.
9. Do NOT use phrases like "in today's post" or "let's dive in".
10. If a product has an image URL, include it as: ![Product Name](image_url)

Write the complete blog post now in markdown:"""

        content = self._call_llm(system_prompt, user_prompt)

        # Clean up any markdown code fences the model might add
        content = self._clean_markdown(content)

        return content

    def _generate_metadata(self, brief: PostBrief, content: str) -> dict:
        """Generate SEO metadata for the post."""
        system_prompt = (
            "You are an SEO expert. Generate metadata for a blog post. "
            "Respond ONLY with valid JSON, no other text."
        )

        user_prompt = f"""Generate SEO metadata for this blog post.

**Post category:** {brief.category}
**Target keywords:** {', '.join(brief.target_keywords)}
**Post content (first 500 chars):** {content[:500]}

Respond with ONLY this JSON structure:
{{
    "title": "An SEO-optimized title (50-60 chars, include main keyword)",
    "description": "A compelling meta description (150-160 chars, include keyword, encourage click)",
    "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]
}}"""

        raw_response = self._call_llm(system_prompt, user_prompt)

        try:
            # Extract JSON from response (handle models that add extra text)
            json_match = re.search(r"\{[^{}]*\}", raw_response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning("Failed to parse metadata JSON: %s", e)

        # Fallback metadata
        return {
            "title": brief.suggested_title,
            "description": f"Discover the best {brief.category.lower()} — curated picks, honest reviews, and great deals.",
            "tags": brief.target_keywords[:5],
        }

    def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """
        Make a chat completion call to the LLM API.
        Uses OpenAI-compatible /v1/chat/completions endpoint.
        """
        url = f"{self.base_url}/v1/chat/completions"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": config.LLM_MAX_TOKENS,
            "temperature": config.LLM_TEMPERATURE,
        }

        logger.debug("Calling LLM: %s (model: %s)", url, self.model)

        try:
            response = self._client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            content = data["choices"][0]["message"]["content"]
            logger.debug("LLM response: %d chars", len(content))
            return content.strip()

        except httpx.HTTPStatusError as e:
            logger.error("LLM API HTTP error %d: %s", e.response.status_code, e.response.text)
            raise
        except httpx.RequestError as e:
            logger.error("LLM API connection error: %s", e)
            raise
        except (KeyError, IndexError) as e:
            logger.error("Unexpected LLM response format: %s", e)
            raise

    # ─────────────────────────────────────────
    # Formatting Helpers
    # ─────────────────────────────────────────

    @staticmethod
    def _format_products_for_prompt(listings: list[dict]) -> str:
        """Format listing data into a readable product list for the LLM prompt."""
        lines = []
        for i, item in enumerate(listings, 1):
            title = item.get("title", "Unknown Product")
            price = item.get("price", 0)
            url = item.get("listing_url", "")
            condition = item.get("condition", "")
            image = item.get("image_url", "")
            shipping = item.get("shipping", "")

            entry = f"{i}. **{title}**\n"
            entry += f"   - Price: ${price:.2f}\n"
            if condition:
                entry += f"   - Condition: {condition}\n"
            if shipping:
                entry += f"   - Shipping: {shipping}\n"
            if url:
                entry += f"   - Link: {url}\n"
            if image:
                entry += f"   - Image: {image}\n"

            lines.append(entry)

        return "\n".join(lines)

    @staticmethod
    def _clean_markdown(text: str) -> str:
        """Remove code fences and other artifacts from LLM output."""
        # Remove ```markdown ... ``` wrapping
        text = re.sub(r"^```(?:markdown)?\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)

        # Remove any leading/trailing whitespace
        text = text.strip()

        return text

    @staticmethod
    def _pick_featured_image(brief: PostBrief) -> str:
        """Pick the best image from the featured listings for the post header."""
        for listing in brief.listings:
            image = listing.get("image_url", "")
            if image and "s-l" in image:
                # Upgrade to larger eBay image if possible
                return image.replace("s-l225.", "s-l500.").replace("s-l140.", "s-l500.")
        return ""
