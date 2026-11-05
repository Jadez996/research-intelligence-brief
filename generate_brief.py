"""Weekly research intelligence brief.

Phase 1 adds deterministic, metadata-only topic discovery alongside the
existing tracked-author route. The normal pipeline uses OpenAlex metadata and
local scoring only; it never calls an LLM and never downloads PDFs.

Data flow:
    OpenAlex (tracked authors + configured topic queries)
        -> deduplication and deterministic filtering/scoring
        -> weekly_papers.json / weekly_papers.csv (authoritative)
        -> weekly_brief.md (complete) / issue_brief.md (executive summary)
        -> seen_works.json (processed *qualifying* papers only)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import sys
import time
from collections import Counter
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
PAPERS_JSON_FILE = Path("weekly_papers.json")
PAPERS_CSV_FILE = Path("weekly_papers.csv")
RUN_MANIFEST_FILE = Path("run_manifest.json")

OPENALEX_API = "https://api.openalex.org"

# GitHub's actual Issue body limit is higher. This safety margin leaves room
# for a notice; the issue is an executive summary, not the full database.
ISSUE_BODY_LIMIT = 60_000
DEFAULT_ISSUE_LIMIT = 55_000

# Set in main() so report code can reference the chosen output directory.
OUTPUT_DIR = Path(".")

# Fields requested from OpenAlex. `select` supports root-level fields; the
# nested authorships/topics/keywords objects are returned in full.
WORK_SELECT = (
    "id,title,doi,publication_date,publication_year,"
    "authorships,primary_location,abstract_inverted_index,"
    "topics,keywords,type,cited_by_count"
)


class StateError(RuntimeError):
    """Raised when persistent state cannot be read safely."""


def load_yaml(path: Path) -> dict[str, Any]:
    """Load and validate a YAML configuration file."""
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping.")

    return data


def out_path(name: str) -> Path:
    """Return a path inside the configured output directory."""
    return OUTPUT_DIR / name


def atomic_write_text(path: Path, text: str) -> None:
    """Write a UTF-8 file atomically in the destination directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            # OneDrive/antivirus can briefly lock a just-created file.
            time.sleep(0.1 * (attempt + 1))


def load_seen_works(path: Path = SEEN_FILE) -> set[str]:
    """Load processed Work IDs, refusing to silently reset corrupt state."""
    path = Path(path)
    if not path.exists():
        return set()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(
            f"Could not read {path}: {error}. State was not changed; "
            "restore the file from Git or fix it before rerunning."
        ) from error

    if not isinstance(data, list):
        raise StateError(
            f"{path} must contain a JSON list of Work IDs. "
            "State was not changed."
        )

    if any(not isinstance(item, str) for item in data):
        raise StateError(
            f"{path} contains a non-string Work ID. State was not changed."
        )

    return {item for item in data if item}


def save_seen_works(work_ids: set[str], path: Path = SEEN_FILE) -> None:
    """Save processed Work IDs atomically in deterministic order."""
    payload = json.dumps(
        sorted(work_ids),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    atomic_write_text(Path(path), payload)


class CountingSession(requests.Session):
    """Requests session that records route, retry, and provider cost headers."""

    def __init__(self) -> None:
        super().__init__()
        self.stats: dict[str, Any] = {
            "requests": 0,
            "failed": 0,
            "by_route": Counter(),
            "failed_by_route": Counter(),
            "retries": 0,
            "cost_usd": 0.0,
            "cost_available": True,
            "credits": 0,
            "credits_available": True,
            "remaining_usd": None,
        }
        self.current_route = "other"

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        route = str(getattr(self, "current_route", "other") or "other")
        self.stats["requests"] += 1
        self.stats["by_route"][route] += 1

        try:
            response = super().request(method, url, **kwargs)
        except requests.RequestException:
            self.stats["failed"] += 1
            self.stats["failed_by_route"][route] += 1
            raise

        if int(getattr(response, "status_code", 200) or 200) >= 400:
            self.stats["failed"] += 1
            self.stats["failed_by_route"][route] += 1

        headers = getattr(response, "headers", {}) or {}

        cost_header = headers.get("X-RateLimit-Cost-USD")
        if cost_header is None:
            self.stats["cost_available"] = False
        else:
            try:
                self.stats["cost_usd"] += float(cost_header)
            except (TypeError, ValueError):
                self.stats["cost_available"] = False

        credits_header = headers.get("X-RateLimit-Credits-Used")
        if credits_header is None:
            self.stats["credits_available"] = False
        else:
            try:
                self.stats["credits"] += int(float(credits_header))
            except (TypeError, ValueError):
                self.stats["credits_available"] = False

        remaining_header = headers.get("X-RateLimit-Remaining-USD")
        if remaining_header is not None:
            try:
                self.stats["remaining_usd"] = float(remaining_header)
            except (TypeError, ValueError):
                pass

        raw = getattr(response, "raw", None)
        retry_state = getattr(raw, "retries", None)
        retry_history = getattr(retry_state, "history", None)
        if retry_history is not None:
            try:
                self.stats["retries"] += len(retry_history)
            except TypeError:
                pass

        return response


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

    session = CountingSession()
    session.mount("https://", HTTPAdapter(max_retries=retry_policy))
    session.headers.update(
        {"User-Agent": "research-intelligence-brief/2.0"}
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
    abstract = " ".join(word for _, word in positions)

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
    if len(keyword_normalised) <= 4 and keyword_normalised.isalpha():
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
    """Calculate keyword/tracked-author evidence for one work."""
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))

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
    high_priority = keyword_config.get("high_priority") or []
    medium_priority = keyword_config.get("medium_priority") or []
    exclusions = keyword_config.get("exclusion") or []

    scoring = config.get("scoring") or {}
    author_score = (
        int(scoring.get("tracked_author", 2))
        if tracked_author
        else 0
    )
    high_score = int(scoring.get("high_priority", 3))
    medium_score = int(scoring.get("medium_priority", 1))
    exclusion_score = int(scoring.get("exclusion", -4))

    matched_high: list[str] = []
    matched_medium: list[str] = []
    matched_exclusion: list[str] = []

    for keyword in high_priority:
        keyword = str(keyword)
        if keyword_is_present(keyword, searchable_text):
            matched_high.append(keyword)
    for keyword in medium_priority:
        keyword = str(keyword)
        if keyword_is_present(keyword, searchable_text):
            matched_medium.append(keyword)
    for keyword in exclusions:
        keyword = str(keyword)
        if keyword_is_present(keyword, searchable_text):
            matched_exclusion.append(keyword)

    score = (
        author_score
        + high_score * len(matched_high)
        + medium_score * len(matched_medium)
        + exclusion_score * len(matched_exclusion)
    )

    components = {
        "tracked_author": author_score,
        "high": high_score * len(matched_high),
        "medium": medium_score * len(matched_medium),
        "exclusion": exclusion_score * len(matched_exclusion),
        "query": 0,
        "recency": 0,
        "total": score,
    }

    return {
        "score": score,
        "components": components,
        "matched_high": matched_high,
        "matched_medium": matched_medium,
        "matched_exclusion": matched_exclusion,
        "abstract": abstract,
        "tracked_authors": [tracked_author] if tracked_author else [],
    }


def short_id(value: Any) -> str:
    """Return the bare OpenAlex ID from a URL or bare ID."""
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def clean_openalex_id(author_id: str) -> str:
    """Validate and normalise an OpenAlex Author ID."""
    value = str(author_id).strip().rstrip("/")

    if value.startswith("https://openalex.org/"):
        value = value.rsplit("/", 1)[-1]

    if not re.fullmatch(r"A\d+", value):
        raise ValueError(f"Invalid OpenAlex author ID: {author_id!r}")

    return value


