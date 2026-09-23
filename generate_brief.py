import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


CONFIG_FILE = Path("config.yml")
SEEN_FILE = Path("seen_works.json")
OUTPUT_FILE = Path("weekly_brief.md")
ISSUE_OUTPUT_FILE = Path("issue_brief.md")

OPENALEX_API = "https://api.openalex.org"

# GitHub's actual Issue body limit is higher, but this safety margin leaves
# space for the truncation notice and avoids edge cases.
ISSUE_BODY_LIMIT = 60_000


def load_yaml(path: Path) -> dict[str, Any]:
    """Load and validate the YAML configuration file."""
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping.")

    return data


def load_seen_works():
    """Load previously processed OpenAlex Work IDs."""
    if not SEEN_FILE.exists():
        return set()

    try:
        with SEEN_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            return set()

        return {str(item) for item in data}

    except (OSError, json.JSONDecodeError, TypeError):
        return set()


def save_seen_works(work_ids: set[str]) -> None:
    """Save processed OpenAlex Work IDs in deterministic order."""
    with SEEN_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            sorted(work_ids),
            file,
            ensure_ascii=False,
            indent=2,
        )
        file.write("\n")


def build_http_session() -> requests.Session:
    """Create an HTTP session with retry and backoff behaviour."""
    retry_policy = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.0,
        status_forcelist=(
            429,
            500,
            502,
            503,
            504,
        ),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )

    session = requests.Session()

    session.mount(
        "https://",
        HTTPAdapter(max_retries=retry_policy),
    )

    session.headers.update(
        {
            "User-Agent": "research-intelligence-brief/2.0",
        }
    )

    return session


def reconstruct_abstract(inverted_index: Any) -> str:
    """Reconstruct an abstract from the OpenAlex inverted index."""
    if not isinstance(inverted_index, dict):
        return ""

    positions: list[tuple[int, str]] = []

    for word, indexes in inverted_index.items():
        if not isinstance(indexes, list):
            continue

        for index in indexes:
            if isinstance(index, int):
                positions.append((index, str(word)))

    positions.sort(key=lambda item: item[0])

    abstract = " ".join(
        word
        for _, word in positions
    )

    return html.unescape(abstract).strip()


def normalise_text(text: Any) -> str:
    """Normalise text for case-insensitive keyword matching."""
    if not text:
        return ""

    value = html.unescape(str(text)).lower()
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def keyword_is_present(keyword: str, text: str) -> bool:
    """Check whether a keyword occurs in the searchable text."""
    keyword_normalised = normalise_text(keyword)

    if not keyword_normalised:
        return False

    # Short abbreviations such as AFM must be matched as complete words.
    if (
        len(keyword_normalised) <= 4
        and keyword_normalised.isalpha()
    ):
        pattern = rf"\b{re.escape(keyword_normalised)}\b"

        return (
            re.search(
                pattern,
                text,
                flags=re.IGNORECASE,
            )
            is not None
        )

    return keyword_normalised in text


def score_work(
    work: dict[str, Any],
    config: dict[str, Any],
    tracked_author: str,
) -> dict[str, Any]:
    """Calculate the keyword relevance score for one work."""
    abstract = reconstruct_abstract(
        work.get("abstract_inverted_index")
    )

    topics = work.get("topics") or []
    metadata_keywords = work.get("keywords") or []

    topic_text = " ".join(
        str(item.get("display_name", ""))
        for item in topics
        if isinstance(item, dict)
    )

    keyword_text = " ".join(
        str(item.get("display_name", ""))
        for item in metadata_keywords
        if isinstance(item, dict)
    )

    searchable_text = normalise_text(
        " ".join(
            [
                str(work.get("title") or ""),
                abstract,
                topic_text,
                keyword_text,
            ]
        )
    )

    keyword_config = config.get("keywords") or {}

    high_priority = (
        keyword_config.get("high_priority")
        or []
    )

    medium_priority = (
        keyword_config.get("medium_priority")
        or []
    )

    exclusions = (
        keyword_config.get("exclusion")
        or []
    )

    scoring = config.get("scoring") or {}

    author_score = int(
        scoring.get("tracked_author", 2)
    )

    high_score = int(
        scoring.get("high_priority", 3)
    )

    medium_score = int(
        scoring.get("medium_priority", 1)
    )

    exclusion_score = int(
        scoring.get("exclusion", -4)
    )

    score = author_score

    matched_high: list[str] = []
    matched_medium: list[str] = []
    matched_exclusion: list[str] = []

    for keyword in high_priority:
        keyword = str(keyword)

        if keyword_is_present(
            keyword,
            searchable_text,
        ):
            score += high_score
            matched_high.append(keyword)

    for keyword in medium_priority:
        keyword = str(keyword)

        if keyword_is_present(
            keyword,
            searchable_text,
        ):
            score += medium_score
            matched_medium.append(keyword)

    for keyword in exclusions:
        keyword = str(keyword)

        if keyword_is_present(
            keyword,
            searchable_text,
        ):
            score += exclusion_score
            matched_exclusion.append(keyword)

    return {
        "score": score,
        "matched_high": matched_high,
        "matched_medium": matched_medium,
        "matched_exclusion": matched_exclusion,
        "abstract": abstract,
        "tracked_authors": [tracked_author],
    }


