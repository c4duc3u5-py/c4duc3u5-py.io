# 🛒 eBay Auto-Blog

**AI-powered blog that automatically promotes your eBay listings through SEO-optimized content.**

Scrapes your active eBay inventory → Groups items into blog topics → Generates engaging articles with AI → Publishes to a free Hugo static site on GitHub Pages.

🌐 **Live site**: [c4duc3u5-py.github.io/c4duc3u5-py.io](https://c4duc3u5-py.github.io/c4duc3u5-py.io/)

## How It Works

```
eBay Listings → Content Planner → AI Writer → Hugo Site → GitHub Pages
     24 items      5 categories     5 posts     static     free hosting
```

1. **Scraper** pulls active listings from your eBay seller page
2. **Planner** groups items by category and picks post types (buyer's guide, roundup, comparison, etc.)
3. **AI Writer** generates 1000+ word SEO articles with embedded product links
4. **Site Builder** outputs Hugo-compatible markdown with proper front matter
5. **Hugo** builds the static site; GitHub Pages hosts it for free

## Quick Start

### Prerequisites
- Python 3.10+
- [Hugo Extended](https://gohugo.io/installation/) (for local preview)
- An OpenAI-compatible LLM endpoint (local or cloud)

### Setup

```bash
# Clone the repo
git clone https://github.com/c4duc3u5-py/c4duc3u5-py.io.git
cd c4duc3u5-py.io

# Install Python dependencies
pip install -r blog-generator/requirements.txt

# Install Hugo theme
cd site && mkdir -p themes
git clone https://github.com/theNewDynamic/gohugo-theme-ananke.git themes/ananke --depth 1
cd ..

# Configure environment
cp .env.example .env
# Edit .env with your seller name and LLM endpoint
```

### Run

```powershell
# Full pipeline (scrape + generate + build)
.\run.ps1

# Skip scraping (use cached listings)
.\run.ps1 -SkipScrape

# Generate 3 posts, don't push to GitHub
.\run.ps1 -SkipScrape -MaxPosts 3 -NoPush
```

Or run the Python pipeline directly:

```bash
cd blog-generator
python main.py --skip-scrape --max-posts 5
```

### Preview Locally

```bash
cd site
hugo server
# Open http://localhost:1313/c4duc3u5-py.io/
```

## Project Structure

```
├── blog-generator/
│   ├── config.py           # Configuration (env vars)
│   ├── ebay_scraper.py     # eBay listing scraper
│   ├── content_planner.py  # Groups listings → post briefs
│   ├── ai_writer.py        # LLM-powered article generator
│   ├── site_builder.py     # Hugo markdown file writer
│   ├── main.py             # CLI orchestrator
│   ├── requirements.txt    # Python dependencies
│   └── data/
│       ├── listings.json   # Cached eBay listings
│       └── content_plan.json
├── site/
│   ├── hugo.toml           # Hugo configuration
│   └── content/posts/      # Generated blog posts
├── .github/workflows/
│   └── generate.yml        # Daily automation (GitHub Actions)
├── run.ps1                 # Local run script (PowerShell)
├── .env.example            # Environment template
└── .gitignore
```

## Configuration

All settings via environment variables (`.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `EBAY_SELLER_NAME` | — | Your eBay seller username |
| `LLM_BASE_URL` | `http://localhost:4141` | OpenAI-compatible API endpoint |
| `LLM_MODEL` | `gpt-5-mini` | Model to use for content generation |
| `LLM_API_KEY` | `sk` | API key for the LLM |

## Post Types

The planner generates 5 types of content:

| Type | Description |
|------|-------------|
| **Buyer's Guide** | "Best X Under $Y" — comparison-style posts |
| **Product Roundup** | "Top 5 Deals This Week" — curated collections |
| **How-To** | "How to Choose the Right X" — educational content |
| **Comparison** | "New vs Used: Which is Better?" — side-by-side analysis |
| **Deals** | "Latest Deals on X" — time-sensitive promotions |

## Automation

The GitHub Actions workflow (`.github/workflows/generate.yml`) runs daily at 6 AM UTC:
1. Scrapes fresh eBay listings
2. Generates new blog posts
3. Builds the Hugo site
4. Deploys to GitHub Pages
5. Commits generated content back to the repo

> **Note**: Requires `LLM_BASE_URL` to point to a cloud-accessible LLM endpoint (not localhost). Set secrets in **Settings → Secrets → Actions**.

## Tech Stack

- **Python 3.12+** — pipeline logic
- **Hugo** — static site generator
- **Ananke** — clean, responsive Hugo theme
- **GitHub Pages** — free hosting
- **GitHub Actions** — daily automation
- **OpenAI-compatible API** — content generation (any provider)

## License

Personal project. Not affiliated with eBay.
