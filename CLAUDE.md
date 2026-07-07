# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Scraper for the Nowe Horyzonty film festival website (https://www.nowehoryzonty.pl/menu.do?id=3755) to extract film data — titles, screening dates/times, and availability — to help optimize ticket purchasing.

## Setup & Commands

This project uses `uv` with Python 3.14 and a `.venv` managed by uv.

```bash
# Install dependencies
uv sync

# Run
uv run main.py

# Add a dependency
uv add <package>
```

## Architecture

The project is at the very start — `main.py` is a placeholder. The intended direction:

- **Scraping target**: `https://www.nowehoryzonty.pl/menu.do?id=3755` — film listings page, including per-film screening schedules
- **Goal**: structured data (film title, screening dates/times, venue/hall, ticket availability) suitable for date-based filtering
- **Future**: a web app frontend, but that is out of scope for now — focus is on producing clean, filterable data

When implementing the scraper, prefer `httpx` + `beautifulsoup4` or `playwright` if JavaScript rendering is needed. Store scraped results as JSON or SQLite for easy querying by date.