def clean_openalex_id(author_id: str) -> str:
    """Validate and normalise an OpenAlex Author ID."""
    value = author_id.strip().rstrip("/")

    if value.startswith("https://openalex.org/"):
        value = value.rsplit("/", 1)[-1]

    if not re.fullmatch(r"A\d+", value):
        raise ValueError(
            f"Invalid OpenAlex author ID: {author_id!r}"
        )

    return value


def fetch_recent_works(
    session: requests.Session,
    author_id: str,
    start_date: str,
    end_date: str,
    contact_email: str,
) -> list[dict[str, Any]]:
    """Retrieve recent works for one OpenAlex author."""
    clean_id = clean_openalex_id(author_id)

    filters = (
        f"authorships.author.id:{clean_id},"
        f"from_publication_date:{start_date},"
        f"to_publication_date:{end_date}"
    )

    params = {
        "filter": filters,
        "per_page": 100,
        "sort": "publication_date:desc",
        "select": (
            "id,title,doi,publication_date,"
            "authorships,primary_location,"
            "abstract_inverted_index,topics,"
            "keywords,type"
        ),
    }

    if contact_email:
        params["mailto"] = contact_email

    response = session.get(
        f"{OPENALEX_API}/works",
        params=params,
        timeout=45,
    )

    response.raise_for_status()

    payload = response.json()
    results = payload.get("results") or []

    return [
        item
        for item in results
        if isinstance(item, dict)
    ]


def get_primary_source(
    work: dict[str, Any],
) -> str:
    """Return the journal or primary source name."""
    primary_location = (
        work.get("primary_location")
        or {}
    )

    source = (
        primary_location.get("source")
        or {}
    )

    return str(
        source.get("display_name")
        or "Unknown source"
    )


def get_authors(
    work: dict[str, Any],
) -> list[str]:
    """Extract author display names."""

    names: list[str] = []

    for authorship in work.get("authorships") or [\]:
        if not isinstance(authorship, dict):
            continue

        author = authorship.get("author") or {}
        name = author.get("display_name")

        if name:
            names.append(str(name))

    return names

def get_work_url(
    work: dict[str, Any],
) -> str:
    """Return the DOI, landing page, or OpenAlex URL."""
    doi = work.get("doi")

    if doi:
        return str(doi)

    primary_location = (
        work.get("primary_location")
        or {}
    )

    landing_page = (
        primary_location.get("landing_page_url")
    )

    return str(
        landing_page
        or work.get("id")
        or ""
    )


def truncate_text(
    text: str,
    maximum_length: int,
) -> str:
    """Shorten an abstract while preferring a sentence boundary."""
    value = re.sub(
        r"\s+",
        " ",
        text or "",
    ).strip()

    if not value:
        return "No abstract is available from OpenAlex."

    if len(value) <= maximum_length:
        return value

    shortened = value[:maximum_length]
    sentence_end = shortened.rfind(". ")

    if sentence_end >= maximum_length // 3:
        shortened = shortened[: sentence_end + 1]

    return shortened.rstrip() + " ..."


def relevance_label(score: int) -> str:
    """Convert a numerical score into a report section."""
    if score >= 8:
        return "Highly relevant"

    if score >= 4:
        return "Relevant"

    return "Possibly relevant"


def format_authors(
    authors: list[str],
    maximum_displayed: int = 8,
) -> str:
    """Format the author list without producing very long lines."""
    if not authors:
        return "Unavailable"

    displayed = authors[:maximum_displayed]
    result = ", ".join(displayed)

    remaining = len(authors) - len(displayed)

    if remaining > 0:
        result += f", and {remaining} more"

    return result