def openalex_common_params(contact_email: str) -> dict[str, str]:
    """Return optional polite-pool/auth parameters for OpenAlex."""
    params: dict[str, str] = {}
    if contact_email:
        params["mailto"] = contact_email

    api_key = os.getenv("OPENALEX_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key

    return params


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
        "select": WORK_SELECT,
    }
    params.update(openalex_common_params(contact_email))

    if hasattr(session, "current_route"):
        session.current_route = "seed"  # type: ignore[attr-defined]

    response = session.get(
        f"{OPENALEX_API}/works",
        params=params,
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results") or []

    return [item for item in results if isinstance(item, dict)]


def sanitise_query(query: Any) -> str:
    """Make a configured free-text query safe for an OpenAlex filter value."""
    value = str(query or "")
    value = value.replace('"', " ").replace(",", " ")
    return re.sub(r"\s+", " ", value).strip()


def fetch_topic_works(
    session: requests.Session,
    query: str,
    start_date: str,
    end_date: str,
    contact_email: str,
    results_per_query: int = 100,
    include_types: list[str] | None = None,
    exclude_retracted: bool = True,
) -> list[dict[str, Any]]:
    """Retrieve recent works for one configured topic query."""
    cleaned_query = sanitise_query(query)
    if not cleaned_query:
        return []

    filters = [
        f'title_abstract_keywords.search:"{cleaned_query}"',
        f"from_publication_date:{start_date}",
        f"to_publication_date:{end_date}",
    ]

    types = [
        str(item).strip()
        for item in (include_types or [])
        if str(item).strip()
    ]
    if types:
        filters.append("type:" + "|".join(types))
    if exclude_retracted:
        filters.append("is_retracted:false")

    params = {
        "filter": ",".join(filters),
        "per_page": min(100, max(1, int(results_per_query))),
        "sort": "publication_date:desc",
        "select": WORK_SELECT,
    }
    params.update(openalex_common_params(contact_email))

    if hasattr(session, "current_route"):
        session.current_route = "topic"  # type: ignore[attr-defined]

    response = session.get(
        f"{OPENALEX_API}/works",
        params=params,
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results") or []

    return [item for item in results if isinstance(item, dict)]


def get_primary_source(work: dict[str, Any]) -> str:
    """Return the journal or primary source name."""
    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    return str(source.get("display_name") or "Unknown source")


def get_authors(work: dict[str, Any]) -> list[str]:
    """Extract author display names."""
    names: list[str] = []

    for authorship in work.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue

        author = authorship.get("author") or {}
        name = author.get("display_name")
        if name:
            names.append(str(name))

    return names


def get_author_records(work: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract author IDs, names, positions, and institution names."""
    records: list[dict[str, Any]] = []

    for authorship in work.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue

        author = authorship.get("author") or {}
        name = author.get("display_name")
        if not name:
            continue

        institutions = []
        for institution in authorship.get("institutions") or []:
            if isinstance(institution, dict) and institution.get("display_name"):
                institutions.append(str(institution["display_name"]))

        records.append(
            {
                "id": short_id(author.get("id")),
                "name": str(name),
                "position": str(authorship.get("author_position") or ""),
                "institutions": institutions,
            }
        )

    return records


def get_topic_records(work: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract compact OpenAlex topic metadata."""
    records: list[dict[str, Any]] = []

    for topic in work.get("topics") or []:
        if not isinstance(topic, dict):
            continue
        records.append(
            {
                "id": short_id(topic.get("id")),
                "name": str(topic.get("display_name") or ""),
                "score": topic.get("score"),
            }
        )

    return records


def get_keyword_records(work: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract compact OpenAlex keyword metadata."""
    records: list[dict[str, Any]] = []

    for keyword in work.get("keywords") or []:
        if not isinstance(keyword, dict):
            continue
        records.append(
            {
                "name": str(keyword.get("display_name") or ""),
                "score": keyword.get("score"),
            }
        )

    return records


def get_work_url(work: dict[str, Any]) -> str:
    """Return the DOI, landing page, or OpenAlex URL."""
    doi = work.get("doi")
    if doi:
        return str(doi)

    primary_location = work.get("primary_location") or {}
    landing_page = primary_location.get("landing_page_url")

    return str(landing_page or work.get("id") or "")


def short_error(error: Exception, maximum_length: int = 300) -> str:
    """Keep operational warnings readable in Markdown and logs."""
    text = re.sub(r"\s+", " ", str(error or "")).strip()
    if len(text) <= maximum_length:
        return text
    return text[:maximum_length].rstrip() + " ..."


def truncate_text(text: str, maximum_length: int) -> str:
    """Shorten an abstract while preferring a sentence boundary."""
    value = re.sub(r"\s+", " ", text or "").strip()

    if not value:
        return "No abstract is available from OpenAlex."

    if len(value) <= maximum_length:
        return value

    shortened = value[:maximum_length]
    sentence_end = shortened.rfind(". ")

    if sentence_end >= maximum_length // 3:
        shortened = shortened[: sentence_end + 1]

    return shortened.rstrip() + " ..."


def relevance_label(score: int | float) -> str:
    """Convert a numerical score into a report section."""
    if score >= 8:
        return "Highly relevant"
    if score >= 4:
        return "Relevant"
    return "Possibly relevant"


def format_authors(authors: list[str], maximum_displayed: int = 8) -> str:
    """Format the author list without producing very long lines."""
    if not authors:
        return "Unavailable"

    displayed = authors[:maximum_displayed]
    result = ", ".join(displayed)
    remaining = len(authors) - len(displayed)

    if remaining > 0:
        result += f", and {remaining} more"

    return result


def doi_key(work: dict[str, Any]) -> str:
    """Return a normalised DOI for deduplication, if available."""
    value = str(work.get("doi") or "").strip().lower()
    if not value:
        return ""

    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break

    return value.strip()


def normalised_title_key(work: dict[str, Any]) -> str:
    """Return a punctuation-insensitive title key for deduplication."""
    value = normalise_text(work.get("title") or "")
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    return value


def work_state_ids(work: dict[str, Any]) -> set[str]:
    """Return stable IDs suitable for seen_works.json."""
    values: set[str] = set()

    raw = str(work.get("id") or "").strip()
    if raw:
        values.add(raw)
        return values

    doi = doi_key(work)
    if doi:
        values.add(f"doi:{doi}")
        return values

    title = normalised_title_key(work)
    if title:
        year = work.get("publication_year") or ""
        values.add(f"title:{title}|{year}")

    return values


def work_id_variants(work: dict[str, Any]) -> set[str]:
    """Return all IDs that can identify this work in seen state."""
    values = set(work_state_ids(work))

    raw = str(work.get("id") or "").strip()
    if raw:
        values.add(short_id(raw))

    doi = doi_key(work)
    if doi:
        values.add(f"doi:{doi}")

    title = normalised_title_key(work)
    if title:
        year = work.get("publication_year") or ""
        values.add(f"title:{title}|{year}")

    return values


def work_dedupe_keys(work: dict[str, Any]) -> set[str]:
    """Return keys used to detect duplicate records within one run."""
    return {f"key:{value}" for value in work_id_variants(work)}


def type_rank(work: dict[str, Any]) -> int:
    """Prefer a published article over a preprint when records duplicate."""
    return {
        "article": 4,
        "review": 4,
        "preprint": 3,
    }.get(str(work.get("type") or ""), 2)


def choose_better_work(
    old_work: dict[str, Any],
    new_work: dict[str, Any],
) -> bool:
    """Return True when the new record is at least as useful as the old one."""
    new_rank = type_rank(new_work)
    old_rank = type_rank(old_work)

    if new_rank != old_rank:
        return new_rank > old_rank

    new_abstract = len(reconstruct_abstract(new_work.get("abstract_inverted_index")))
    old_abstract = len(reconstruct_abstract(old_work.get("abstract_inverted_index")))

    if new_abstract != old_abstract:
        return new_abstract > old_abstract

    return str(new_work.get("publication_date") or "") > str(
        old_work.get("publication_date") or ""
    )


def find_tracked_authors(
    work: dict[str, Any],
    seed_by_id: dict[str, str],
) -> list[str]:
    """Find configured seed authors on a work."""
    names: set[str] = set()

    for authorship in work.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") or {}
        aid = short_id(author.get("id"))
        if aid in seed_by_id:
            names.add(seed_by_id[aid])

    return sorted(names)


def build_seed_map(
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return valid seed-author configs and an ID -> name map."""
    seeds: list[dict[str, Any]] = []
    seed_by_id: dict[str, str] = {}

    for author in config.get("authors") or []:
        if not isinstance(author, dict):
            continue

        name = str(author.get("name") or "").strip()
        author_id = str(author.get("openalex_id") or "").strip()

        if not name or not author_id:
            continue

        try:
            clean_id = clean_openalex_id(author_id)
        except ValueError:
            continue

        seeds.append(author)
        seed_by_id[clean_id] = name

    return seeds, seed_by_id


def is_recent(publication_date: Any, today: Any, days: int) -> bool:
    """Return True when a publication date is within the recent window."""
    if not publication_date or days < 0:
        return False

    try:
        parsed = datetime.strptime(
            str(publication_date)[:10],
            "%Y-%m-%d",
        ).date()
    except ValueError:
        return False

    return parsed >= today - timedelta(days=days)


def build_evidence(
    analysis: dict[str, Any],
    matched_queries: list[str],
    recent: bool,
    recent_days: int,
) -> str:
    """Build a compact, honest explanation of why a paper qualified."""
    parts: list[str] = []

    if analysis["matched_high"]:
        parts.append(
            "high-priority keywords: "
            + ", ".join(analysis["matched_high"])
        )
    if analysis["matched_medium"]:
        parts.append(
            "medium-priority keywords: "
            + ", ".join(analysis["matched_medium"])
        )
    if analysis["tracked_authors"]:
        parts.append(
            "tracked author(s): "
            + ", ".join(analysis["tracked_authors"])
        )
    if matched_queries:
        parts.append(
            "topic queries: " + ", ".join(matched_queries)
        )
    if recent:
        parts.append(f"recent publication (within {recent_days} days)")

    if not parts:
        return "Retrieved by a configured discovery route."

    return "; ".join(parts) + "."


class WorkCollector:
    """Collect, deduplicate, and score works from both discovery routes."""

    def __init__(
        self,
        config: dict[str, Any],
        settings: dict[str, Any],
        seed_by_id: dict[str, str],
        seen: set[str],
        today: Any,
    ) -> None:
        self.config = config
        self.settings = settings
        self.seed_by_id = seed_by_id
        self.seen = set(seen)
        self.today = today

        self.records: dict[str, dict[str, Any]] = {}
        self.dedupe_keys: dict[str, str] = {}

        self.stats: Counter[str] = Counter()

    def _is_seen(self, work: dict[str, Any]) -> bool:
        return any(
            value in self.seen
            for value in work_id_variants(work)
        )

    def _analyse(self, record: dict[str, Any]) -> None:
        work = record["work"]
        tracked = sorted(record["tracked_names"])

        analysis = score_work(
            work=work,
            config=self.config,
            tracked_author=tracked[0] if tracked else "",
        )
        analysis["tracked_authors"] = tracked

        query_bonus = (
            min(
                len(record["matched_queries"]),
                int(self.settings["max_query_bonus"]),
            )
            * float(self.settings["query_bonus"])
        )
        recent = is_recent(
            work.get("publication_date"),
            self.today,
            int(self.settings["recent_days"]),
        )
        recency_bonus = (
            float(self.settings["recent_bonus"]) if recent else 0.0
        )

        analysis["score"] = (
            float(analysis["score"]) + query_bonus + recency_bonus
        )
        analysis["components"]["query"] = query_bonus
        analysis["components"]["recency"] = recency_bonus
        analysis["components"]["total"] = analysis["score"]
        analysis["evidence"] = build_evidence(
            analysis,
            record["matched_queries"],
            recent,
            int(self.settings["recent_days"]),
        )

        record["analysis"] = analysis

    def _new_record(
        self,
        work: dict[str, Any],
        route: str,
        query: str | None,
    ) -> dict[str, Any]:
        state_ids = work_state_ids(work)
        key = (
            str(work.get("id") or "").strip()
            or sorted(state_ids)[0]
            if state_ids
            else f"untitled:{id(work)}"
        )

        record: dict[str, Any] = {
            "key": key,
            "work": work,
            "routes": {route},
            "matched_queries": [query] if query else [],
            "tracked_names": set(find_tracked_authors(work, self.seed_by_id)),
            "work_ids": set(state_ids),
        }
        self._analyse(record)

        self.records[key] = record
        for dedupe_key in work_dedupe_keys(work):
            self.dedupe_keys[dedupe_key] = key

        return record

    def _merge(
        self,
        record: dict[str, Any],
        work: dict[str, Any],
        route: str,
        query: str | None,
    ) -> None:
        record["routes"].add(route)
        if query and query not in record["matched_queries"]:
            record["matched_queries"].append(query)

        record["work_ids"].update(work_state_ids(work))
        record["tracked_names"].update(
            find_tracked_authors(work, self.seed_by_id)
        )

        if choose_better_work(record["work"], work):
            record["work"] = work

        self._analyse(record)

        for dedupe_key in work_dedupe_keys(work):
            self.dedupe_keys[dedupe_key] = record["key"]

    def add(
        self,
        work: dict[str, Any],
        route: str,
        query: str | None = None,
    ) -> None:
        """Add a retrieved work, ignoring duplicates and already-seen works."""
        if not isinstance(work, dict):
            return

        self.stats["retrieved"] += 1

        include_types = self.settings["include_types"]
        work_type = str(work.get("type") or "")
        if include_types and work_type and work_type not in include_types:
            self.stats["excluded_type"] += 1
            return

        if self._is_seen(work):
            self.stats["skipped_seen"] += 1
            return

        dedupe_keys = work_dedupe_keys(work)
        existing_key = next(
            (
                self.dedupe_keys[key]
                for key in dedupe_keys
                if key in self.dedupe_keys
            ),
            None,
        )

        if existing_key:
            self._merge(
                self.records[existing_key],
                work,
                route,
                query,
            )
            self.stats["duplicates"] += 1
            return

        self._new_record(work, route, query)
        self.stats["evaluated"] += 1

    def qualify(
        self,
        record: dict[str, Any],
    ) -> tuple[bool, str]:
        """Return whether a record qualifies and the rejection reason."""
        analysis = record["analysis"]

        if analysis["matched_exclusion"]:
            return False, "excluded_keyword"

        tracked = bool(analysis["tracked_authors"])
        topic_only = "topic" in record["routes"] and not tracked
        has_keyword = bool(
            analysis["matched_high"] or analysis["matched_medium"]
        )

        if (
            topic_only
            and bool(self.settings["require_keyword_match"])
            and not has_keyword
        ):
            return False, "topic_no_keyword"

        if float(analysis["score"]) < float(
            self.settings["minimum_score"]
        ):
            return False, "below_threshold"

        return True, ""


def build_candidate_authors(
    papers: list[dict[str, Any]],
    seed_ids: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Aggregate topic-paper authors, preserving one-paper candidates."""
    candidates: dict[str, dict[str, Any]] = {}

    for paper in papers:
        if "topic" not in paper["routes"]:
            continue

        work = paper["work"]
        seed_coauthor = bool(paper["analysis"]["tracked_authors"])

        for author in get_author_records(work):
            aid = str(author.get("id") or "")
            if not aid or aid in seed_ids:
                continue

            row = candidates.setdefault(
                aid,
                {
                    "id": aid,
                    "name": author["name"],
                    "papers": set(),
                    "matched_keywords": set(),
                    "matched_queries": set(),
                    "latest_publication_date": "",
                    "seed_coauthor": False,
                    "institutions": set(),
                },
            )

            row["papers"].add(paper["key"])
            row["matched_keywords"].update(
                paper["analysis"]["matched_high"]
                + paper["analysis"]["matched_medium"]
            )
            row["matched_queries"].update(paper["matched_queries"])
            row["institutions"].update(author.get("institutions") or [])

            publication_date = str(
                work.get("publication_date") or ""
            )
            if publication_date > row["latest_publication_date"]:
                row["latest_publication_date"] = publication_date

            if seed_coauthor:
                row["seed_coauthor"] = True

    rows: list[dict[str, Any]] = []
    for row in candidates.values():
        seed_coauthor = bool(row["seed_coauthor"])
        outside_network = not seed_coauthor
        evidence_parts = [
            f"appeared on {len(row['papers'])} topic-discovered paper(s)"
        ]
        if row["matched_keywords"]:
            evidence_parts.append(
                "matched keywords: "
                + ", ".join(sorted(row["matched_keywords"])[:6])
            )
        if seed_coauthor:
            evidence_parts.append(
                "coauthored with a tracked researcher on a topic paper"
            )
        else:
            evidence_parts.append(
                "no seed coauthorship observed in this run"
            )

        rows.append(
            {
                "id": row["id"],
                "name": row["name"],
                "topic_papers": len(row["papers"]),
                "paper_ids": sorted(row["papers"]),
                "matched_keywords": sorted(row["matched_keywords"]),
                "matched_queries": sorted(row["matched_queries"]),
                "latest_publication_date": row[
                    "latest_publication_date"
                ],
                "seed_coauthor": seed_coauthor,
                "outside_network": outside_network,
                "institutions": sorted(row["institutions"]),
                "status": "preliminary",
                "evidence": "; ".join(evidence_parts) + ".",
            }
        )

    rows.sort(
        key=lambda item: (
            item["outside_network"],
            item["topic_papers"],
            item["latest_publication_date"],
            len(item["matched_keywords"]),
        ),
        reverse=True,
    )

    return rows[: max(0, int(limit))]


def format_paper_entry(
    paper: dict[str, Any],
    number: int,
    abstract_length: int,
    compact: bool = False,
) -> list[str]:
    """Format one paper for the weekly report or the concise issue."""
    work = paper["work"]
    analysis = paper["analysis"]

    authors = get_authors(work)
    matched_keywords = (
        analysis["matched_high"] + analysis["matched_medium"]
    )

    if matched_keywords:
        matched_text = ", ".join(
            f"`{keyword}`" for keyword in matched_keywords
        )
    elif analysis["tracked_authors"]:
        matched_text = (
            "No configured keyword matched; retained because a "
            "tracked author is present."
        )
    else:
        matched_text = "No configured keyword matched."

    lines = [
        f"### {number}. {work.get('title') or 'Untitled'}",
        "",
    ]

    if analysis["tracked_authors"]:
        lines.append(
            "- **Tracked author(s):** "
            + "; ".join(analysis["tracked_authors"])
        )

    lines.extend(
        [
            f"- **Authors:** {format_authors(authors, 6 if compact else 8)}",
            (
                "- **Publication date:** "
                f"{work.get('publication_date') or 'Unknown'}"
            ),
            f"- **Journal or source:** {get_primary_source(work)}",
            f"- **Document type:** {work.get('type') or 'Unknown'}",
            f"- **Relevance score:** {analysis['score']}",
            (
                "- **Discovery routes:** "
                + ", ".join(sorted(paper["routes"]))
            ),
            f"- **Matched keywords:** {matched_text}",
        ]
    )

    if paper["matched_queries"]:
        lines.append(
            "- **Matched topic queries:** "
            + ", ".join(paper["matched_queries"])
        )

    lines.append(f"- **Evidence:** {analysis['evidence']}")

    if analysis["matched_exclusion"]:
        lines.append(
            "- **Matched exclusion keywords:** "
            + ", ".join(analysis["matched_exclusion"])
        )

    work_url = get_work_url(work)
    if work_url:
        lines.append(f"- **Paper URL:** {work_url}")

    if not compact and work.get("cited_by_count") is not None:
        lines.append(
            "- **Citations (context only, never scored):** "
            f"{work.get('cited_by_count')}"
        )

    lines.append("")

    if not compact:
        lines.extend(
            [
                "**Abstract:**",
                "",
                truncate_text(analysis["abstract"], abstract_length),
                "",
            ]
        )

    lines.extend(["---", ""])
    return lines


def render_papers(
    lines: list[str],
    papers: list[dict[str, Any]],
    abstract_length: int,
    compact: bool,
    start_number: int = 1,
) -> int:
    """Append formatted papers and return the next number."""
    number = start_number
    for paper in papers:
        lines.extend(
            format_paper_entry(
                paper,
                number,
                abstract_length,
                compact=compact,
            )
        )
        number += 1
    return number


def create_report(
    config: dict[str, Any],
    papers: list[dict[str, Any]],
    start_date: str,
    end_date: str,
    topic_start_date: str,
    run_summary: dict[str, Any],
    candidate_authors: list[dict[str, Any]],
    warnings: list[str],
    researcher_section: str | None = None,
    researcher_summary: dict[str, Any] | None = None,
) -> str:
    """Create the complete Markdown research brief."""
    brief_config = config.get("brief") or {}
    title = str(
        brief_config.get("title") or "Research Intelligence Brief"
    )
    abstract_length = int(
        brief_config.get("abstract_maximum_characters", 500)
    )

    lines = [
        f"# {title}",
        "",
        f"**Tracked-author window:** {start_date} to {end_date}",
        f"**Topic-discovery window:** {topic_start_date} to {end_date}",
        "",
        (
            "**New qualifying papers:** "
            f"{run_summary.get('qualifying', len(papers))}"
        ),
        (
            "**Topic-only qualifying papers:** "
            f"{run_summary.get('topic_only_qualifying', 0)}"
        ),
        (
            (
                "**Researcher candidates:** "
                f"{researcher_summary.get('candidates', 0)} evaluated; "
                f"{researcher_summary.get('recommended', 0)} RECOMMENDED, "
                f"{researcher_summary.get('watch', 0)} WATCH, "
                f"{researcher_summary.get('outside_network', 0)} outside "
                "the seed network"
            )
            if researcher_summary
            else "**Researcher radar:** not run or no candidates produced"
        ),
        (
            "**OpenAlex requests:** "
            f"{run_summary.get('openalex', {}).get('requests', 0)}"
        ),
        "",
        (
            "> Generated deterministically from OpenAlex metadata. "
            "Scores and rankings are discovery/prioritisation signals, "
            "not scientific quality, impact, or peer evaluation. "
            "Citation counts are shown only as context."
        ),
        "",
    ]

    openalex = run_summary.get("openalex") or {}
    if openalex.get("cost_usd") is not None:
        lines.extend(
            [
                (
                    "**OpenAlex reported cost:** "
                    f"${openalex['cost_usd']:.6f}"
                ),
                "",
            ]
        )

    if warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")

    if not papers:
        lines.extend(
            [
                "No new papers met the current relevance criteria.",
                "",
            ]
        )
    else:
        shown: set[str] = set()

        top = papers[:10]
        if top:
            lines.extend(["## Most Relevant New Papers", ""])
            render_papers(lines, top, abstract_length, False)
            shown.update(paper["key"] for paper in top)

        tracked = [
            paper
            for paper in papers
            if paper["analysis"]["tracked_authors"]
            and paper["key"] not in shown
        ][:10]
        if tracked:
            lines.extend(
                ["## Papers from Tracked Researchers", ""]
            )
            render_papers(
                lines,
                tracked,
                abstract_length,
                False,
                start_number=len(top) + 1,
            )
            shown.update(paper["key"] for paper in tracked)

        if researcher_section:
            lines.extend([researcher_section.rstrip(), ""])

        remaining = [
            paper for paper in papers if paper["key"] not in shown
        ]
        if remaining:
            lines.extend(["## Other Qualifying Papers", ""])
            grouped: dict[str, list[dict[str, Any]]] = {
                "Highly relevant": [],
                "Relevant": [],
                "Possibly relevant": [],
            }
            for paper in remaining:
                grouped[relevance_label(paper["analysis"]["score"])].append(
                    paper
                )

            number = len(shown) + 1
            for category in (
                "Highly relevant",
                "Relevant",
                "Possibly relevant",
            ):
                if not grouped[category]:
                    continue
                lines.extend([f"### {category}", ""])
                number = render_papers(
                    lines,
                    grouped[category],
                    abstract_length,
                    False,
                    start_number=number,
                )

    if run_summary.get("qualifying", len(papers)) > len(papers):
        lines.extend(
            [
                (
                    "The Markdown report is capped at "
                    f"{len(papers)} papers; "
                    f"{run_summary['qualifying']} qualifying papers are "
                    "preserved in `weekly_papers.json` and the CSV."
                ),
                "",
            ]
        )

    lines.extend(
        [
            "## Full Data",
            "",
            "- `weekly_papers.json` — authoritative structured paper records",
            "- `weekly_papers.csv` — tabular paper records",
            "- `researcher_radar.md` / `researcher_radar.json` — optional "
            "researcher radar from the same run",
            "- `researcher_history.json` — researcher score history",
            "",
            "## Warnings",
            "",
        ]
    )

    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None.")

    lines.append("")
    return "\n".join(lines)


def create_issue_report(
    config: dict[str, Any],
    papers: list[dict[str, Any]],
    candidate_authors: list[dict[str, Any]],
    run_summary: dict[str, Any],
    warnings: list[str],
    researcher_section: str | None = None,
    researcher_summary: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Create a concise Issue report and return the included papers."""
    reporting = config.get("reporting") or {}
    issue_limit = int(
        reporting.get("issue_max_chars", DEFAULT_ISSUE_LIMIT)
    )
    issue_limit = min(issue_limit, ISSUE_BODY_LIMIT)

    top_limit = int(reporting.get("issue_top_papers", 8))
    tracked_limit = int(reporting.get("issue_tracked_papers", 5))
    author_limit = int(reporting.get("issue_candidate_authors", 5))

    top = papers[: max(0, top_limit)]
    shown = {paper["key"] for paper in top}
    tracked = [
        paper
        for paper in papers
        if paper["analysis"]["tracked_authors"]
        and paper["key"] not in shown
    ][: max(0, tracked_limit)]
    authors = (
        []
        if researcher_section
        else candidate_authors[: max(0, author_limit)]
    )

    trim_notice = ""
    abstract_length = 0

    def render() -> str:
        lines = [
            "# Weekly Research Intelligence",
            "",
            (
                "**New qualifying papers:** "
                f"{run_summary.get('qualifying', len(papers))}"
            ),
            (
                "**Topic-only qualifying papers:** "
                f"{run_summary.get('topic_only_qualifying', 0)}"
            ),
            (
                (
                    "**Researcher candidates:** "
                    f"{researcher_summary.get('candidates', 0)} evaluated; "
                    f"{researcher_summary.get('recommended', 0)} "
                    "RECOMMENDED, "
                    f"{researcher_summary.get('watch', 0)} WATCH, "
                    f"{researcher_summary.get('outside_network', 0)} outside "
                    "the seed network"
                )
                if researcher_summary
                else (
                    "**Preliminary topic-discovered authors:** "
                    f"{len(candidate_authors)}"
                )
            ),
            (
                "**OpenAlex requests:** "
                f"{run_summary.get('openalex', {}).get('requests', 0)}"
            ),
            "",
            (
                "> Discovery prioritisation only, not a judgment of "
                "scientific quality. Full data is in "
                "`weekly_papers.json` and the workflow artifact."
            ),
            "",
        ]

        if top:
            lines.extend(["## Most Relevant New Papers", ""])
            render_papers(
                lines,
                top,
                abstract_length,
                True,
            )

        if tracked:
            lines.extend(
                ["## Papers from Tracked Researchers", ""]
            )
            render_papers(
                lines,
                tracked,
                abstract_length,
                True,
                start_number=len(top) + 1,
            )

        if researcher_section:
            lines.extend([researcher_section.rstrip(), ""])

        if warnings:
            lines.extend(["## Warnings", ""])
            lines.extend(f"- {warning}" for warning in warnings)
            lines.append("")

        lines.extend(
            [
                "## Full Data",
                "",
                "- `weekly_papers.json` / `weekly_papers.csv` — complete "
                "qualifying-paper records and evidence",
                "- `researcher_radar.md` / `researcher_radar.json` — "
                "researcher radar",
                "- `run_manifest.json` — run completeness, route status, "
                "request counts, and state update",
                "- Workflow artifact — full Markdown reports",
                "",
            ]
        )

        if trim_notice:
            lines.extend([trim_notice, ""])

        return "\n".join(lines)

    text = render()

    while len(text) > issue_limit and (authors or tracked or top):
        if authors:
            authors = authors[:-1]
        elif tracked:
            tracked = tracked[:-1]
        else:
            top = top[:-1]
        trim_notice = (
            "_Some lower-priority entries were omitted from this Issue "
            "to keep it concise. See `weekly_papers.json` for the "
            "complete data._"
        )
        text = render()

    if len(text) > issue_limit:
        notice = (
            "\n\n_Issue text was shortened; see `weekly_papers.json` "
            "for the complete data._\n"
        )
        text = text[: max(0, issue_limit - len(notice))] + notice

    selected = top + tracked
    return text, selected


def paper_to_json(paper: dict[str, Any]) -> dict[str, Any]:
    """Convert an internal paper record to the authoritative JSON form."""
    work = paper["work"]
    analysis = paper["analysis"]
    abstract = analysis["abstract"]
    if len(abstract) > 2000:
        abstract = abstract[:2000].rstrip() + " ..."

    return {
        "id": str(work.get("id") or paper["key"]),
        "openalex_id": short_id(work.get("id")),
        "doi": work.get("doi"),
        "title": work.get("title"),
        "publication_date": work.get("publication_date"),
        "publication_year": work.get("publication_year"),
        "type": work.get("type"),
        "source": get_primary_source(work),
        "url": get_work_url(work),
        "score": analysis["score"],
        "score_components": analysis["components"],
        "matched_high": analysis["matched_high"],
        "matched_medium": analysis["matched_medium"],
        "matched_exclusion": analysis["matched_exclusion"],
        "matched_queries": paper["matched_queries"],
        "routes": sorted(paper["routes"]),
        "tracked_authors": analysis["tracked_authors"],
        "authors": get_author_records(work),
        "topics": get_topic_records(work),
        "keywords": get_keyword_records(work),
        "abstract": abstract,
        "cited_by_count": work.get("cited_by_count"),
        "evidence": analysis["evidence"],
        "reported_weekly": bool(paper["reported_weekly"]),
        "reported_issue": bool(paper["reported_issue"]),
        "alternate_ids": sorted(paper["work_ids"]),
    }


def write_csv(papers: list[dict[str, Any]], path: Path) -> None:
    """Write the complete qualifying-paper table."""
    fields = [
        "id",
        "title",
        "publication_date",
        "type",
        "source",
        "doi",
        "url",
        "score",
        "matched_high",
        "matched_medium",
        "matched_queries",
        "routes",
        "tracked_authors",
        "reported_weekly",
        "reported_issue",
    ]

    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=fields,
        lineterminator="\n",
    )
    writer.writeheader()

    for paper in papers:
        data = paper_to_json(paper)
        writer.writerow(
            {
                "id": data["id"],
                "title": data["title"],
                "publication_date": data["publication_date"],
                "type": data["type"],
                "source": data["source"],
                "doi": data["doi"],
                "url": data["url"],
                "score": data["score"],
                "matched_high": "; ".join(data["matched_high"]),
                "matched_medium": "; ".join(data["matched_medium"]),
                "matched_queries": "; ".join(data["matched_queries"]),
                "routes": "; ".join(data["routes"]),
                "tracked_authors": "; ".join(data["tracked_authors"]),
                "reported_weekly": str(data["reported_weekly"]).lower(),
                "reported_issue": str(data["reported_issue"]).lower(),
            }
        )

    atomic_write_text(path, buffer.getvalue())


def get_session_stats(session: requests.Session) -> dict[str, Any]:
    """Return a JSON-safe copy of request/cost statistics."""
    stats = getattr(session, "stats", None)
    if not isinstance(stats, dict):
        stats = {}

    by_route = stats.get("by_route") or {}
    failed_by_route = stats.get("failed_by_route") or {}
    requests_count = int(stats.get("requests", 0) or 0)
    cost_available = bool(stats.get("cost_available", False))
    credits_available = bool(stats.get("credits_available", False))

    cost = None
    if cost_available and requests_count:
        cost = round(float(stats.get("cost_usd", 0.0) or 0.0), 6)

    credits = None
    if credits_available:
        credits = int(stats.get("credits", 0) or 0)

    return {
        "requests": requests_count,
        "by_route": {
            str(key): int(value)
            for key, value in dict(by_route).items()
        },
        "failed": int(stats.get("failed", 0) or 0),
        "failed_by_route": {
            str(key): int(value)
            for key, value in dict(failed_by_route).items()
        },
        "retries": int(stats.get("retries", 0) or 0),
        "cost_usd": cost,
        "credits": credits,
        "remaining_usd": stats.get("remaining_usd"),
        "api_key_present": bool(
            os.getenv("OPENALEX_API_KEY", "").strip()
        ),
    }


def require_number(
    value: Any,
    name: str,
    minimum: float | None = None,
) -> float:
    """Validate a numeric config value."""
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be numeric.") from error

    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")

    return number


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate Phase 1 settings and return normalised values."""
    brief = config.get("brief") or {}
    if not isinstance(brief, dict):
        raise ValueError("brief must be a mapping.")

    lookback_days = int(
        require_number(brief.get("lookback_days", 10), "brief.lookback_days", 1)
    )
    minimum_score = require_number(
        brief.get("minimum_score", 4),
        "brief.minimum_score",
    )
    maximum_papers = int(
        require_number(
            brief.get("maximum_papers", 15),
            "brief.maximum_papers",
            1,
        )
    )

    discovery = config.get("paper_discovery") or {}
    if not isinstance(discovery, dict):
        raise ValueError("paper_discovery must be a mapping.")

    enabled = bool(discovery.get("enabled", False))
    raw_queries = discovery.get("queries") or []
    if not isinstance(raw_queries, list):
        raise ValueError("paper_discovery.queries must be a list.")

    queries: list[str] = []
    for query in raw_queries:
        if not isinstance(query, str):
            raise ValueError(
                "paper_discovery.queries entries must be strings."
            )
        cleaned = sanitise_query(query)
        if cleaned:
            queries.append(cleaned)

    if enabled and not queries:
        raise ValueError(
            "paper_discovery.enabled is true but queries is empty."
        )

    topic_lookback = int(
        require_number(
            discovery.get("lookback_days", 45),
            "paper_discovery.lookback_days",
            1,
        )
    )
    results_per_query = int(
        require_number(
            discovery.get("results_per_query", 100),
            "paper_discovery.results_per_query",
            1,
        )
    )
    if results_per_query > 100:
        raise ValueError(
            "paper_discovery.results_per_query must be 100 or fewer."
        )

    raw_types = discovery.get("include_types")
    if raw_types is None:
        include_types = ["article", "preprint", "review"]
    elif isinstance(raw_types, list):
        if any(not isinstance(item, str) for item in raw_types):
            raise ValueError(
                "paper_discovery.include_types entries must be strings."
            )
        include_types = [
            item.strip() for item in raw_types if item.strip()
        ]
    else:
        raise ValueError(
            "paper_discovery.include_types must be a list."
        )

    query_bonus = require_number(
        discovery.get("query_bonus", 1),
        "paper_discovery.query_bonus",
        0,
    )
    max_query_bonus = int(
        require_number(
            discovery.get("max_query_bonus", 2),
            "paper_discovery.max_query_bonus",
            0,
        )
    )
    recent_days = int(
        require_number(
            discovery.get("recent_days", 30),
            "paper_discovery.recent_days",
            0,
        )
    )
    recent_bonus = require_number(
        discovery.get("recent_bonus", 2),
        "paper_discovery.recent_bonus",
        0,
    )
    candidate_limit = int(
        require_number(
            discovery.get("candidate_authors_limit", 50),
            "paper_discovery.candidate_authors_limit",
            0,
        )
    )

    reporting = config.get("reporting") or {}
    if not isinstance(reporting, dict):
        raise ValueError("reporting must be a mapping.")

    issue_top = int(
        require_number(
            reporting.get("issue_top_papers", 8),
            "reporting.issue_top_papers",
            0,
        )
    )
    issue_tracked = int(
        require_number(
            reporting.get("issue_tracked_papers", 5),
            "reporting.issue_tracked_papers",
            0,
        )
    )
    issue_authors = int(
        require_number(
            reporting.get("issue_candidate_authors", 5),
            "reporting.issue_candidate_authors",
            0,
        )
    )
    issue_max = int(
        require_number(
            reporting.get("issue_max_chars", DEFAULT_ISSUE_LIMIT),
            "reporting.issue_max_chars",
            1000,
        )
    )
    if issue_max > ISSUE_BODY_LIMIT:
        raise ValueError(
            f"reporting.issue_max_chars must be {ISSUE_BODY_LIMIT} or fewer."
        )

    return {
        "brief": {
            "lookback_days": lookback_days,
            "minimum_score": minimum_score,
            "maximum_papers": maximum_papers,
        },
        "paper_discovery": {
            "enabled": enabled,
            "lookback_days": topic_lookback,
            "queries": queries,
            "results_per_query": results_per_query,
            "query_bonus": query_bonus,
            "max_query_bonus": max_query_bonus,
            "recent_days": recent_days,
            "recent_bonus": recent_bonus,
            "include_types": include_types,
            "exclude_retracted": bool(
                discovery.get("exclude_retracted", True)
            ),
            "require_keyword_match": bool(
                discovery.get("require_keyword_match", True)
            ),
            "candidate_authors_limit": candidate_limit,
        },
        "reporting": {
            "issue_top_papers": issue_top,
            "issue_tracked_papers": issue_tracked,
            "issue_candidate_authors": issue_authors,
            "issue_max_chars": issue_max,
        },
    }


def config_fingerprint(
    config: dict[str, Any],
    settings: dict[str, Any],
) -> str:
    """Fingerprint the scientific filters, not secrets."""
    payload = {
        "keywords": config.get("keywords") or {},
        "queries": settings["paper_discovery"]["queries"],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for production, testing, and dry runs."""
    parser = argparse.ArgumentParser(
        description="Generate the weekly research intelligence brief."
    )
    parser.add_argument(
        "--config",
        default="config.yml",
        help="YAML config path (default: config.yml).",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Directory for generated reports/data. Defaults to the "
            "current directory, or .dryrun with --dry-run."
        ),
    )
    parser.add_argument(
        "--seen-file",
        default=str(SEEN_FILE),
        help="Processed Works state path (default: seen_works.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without changing persistent state; defaults to .dryrun output.",
    )
    parser.add_argument(
        "--skip-radar",
        action="store_true",
        help="Skip researcher radar for this run.",
    )
    parser.add_argument(
        "--with-radar",
        action="store_true",
        help="Run researcher radar during a dry run (no history writes).",
    )
    parser.add_argument(
        "--ignore-seen",
        action="store_true",
        help="Re-evaluate works already present in seen_works.json.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the complete research intelligence workflow."""
    global OUTPUT_DIR

    args = parse_args(argv)

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: {config_path} was not found.", file=sys.stderr)
        return 1

    try:
        config = load_yaml(config_path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"Error loading {config_path}: {error}", file=sys.stderr)
        return 1

    try:
        settings = validate_config(config)
    except (TypeError, ValueError) as error:
        print(f"Error in config: {error}", file=sys.stderr)
        return 1

    brief = settings["brief"]
    discovery = settings["paper_discovery"]
    reporting = settings["reporting"]

    authors = config.get("authors") or []
    if not isinstance(authors, list):
        print("Error: authors must be a list.", file=sys.stderr)
        return 1

    if not authors and not discovery["enabled"]:
        print(
            "Error: config.yml contains no authors and topic discovery "
            "is disabled.",
            file=sys.stderr,
        )
        return 1

    if args.output_dir:
        output_dir = Path(args.output_dir)
    elif args.dry_run:
        output_dir = Path(".dryrun")
    else:
        output_dir = Path(".")

    output_dir.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR = output_dir

    seen_path = Path(args.seen_file)
    try:
        seen_before = load_seen_works(seen_path)
    except StateError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    seen = (
        set()
        if (args.dry_run or args.ignore_seen)
        else set(seen_before)
    )

    today = datetime.now(timezone.utc).date()
    brief_start = today - timedelta(
        days=brief["lookback_days"]
    )
    topic_start = today - timedelta(
        days=discovery["lookback_days"]
    )
    brief_start_text = brief_start.isoformat()
    topic_start_text = topic_start.isoformat()
    end_date_text = today.isoformat()

    contact_email = os.getenv("OPENALEX_EMAIL", "").strip()
    seeds, seed_by_id = build_seed_map(config)

    collector = WorkCollector(
        config=config,
        settings={
            "minimum_score": brief["minimum_score"],
            "query_bonus": discovery["query_bonus"],
            "max_query_bonus": discovery["max_query_bonus"],
            "recent_days": discovery["recent_days"],
            "recent_bonus": discovery["recent_bonus"],
            "include_types": discovery["include_types"],
            "require_keyword_match": discovery["require_keyword_match"],
        },
        seed_by_id=seed_by_id,
        seen=seen,
        today=today,
    )

    session = build_http_session()
    warnings: list[str] = []
    seed_success = 0
    topic_success = 0

    for tracked_author in authors:
        if not isinstance(tracked_author, dict):
            warnings.append(f"Skipped invalid author entry: {tracked_author!r}")
            continue

        name = str(tracked_author.get("name") or "").strip()
        author_id = str(
            tracked_author.get("openalex_id") or ""
        ).strip()

        if not name or not author_id:
            warnings.append(
                f"Skipped incomplete author configuration: {tracked_author}"
            )
            continue

        print(f"Querying tracked author {name} ({author_id})")

        try:
            works = fetch_recent_works(
                session=session,
                author_id=author_id,
                start_date=brief_start_text,
                end_date=end_date_text,
                contact_email=contact_email,
            )
            seed_success += 1
        except (
            ValueError,
            requests.RequestException,
            json.JSONDecodeError,
        ) as error:
            warnings.append(
                f"Tracked-author query failed for {name}: {short_error(error)}"
            )
            print(
                f"Failed to query {name}: {error}",
                file=sys.stderr,
            )
            continue

        for work in works:
            collector.add(work, "seed")

        time.sleep(0.25)

    if discovery["enabled"]:
        for query in discovery["queries"]:
            print(f"Querying topic: {query}")
            try:
                works = fetch_topic_works(
                    session=session,
                    query=query,
                    start_date=topic_start_text,
                    end_date=end_date_text,
                    contact_email=contact_email,
                    results_per_query=discovery["results_per_query"],
                    include_types=discovery["include_types"],
                    exclude_retracted=discovery["exclude_retracted"],
                )
                topic_success += 1
            except (
                ValueError,
                requests.RequestException,
                json.JSONDecodeError,
            ) as error:
                warnings.append(
                    f"Topic query failed for {query!r}: {short_error(error)}"
                )
                print(
                    f"Topic query failed for {query!r}: {short_error(error)}",
                    file=sys.stderr,
                )
                continue

            for work in works:
                collector.add(work, "topic", query)

            time.sleep(0.25)
    else:
        print("Topic discovery is disabled; running seed-only.")

    if seed_success + topic_success == 0:
        failure_manifest = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "complete": False,
            "dry_run": bool(args.dry_run),
            "search_routes": {
                "seed": {
                    "attempted": len(authors),
                    "succeeded": 0,
                    "failed": len(authors),
                },
                "topic": {
                    "attempted": (
                        len(discovery["queries"])
                        if discovery["enabled"]
                        else 0
                    ),
                    "succeeded": 0,
                    "failed": (
                        len(discovery["queries"])
                        if discovery["enabled"]
                        else 0
                    ),
                },
                "radar": {"attempted": 0, "succeeded": 0, "failed": 0},
            },
            "successful_search_routes": [],
            "failed_search_routes": ["seed", "topic"],
            "openalex": get_session_stats(session),
            "llm_calls": 0,
            "paid_api_calls": 0,
            "state": {
                "dry_run": bool(args.dry_run),
                "updated": False,
                "planned": False,
                "path": str(seen_path),
                "qualifying_ids": 0,
                "error": "all seed and topic OpenAlex routes failed",
            },
            "warnings": warnings,
        }
        try:
            atomic_write_text(
                out_path(RUN_MANIFEST_FILE.name),
                json.dumps(failure_manifest, ensure_ascii=False, indent=2)
                + "\n",
            )
        except OSError:
            pass
        print(
            "Error: all OpenAlex discovery routes failed. "
            "State was not changed.",
            file=sys.stderr,
        )
        return 1

    papers: list[dict[str, Any]] = []
    qualification_counts: Counter[str] = Counter()

    for record in collector.records.values():
        qualified, reason = collector.qualify(record)
        if qualified:
            papers.append(record)
        else:
            qualification_counts[reason] += 1

    papers.sort(
        key=lambda item: (
            float(item["analysis"]["score"]),
            str(item["work"].get("publication_date") or ""),
            str(item["work"].get("title") or ""),
        ),
        reverse=True,
    )

    candidate_authors: list[dict[str, Any]] = []

    discovery_config = config.get("researcher_discovery") or {}
    run_radar = bool(discovery_config.get("enabled", False))
    if args.skip_radar:
        run_radar = False
    if args.dry_run and not args.with_radar:
        run_radar = False

    radar_result: dict[str, Any] | None = None
    if run_radar:
        try:
            from researcher_discovery import run_researcher_discovery

            if hasattr(session, "current_route"):
                session.current_route = "radar"  # type: ignore[attr-defined]

            radar_result = run_researcher_discovery(
                config,
                session,
                output_dir=output_dir,
                dry_run=args.dry_run,
                papers=papers,
            )
        except Exception as error:  # noqa: BLE001 - radar must not block papers
            warnings.append(
                f"Researcher discovery failed: {short_error(error, 500)}"
            )
            print(
                f"Researcher discovery failed: {short_error(error, 500)}",
                file=sys.stderr,
            )

    researcher_rows = list((radar_result or {}).get("rows") or [])
    researcher_summary = dict((radar_result or {}).get("summary") or {})
    issue_researcher_section = (radar_result or {}).get("issue_section")
    report_researcher_section = (radar_result or {}).get("report_section")
    for radar_warning in (radar_result or {}).get("warnings") or []:
        if radar_warning:
            warnings.append(str(radar_warning))

    weekly_selected = papers[: brief["maximum_papers"]]

    base_summary = {
        "qualifying": len(papers),
        "topic_only_qualifying": sum(
            1
            for paper in papers
            if "topic" in paper["routes"]
            and not paper["analysis"]["tracked_authors"]
        ),
        "researcher_candidates": int(
            researcher_summary.get("candidates", len(researcher_rows))
        ),
        "researcher_recommended": int(
            researcher_summary.get("recommended", 0)
        ),
        "researcher_watch": int(researcher_summary.get("watch", 0)),
        "researcher_outside_network": int(
            researcher_summary.get("outside_network", 0)
        ),
        "researcher_reappearing": int(
            researcher_summary.get("reappearing", 0)
        ),
        "researcher_emerging_activity": int(
            researcher_summary.get("emerging_activity", 0)
        ),
        "openalex": get_session_stats(session),
    }

    if base_summary["openalex"]["requests"] > 100:
        warnings.append(
            "OpenAlex requests exceeded the soft budget of 100 "
            f"({base_summary['openalex']['requests']} requests)."
        )

    issue_report, issue_selected = create_issue_report(
        config=config,
        papers=papers,
        candidate_authors=candidate_authors,
        run_summary=base_summary,
        warnings=warnings,
        researcher_section=issue_researcher_section,
        researcher_summary=researcher_summary or None,
    )

    weekly_keys = {paper["key"] for paper in weekly_selected}
    issue_keys = {paper["key"] for paper in issue_selected}

    for paper in papers:
        paper["reported_weekly"] = paper["key"] in weekly_keys
        paper["reported_issue"] = paper["key"] in issue_keys

    run_summary = {
        "status": (
            "empty"
            if not papers
            else ("partial" if warnings else "ok")
        ),
        "retrieved": int(collector.stats.get("retrieved", 0)),
        "evaluated": int(collector.stats.get("evaluated", 0)),
        "qualifying": len(papers),
        "skipped_seen": int(collector.stats.get("skipped_seen", 0)),
        "duplicates": int(collector.stats.get("duplicates", 0)),
        "excluded_type": int(collector.stats.get("excluded_type", 0)),
        "below_threshold": int(
            qualification_counts.get("below_threshold", 0)
        ),
        "excluded_keyword": int(
            qualification_counts.get("excluded_keyword", 0)
        ),
        "topic_no_keyword": int(
            qualification_counts.get("topic_no_keyword", 0)
        ),
        "topic_only_qualifying": base_summary[
            "topic_only_qualifying"
        ],
        "reported_weekly": len(weekly_selected),
        "reported_issue": len(issue_selected),
        "researcher_candidates": base_summary["researcher_candidates"],
        "researcher_recommended": base_summary["researcher_recommended"],
        "researcher_watch": base_summary["researcher_watch"],
        "researcher_outside_network": base_summary[
            "researcher_outside_network"
        ],
        "researcher_reappearing": base_summary[
            "researcher_reappearing"
        ],
        "researcher_emerging_activity": base_summary[
            "researcher_emerging_activity"
        ],
        "seed_queries_succeeded": seed_success,
        "topic_queries_succeeded": topic_success,
        "openalex": get_session_stats(session),
        "llm_calls": 0,
        "paid_api_calls": 0,
        "dry_run": bool(args.dry_run),
        "state_update_planned": bool(papers),
    }

    weekly_report = create_report(
        config=config,
        papers=weekly_selected,
        start_date=brief_start_text,
        end_date=end_date_text,
        topic_start_date=topic_start_text,
        run_summary=run_summary,
        candidate_authors=candidate_authors,
        warnings=warnings,
        researcher_section=report_researcher_section,
        researcher_summary=researcher_summary or None,
    )

    payload = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "brief_start": brief_start_text,
            "brief_end": end_date_text,
            "topic_start": topic_start_text,
            "topic_end": end_date_text,
        },
        "config_fingerprint": config_fingerprint(config, settings),
        "run": run_summary,
        "papers": [paper_to_json(paper) for paper in papers],
        "researcher_summary": researcher_summary,
        "researcher_radar_file": "researcher_radar.json",
        "run_manifest_file": "run_manifest.json",
        "warnings": warnings,
    }

    try:
        atomic_write_text(
            out_path(PAPERS_JSON_FILE.name),
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        write_csv(papers, out_path(PAPERS_CSV_FILE.name))
        atomic_write_text(
            out_path(OUTPUT_FILE.name),
            weekly_report,
        )
        atomic_write_text(
            out_path(ISSUE_OUTPUT_FILE.name),
            issue_report,
        )
    except OSError as error:
        print(
            f"Error writing generated outputs: {error}. "
            "State was not changed.",
            file=sys.stderr,
        )
        return 1

    state_updated = False
    state_error: str | None = None
    new_ids: set[str] = set()
    if not args.dry_run:
        for paper in papers:
            new_ids.update(paper["work_ids"])

        seen_after = set(seen_before) | new_ids
        if seen_after != seen_before:
            try:
                save_seen_works(seen_after, seen_path)
                state_updated = True
            except OSError as error:
                state_error = str(error)
                warnings.append(
                    "Processed-paper state was not updated: "
                    f"{error}"
                )
                print(
                    f"Error saving state: {error}. "
                    "Reports were written but processed state was not updated.",
                    file=sys.stderr,
                )

    topic_attempted = (
        len(discovery["queries"]) if discovery["enabled"] else 0
    )
    radar_attempted = 1 if run_radar else 0
    radar_succeeded = 1 if run_radar and radar_result is not None else 0
    route_stats = {
        "seed": {
            "attempted": len(authors),
            "succeeded": seed_success,
            "failed": max(0, len(authors) - seed_success),
        },
        "topic": {
            "attempted": topic_attempted,
            "succeeded": topic_success,
            "failed": max(0, topic_attempted - topic_success),
        },
        "radar": {
            "attempted": radar_attempted,
            "succeeded": radar_succeeded,
            "failed": radar_attempted - radar_succeeded,
        },
    }
    successful_routes = [
        name for name, values in route_stats.items() if values["succeeded"]
    ]
    failed_routes = [
        name for name, values in route_stats.items() if values["failed"]
    ]
    manifest_status = (
        "partial" if state_error else str(run_summary["status"])
    )
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": manifest_status,
        "complete": manifest_status in {"ok", "empty"} and not state_error,
        "dry_run": bool(args.dry_run),
        "search_routes": route_stats,
        "successful_search_routes": successful_routes,
        "failed_search_routes": failed_routes,
        "openalex": run_summary["openalex"],
        "llm_calls": 0,
        "paid_api_calls": 0,
        "state": {
            "dry_run": bool(args.dry_run),
            "updated": state_updated,
            "planned": bool(run_summary["state_update_planned"]),
            "path": str(seen_path),
            "qualifying_ids": len(new_ids) if not args.dry_run else 0,
            "error": state_error,
        },
        "counts": {
            "papers": run_summary["qualifying"],
            "reported_weekly": run_summary["reported_weekly"],
            "reported_issue": run_summary["reported_issue"],
            "researcher_candidates": run_summary["researcher_candidates"],
            "researcher_recommended": run_summary[
                "researcher_recommended"
            ],
            "researcher_watch": run_summary["researcher_watch"],
            "researcher_outside_network": run_summary[
                "researcher_outside_network"
            ],
            "researcher_reappearing": run_summary[
                "researcher_reappearing"
            ],
            "researcher_emerging_activity": run_summary[
                "researcher_emerging_activity"
            ],
        },
        "outputs": {
            "weekly_papers_json": str(out_path(PAPERS_JSON_FILE.name)),
            "weekly_papers_csv": str(out_path(PAPERS_CSV_FILE.name)),
            "weekly_brief": str(out_path(OUTPUT_FILE.name)),
            "issue_brief": str(out_path(ISSUE_OUTPUT_FILE.name)),
            "researcher_radar_json": (
                str(out_path("researcher_radar.json"))
                if run_radar
                else None
            ),
            "researcher_radar_markdown": (
                str(out_path("researcher_radar.md"))
                if run_radar
                else None
            ),
        },
        "warnings": warnings,
    }
    try:
        atomic_write_text(
            out_path(RUN_MANIFEST_FILE.name),
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
    except OSError as error:
        print(
            f"Error writing run manifest: {error}",
            file=sys.stderr,
        )
        return 1

    print(f"Generated full report: {out_path(OUTPUT_FILE.name)}")
    print(
        "Generated issue report: "
        f"{out_path(ISSUE_OUTPUT_FILE.name)}"
    )
    print(f"Generated weekly data: {out_path(PAPERS_JSON_FILE.name)}")
    print(f"Generated run manifest: {out_path(RUN_MANIFEST_FILE.name)}")
    print(
        "Papers: retrieved="
        f"{run_summary['retrieved']}, evaluated="
        f"{run_summary['evaluated']}, qualifying="
        f"{run_summary['qualifying']}, topic-only="
        f"{run_summary['topic_only_qualifying']}, "
        f"reported_weekly={run_summary['reported_weekly']}, "
        f"reported_issue={run_summary['reported_issue']}"
    )
    print(
        "OpenAlex requests: "
        f"{run_summary['openalex']['requests']} "
        f"({run_summary['openalex']['by_route']})"
    )
    if run_summary["openalex"]["cost_usd"] is not None:
        print(
            "OpenAlex reported cost: "
            f"${run_summary['openalex']['cost_usd']:.6f}"
        )
    else:
        print("OpenAlex reported cost: not available")
    print("LLM calls: 0")
    print("Paid API calls: 0")
    print(
        "Seen state: "
        + (
            "updated"
            if state_updated
            else "unchanged"
        )
    )

    if args.dry_run:
        print("Dry run: persistent state was not changed.")

    print(
        "Run status: "
        f"{manifest_status}"
        + ("" if manifest["complete"] else " (incomplete)")
    )
    return 1 if state_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
