import json
import re
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://www.nowehoryzonty.pl/"
PROGRAM_PAGE = urljoin(BASE_URL, "menu.do?id=3755")
HEADERS = {"Accept-Language": "pl-PL,pl;q=0.9"}


@dataclass
class Screening:
    screening_id: str
    datetime_text: str  # e.g. "sb 25 lip, 22:30"
    venue: str
    venue_short: str
    available: bool  # True if "koszyk activ" class present


@dataclass
class Film:
    slug: str          # URL slug or "article-{id}-{n}" for article-sourced films
    url: str           # canonical URL (program/26/slug or akt.do article)
    title: str
    director: str = ""
    title_original: str = ""
    description: str = ""
    prizes: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    screenings: list[Screening] = field(default_factory=list)


def fetch(client: httpx.Client, url: str) -> BeautifulSoup:
    resp = client.get(url, headers=HEADERS, follow_redirects=True)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


# ---------------------------------------------------------------------------
# Section index scraping (full program, available after July 7)
# ---------------------------------------------------------------------------

def get_program_sections(client: httpx.Client) -> list[tuple[str, str]]:
    """Return list of (section_name, section_url) from /program/sekcje."""
    soup = fetch(client, urljoin(BASE_URL, "program/sekcje"))
    sections = []
    for a in soup.select("a.cykl-kafel"):
        href = a.get("href", "")
        if not href.startswith("program/index/"):
            continue
        name = href.split("/")[-1]
        sections.append((name, urljoin(BASE_URL, href)))
    return sections


def get_section_name(soup: BeautifulSoup, fallback: str) -> str:
    title_el = soup.select_one("title")
    if title_el:
        text = title_el.get_text(strip=True).split("|")[0].strip()
        if text:
            return text
    return fallback


def parse_screenings(row: Tag) -> list[Screening]:
    screenings = []
    for senpoz in row.select(".senpozycja"):
        ids = senpoz.get("data-ids", "")
        time_el = senpoz.select_one(".st")
        venue_el = senpoz.select_one("a.sa")
        cart_el = senpoz.select_one("a.koszyk")

        datetime_text = time_el.get_text(strip=True) if time_el else ""
        venue = venue_el.get("title", "") if venue_el else ""
        venue_short = venue_el.get_text(strip=True) if venue_el else ""
        available = cart_el is not None and "activ" in cart_el.get("class", [])

        screenings.append(Screening(
            screening_id=ids,
            datetime_text=datetime_text,
            venue=venue,
            venue_short=venue_short,
            available=available,
        ))
    return screenings


def scrape_section(client: httpx.Client, section_url: str, fallback_name: str) -> tuple[str, list[tuple[str, str, str, list[Screening]]]]:
    """Return (section_name, [(slug, url, title, screenings), ...])."""
    soup = fetch(client, section_url)
    section_name = get_section_name(soup, fallback_name)

    films = []
    for row in soup.select("div.wiersz"):
        title_el = row.select_one("td.tytulgl a, a.undlink")
        if not title_el:
            continue
        href = title_el.get("href", "")
        if not href.startswith("program/"):
            continue
        slug = href.split("/")[-1]
        title = title_el.get_text(strip=True)
        screenings = parse_screenings(row)
        films.append((slug, urljoin(BASE_URL, href), title, screenings))

    return section_name, films


def scrape_film_detail(client: httpx.Client, film_url: str) -> tuple[str, list[str]]:
    """Fetch a /program/26/slug page and return (description, prizes)."""
    soup = fetch(client, film_url)
    desc_el = soup.select_one("div.tresc.glownyop")
    desc = desc_el.get_text(separator="\n", strip=True) if desc_el else ""

    prizes = [li.get_text(strip=True) for li in soup.select(".nagrody li") if li.get_text(strip=True)]

    return desc, prizes


# ---------------------------------------------------------------------------
# Article scraping (pre-program announcements on akt.do pages)
# ---------------------------------------------------------------------------

def get_article_urls(client: httpx.Client) -> list[tuple[str, str]]:
    """Return list of (section_name, article_url) from the main program page."""
    soup = fetch(client, PROGRAM_PAGE)
    seen: set[str] = set()
    articles = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "akt.do" not in href:
            continue
        # Normalise: strip trailing &, skip English versions
        href = href.rstrip("&")
        if "lang=en" in href:
            continue
        # Deduplicate by article id
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        art_id = qs.get("id", [None])[0]
        if not art_id or art_id in seen:
            continue
        seen.add(art_id)
        section_name = a.get_text(strip=True) or href
        # Ensure absolute URL
        if not href.startswith("http"):
            href = urljoin(BASE_URL, href)
        articles.append((section_name, href))
    return articles