def create_report(
    config: dict[str, Any],
    papers: list[dict[str, Any]],
    start_date: str,
    end_date: str,
) -> str:
    """Create the complete Markdown research brief."""
    brief_config = config.get("brief") or {}

    title = str(
        brief_config.get("title")
        or "Research Intelligence Brief"
    )

    abstract_length = int(
        brief_config.get(
            "abstract_maximum_characters",
            500,
        )
    )

    lines = [
        f"# {title}",
        "",
        f"**Search period:** {start_date} to {end_date}",
        "",
        f"**Papers selected:** {len(papers)}",
        "",
        "> This report was generated automatically from "
        "OpenAlex metadata. Relevance scores are based on "
        "repository keyword rules and do not represent "
        "scientific quality, impact, or peer evaluation.",
        "",
    ]

    if not papers:
        lines.extend(
            [
                "No new papers met the minimum "
                "relevance threshold.",
                "",
            ]
        )

        return "\n".join(lines)

    grouped = {
        "Highly relevant": [],
        "Relevant": [],
        "Possibly relevant": [],
    }

    for paper in papers:
        score = int(
            paper["analysis"]["score"]
        )

        label = relevance_label(score)
        grouped[label].append(paper)

    paper_number = 1

    for category in (
        "Highly relevant",
        "Relevant",
        "Possibly relevant",
    ):
        category_papers = grouped[category]

        if not category_papers:
            continue

        lines.extend(
            [
                f"## {category}",
                "",
            ]
        )

        for paper in category_papers:
            work = paper["work"]
            analysis = paper["analysis"]

            authors = get_authors(work)

            matched_keywords = (
                analysis["matched_high"]
                + analysis["matched_medium"]
            )

            if matched_keywords:
                matched_text = ", ".join(
                    f"`{keyword}`"
                    for keyword in matched_keywords
                )
            else:
                matched_text = (
                    "No research keyword matched; retained "
                    "because a tracked author is present"
                )

            tracked_authors = "; ".join(
                analysis["tracked_authors"]
            )

            lines.extend(
                [
                    (
                        f"### {paper_number}. "
                        f"{work.get('title') or 'Untitled'}"
                    ),
                    "",
                    (
                        f"- **Tracked author:** "
                        f"{tracked_authors}"
                    ),
                    (
                        f"- **Authors:** "
                        f"{format_authors(authors)}"
                    ),
                    (
                        f"- **Publication date:** "
                        f"{work.get('publication_date') or 'Unknown'}"
                    ),
                    (
                        f"- **Journal or source:** "
                        f"{get_primary_source(work)}"
                    ),
                    (
                        f"- **Document type:** "
                        f"{work.get('type') or 'Unknown'}"
                    ),
                    (
                        f"- **Relevance score:** "
                        f"{analysis['score']}"
                    ),
                    (
                        f"- **Matched keywords:** "
                        f"{matched_text}"
                    ),
                ]
            )

            if analysis["matched_exclusion"]:
                exclusion_text = ", ".join(
                    f"`{keyword}`"
                    for keyword
                    in analysis["matched_exclusion"]
                )

                lines.append(
                    "- **Matched exclusion keywords:** "
                    f"{exclusion_text}"
                )

            work_url = get_work_url(work)

            if work_url:
                lines.append(
                    f"- **Paper URL:** {work_url}"
                )

            lines.extend(
                [
                    "",
                    "**Abstract:**",
                    "",
                    truncate_text(
                        analysis["abstract"],
                        abstract_length,
                    ),
                    "",
                    "---",
                    "",
                ]
            )

            paper_number += 1

    return "\n".join(lines)


def create_issue_report(
    full_report: str,
    limit: int = ISSUE_BODY_LIMIT,
) -> str:
    """
    Create a shortened report suitable for a GitHub Issue.

    The complete report remains available in weekly_brief.md.
    """
    if len(full_report) <= limit:
        return full_report

    notice = (
        "\n\n---\n\n"
        "_This issue contains a shortened version because "
        "GitHub limits issue body size. Download the workflow "
        "artefact or open `weekly_brief.md` for the complete "
        "report._\n"
    )

    available = max(
        0,
        limit - len(notice),
    )

    shortened = full_report[:available]

    # Prefer ending after a complete paper entry.
    last_separator = shortened.rfind("\n---\n")

    if last_separator > available // 2:
        shortened = shortened[:last_separator]

    return shortened.rstrip() + notice


def main() -> int:
    """Run the complete research intelligence workflow."""
    if not CONFIG_FILE.exists():
        print(
            "Error: config.yml was not found.",
            file=sys.stderr,
        )
        return 1

    try:
        config = load_yaml(CONFIG_FILE)

    except (
        OSError,
        ValueError,
        yaml.YAMLError,
    ) as error:
        print(
            f"Error loading config.yml: {error}",
            file=sys.stderr,
        )
        return 1

    brief_config = config.get("brief") or {}

    lookback_days = int(
        brief_config.get("lookback_days", 10)
    )

    minimum_score = int(
        brief_config.get("minimum_score", 4)
    )

    maximum_papers = int(
        brief_config.get("maximum_papers", 15)
    )

    if (
        lookback_days < 1
        or maximum_papers < 1
    ):
        print(
            "Error: lookback_days and maximum_papers "
            "must be positive.",
            file=sys.stderr,
        )
        return 1

    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(
        days=lookback_days
    )

    start_date_text = start_date.isoformat()
    end_date_text = today.isoformat()

    contact_email = os.getenv(
        "OPENALEX_EMAIL",
        "",
    ).strip()

    seen_works = load_seen_works()
    processed_works = set(seen_works)

    collected: dict[
        str,
        dict[str, Any],
    ] = {}

    authors = config.get("authors") or []

    if (
        not isinstance(authors, list)
        or not authors
    ):
        print(
            "Error: config.yml contains no authors.",
            file=sys.stderr,
        )
        return 1

    session = build_http_session()
    successful_queries = 0

    for tracked_author in authors:
        if not isinstance(
            tracked_author,
            dict,
        ):
            print(
                "Skipping invalid author entry: "
                f"{tracked_author!r}"
            )
            continue

        name = str(
            tracked_author.get("name")
            or ""
        ).strip()

        author_id = str(
            tracked_author.get("openalex_id")
            or ""
        ).strip()

        if not name or not author_id:
            print(
                "Skipping incomplete author configuration: "
                f"{tracked_author}"
            )
            continue

        print(
            f"Querying {name} ({author_id})"
        )

        try:
            works = fetch_recent_works(
                session=session,
                author_id=author_id,
                start_date=start_date_text,
                end_date=end_date_text,
                contact_email=contact_email,
            )

            successful_queries += 1

        except (
            ValueError,
            requests.RequestException,
            json.JSONDecodeError,
        ) as error:
            print(
                f"Failed to query {name}: {error}",
                file=sys.stderr,
            )
            continue

        for work in works:
            work_id = str(
                work.get("id")
                or ""
            ).strip()

            if not work_id:
                continue

            if work_id in seen_works:
                continue

            processed_works.add(work_id)

            # If two tracked PIs co-authored the same paper,
            # keep one paper entry and record both names.
            existing = collected.get(work_id)

            if existing:
                tracked_names = (
                    existing["analysis"][
                        "tracked_authors"
                    ]
                )

                if name not in tracked_names:
                    tracked_names.append(name)

                continue

            analysis = score_work(
                work=work,
                config=config,
                tracked_author=name,
            )

            if (
                int(analysis["score"])
                < minimum_score
            ):
                continue

            collected[work_id] = {
                "work": work,
                "analysis": analysis,
            }

        time.sleep(0.25)

    # Do not update seen_works.json if every API query failed.
    if successful_queries == 0:
        print(
            "Error: all OpenAlex author queries failed. "
            "State was not changed.",
            file=sys.stderr,
        )
        return 1

    papers = list(collected.values())

    papers.sort(
        key=lambda item: (
            int(
                item["analysis"]["score"]
            ),
            str(
                item["work"].get(
                    "publication_date"
                )
                or ""
            ),
        ),
        reverse=True,
    )

    papers = papers[:maximum_papers]

    report = create_report(
        config=config,
        papers=papers,
        start_date=start_date_text,
        end_date=end_date_text,
    )

    issue_report = create_issue_report(
        report
    )

    OUTPUT_FILE.write_text(
        report,
        encoding="utf-8",
    )

    ISSUE_OUTPUT_FILE.write_text(
        issue_report,
        encoding="utf-8",
    )

    save_seen_works(processed_works)

    print(
        f"Generated full report: {OUTPUT_FILE}"
    )

    print(
        f"Generated issue report: "
        f"{ISSUE_OUTPUT_FILE}"
    )

    print(
        f"Papers included: {len(papers)}"
    )

    print(
        f"Full report characters: "
        f"{len(report)}"
    )

    print(
        f"Issue report characters: "
        f"{len(issue_report)}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