def parse_h4_title(h4_text: str) -> tuple[str, str, str]:
    """
    Parse "Polish Title(Original Title), reż. Director Name"
    Returns (title_pl, title_orig, director).
    """
    # Split off director
    if ", reż. " in h4_text:
        title_part, director = h4_text.split(", reż. ", 1)
    elif " reż. " in h4_text:
        title_part, director = h4_text.split(" reż. ", 1)
    else:
        title_part, director = h4_text, ""

    director = director.strip()

    # Extract original title from parentheses at end of title_part
    m = re.match(r"^(.*?)\(([^)]+)\)\s*$", title_part.strip())
    if m:
        title_pl = m.group(1).strip().rstrip(",").strip()
        title_orig = m.group(2).strip()
    else:
        title_pl = title_part.strip().rstrip(",").strip()
        title_orig = ""

    return title_pl, title_orig, director


SKIP_P_CLASSES = {"podpis", "imgpodpis", "small"}


def scrape_article(client: httpx.Client, section_name: str, article_url: str) -> list[Film]:
    """Extract film entries from an announcement article page."""
    soup = fetch(client, article_url)

    parsed = urlparse(article_url)
    art_id = parse_qs(parsed.query).get("id", ["unknown"])[0]

    films = []
    film_index = 0

    # h4 elements end up as children of <body> due to HTML parser quirks
    for h4 in soup.find_all("h4"):
        h4_text = h4.get_text(strip=True)
        if "reż." not in h4_text:
            continue

        title_pl, title_orig, director = parse_h4_title(h4_text)

        # Collect description from <p> siblings until the next <h4>
        desc_parts = []
        for sibling in h4.next_siblings:
            if not isinstance(sibling, Tag):
                continue
            if sibling.name == "h4":
                break
            if sibling.name == "p":
                classes = set(sibling.get("class", []))
                if classes & SKIP_P_CLASSES:
                    continue
                text = sibling.get_text(strip=True)
                if text:
                    desc_parts.append(text)

        films.append(Film(
            slug=f"article-{art_id}-{film_index}",
            url=article_url,
            title=title_pl,
            title_original=title_orig,
            director=director,
            description="\n".join(desc_parts),
            sections=[section_name],
        ))
        film_index += 1

    return films


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def scrape_all() -> list[Film]:
    films: dict[str, Film] = {}

    with httpx.Client(timeout=30) as client:

        # 1. Section index pages (full schedule, available after July 7)
        sections = get_program_sections(client)
        if sections:
            print(f"Found {len(sections)} program section(s).")
            for fallback_name, section_url in sections:
                print(f"  Scraping section: {section_url}")
                try:
                    section_name, entries = scrape_section(client, section_url, fallback_name)
                except Exception as e:
                    print(f"    Error: {e}")
                    continue

                print(f"    '{section_name}': {len(entries)} film(s)")
                for slug, url, title, screenings in entries:
                    if slug not in films:
                        films[slug] = Film(slug=slug, url=url, title=title)
                    film = films[slug]
                    if section_name not in film.sections:
                        film.sections.append(section_name)
                    existing_ids = {s.screening_id for s in film.screenings}
                    for s in screenings:
                        if s.screening_id not in existing_ids:
                            film.screenings.append(s)
                            existing_ids.add(s.screening_id)
                time.sleep(0.3)

            print(f"\nFetching descriptions for {len(films)} section film(s)...")
            for film in films.values():
                try:
                    film.description, film.prizes = scrape_film_detail(client, film.url)
                    print(f"  {film.title}: {len(film.description)} chars, {len(film.prizes)} prize(s)")
                except Exception as e:
                    print(f"  {film.title}: error — {e}")
                time.sleep(0.3)
        else:
            print("No section index pages yet — full program not published.")

        # 2. Announcement articles (available now)
        article_urls = get_article_urls(client)
        print(f"\nFound {len(article_urls)} announcement article(s).")
        for section_name, article_url in article_urls:
            print(f"  Scraping article: {section_name} ({article_url})")
            try:
                article_films = scrape_article(client, section_name, article_url)
            except Exception as e:
                print(f"    Error: {e}")
                continue

            print(f"    {len(article_films)} film(s) found")
            for film in article_films:
                # Don't add if same film already captured from section index
                # (match by title + director)
                key = f"{film.title}|{film.director}"
                existing = next(
                    (f for f in films.values() if f"{f.title}|{f.director}" == key),
                    None,
                )
                if existing:
                    if section_name not in existing.sections:
                        existing.sections.append(section_name)
                    if not existing.description:
                        existing.description = film.description
                else:
                    films[film.slug] = film
            time.sleep(0.3)

    return list(films.values())


def main():
    print("Scraping Nowe Horyzonty 2026 film data...")
    films = scrape_all()

    if not films:
        print("No films scraped.")
        return

    data = [asdict(f) for f in films]
    output_path = "films.json"
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)

    print(f"\nDone. {len(films)} film(s) saved to {output_path}.")
    for film in films:
        orig = f" / {film.title_original}" if film.title_original else ""
        director = f", reż. {film.director}" if film.director else ""
        print(f"  {film.title}{orig}{director} [{', '.join(film.sections)}]")


if __name__ == "__main__":
    main()
