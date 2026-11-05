"""Multi-route researcher discovery for the research-intelligence brief.

Researcher evidence comes from three routes:

* seed/network: a relevant paper coauthored with a tracked researcher;
* topic-first: an author on a paper found by a configured topic query;
* literature recurrence: an author appearing across multiple relevant papers.

The module reuses the Phase 1 paper records wherever possible.  It only
enriches a bounded shortlist of candidates with author works, and it never
turns coauthorship alone into a recommendation.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import requests

OPENALEX_API = "https://api.openalex.org"
OUTPUT_FILE = Path("researcher_radar.md")
DATA_FILE = Path("researcher_radar.json")
HISTORY_FILE = Path("researcher_history.json")

RECOMMENDED = "RECOMMENDED"
WATCH = "WATCH"
OUTSIDE_NETWORK = "OUTSIDE_NETWORK"
REAPPEARING = "REAPPEARING"
EMERGING_ACTIVITY = "EMERGING_ACTIVITY"

# Work types that are not scientific papers and should never be evidence.
NON_PAPER_TYPES = {"dataset", "paratext"}

ROUTE_LABELS = {
    "seed": "seed-author",
    "topic": "topic-first",
    "literature": "recurring-literature",
}


def _id(value: Any) -> str:
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def _normalise(value: Any) -> str:
    text = html.unescape(str(value or "")).lower()
    return re.sub(r"\s+", " ", text).strip()


def _abstract(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    positions = []
    for word, indexes in index.items():
        if isinstance(indexes, list):
            positions.extend(
                (i, str(word)) for i in indexes if isinstance(i, int)
            )
    return " ".join(word for _, word in sorted(positions))


def _contains(keyword: str, text: str) -> bool:
    keyword = _normalise(keyword)
    if not keyword:
        return False
    if len(keyword) <= 4 and keyword.isalpha():
        return re.search(rf"\b{re.escape(keyword)}\b", text, re.I) is not None
    return keyword in text


def _person_key(value: Any) -> str:
    """Normalise a person name for duplicate-record exclusion.

    OpenAlex sometimes carries more than one author ID for the same person.
    Dropping one-letter initials makes ``Andre K. Geim`` and ``Andre Geim``
    comparable without merging distinct full names.
    """
    text = _normalise(value)
    tokens = [
        token
        for token in re.findall(r"[^\W\d_]+", text, re.UNICODE)
        if len(token) > 1
    ]
    return " ".join(tokens)


def work_relevance(
    work: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Score topical evidence without awarding points for author identity."""
    kcfg = config.get("keywords") or {}
    high = [str(x) for x in kcfg.get("high_priority") or []]
    medium = [str(x) for x in kcfg.get("medium_priority") or []]
    exclusion = [str(x) for x in kcfg.get("exclusion") or []]
    topic_names = [
        str(t.get("display_name") or "")
        for t in work.get("topics") or []
        if isinstance(t, dict)
    ]
    keyword_names = [
        str(k.get("display_name") or "")
        for k in work.get("keywords") or []
        if isinstance(k, dict)
    ]
    text = _normalise(
        " ".join(
            [
                str(work.get("title") or ""),
                _abstract(work.get("abstract_inverted_index")),
                " ".join(topic_names),
                " ".join(keyword_names),
            ]
        )
    )
    matched_high = [k for k in high if _contains(k, text)]
    matched_medium = [k for k in medium if _contains(k, text)]
    matched_exclusion = [k for k in exclusion if _contains(k, text)]
    score = (
        3 * len(matched_high)
        + len(matched_medium)
        - 4 * len(matched_exclusion)
    )
    topic_ids = {
        _id(t.get("id"))
        for t in work.get("topics") or []
        if isinstance(t, dict) and t.get("id")
    }
    return {
        "score": score,
        "high": matched_high,
        "medium": matched_medium,
        "exclusion": matched_exclusion,
        "topic_ids": topic_ids,
        "topic_names": topic_names,
    }


def _get(
    session: requests.Session,
    endpoint: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    email = os.getenv("OPENALEX_EMAIL", "").strip()
    api_key = os.getenv("OPENALEX_API_KEY", "").strip()
    if email:
        params["mailto"] = email
    if api_key:
        params["api_key"] = api_key
    response = session.get(
        f"{OPENALEX_API}/{endpoint}",
        params=params,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def fetch_author_works(
    session: requests.Session,
    author_id: str,
    from_date: str,
    per_page: int,
    pages: int = 1,
    include_types: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch an author's works, optionally restricted to publication types."""
    types = [str(value) for value in include_types or [] if str(value)]
    filters = [
        f"authorships.author.id:{_id(author_id)}",
        f"from_publication_date:{from_date}",
    ]
    if types:
        filters.append("type:" + "|".join(types))

    results: list[dict[str, Any]] = []
    cursor = "*"
    for _ in range(max(1, pages)):
        payload = _get(
            session,
            "works",
            {
                "filter": ",".join(filters),
                "per_page": min(200, max(1, per_page)),
                "cursor": cursor,
                "sort": "publication_date:desc",
                "select": (
                    "id,title,doi,publication_date,publication_year,"
                    "cited_by_count,authorships,abstract_inverted_index,"
                    "topics,keywords,type"
                ),
            },
        )
        batch = [
            item
            for item in payload.get("results") or []
            if isinstance(item, dict)
        ]
        results.extend(batch)
        cursor = str((payload.get("meta") or {}).get("next_cursor") or "")
        if not batch or not cursor:
            break
        # Keep a small pause for the public endpoint.
        import time

        time.sleep(0.1)
    return results


def _authorships(work: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        authorship
        for authorship in work.get("authorships") or []
        if isinstance(authorship, dict)
    ]


def _author_records(work: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for authorship in _authorships(work):
        author = authorship.get("author") or {}
        aid = _id(author.get("id"))
        name = str(author.get("display_name") or "").strip()
        if not aid or not name:
            continue
        institutions = []
        for institution in authorship.get("institutions") or []:
            if (
                isinstance(institution, dict)
                and institution.get("display_name")
            ):
                institutions.append(str(institution["display_name"]))
        records.append(
            {
                "id": aid,
                "name": name,
                "institutions": institutions,
            }
        )
    return records


def _work_key(work: dict[str, Any]) -> str:
    raw = str(work.get("id") or "").strip()
    if raw:
        return _id(raw) or raw
    doi = str(work.get("doi") or "").strip()
    if doi:
        return "doi:" + doi.lower()
    title = _normalise(work.get("title"))
    year = str(work.get("publication_year") or "")
    return f"title:{title}|{year}"


def _paper_key(paper: dict[str, Any]) -> str:
    work = paper.get("work") or {}
    if isinstance(work, dict) and work:
        return _work_key(work)
    return str(paper.get("key") or "")


def _paper_date(paper: dict[str, Any]) -> str:
    work = paper.get("work") or {}
    return str(work.get("publication_date") or "")[:10]


def _paper_year(paper: dict[str, Any]) -> int | None:
    work = paper.get("work") or {}
    value = work.get("publication_year")
    return value if isinstance(value, int) else None


def _write_atomic(path: Path, text: str) -> None:
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


def _load_history(path: Path = HISTORY_FILE) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Could not read {path}: {error}. History was not changed."
        ) from error
    if not isinstance(value, dict):
        raise ValueError(
            f"{path} must contain a JSON object of researcher histories."
        )
    return value


def _iso_week_start(value: str) -> date | None:
    """Return the Monday of an ISO date's week, or None for invalid input."""
    try:
        day = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    return day - timedelta(days=day.weekday())


def _save_history(
    rows: list[dict[str, Any]],
    run_date: str,
    path: Path = HISTORY_FILE,
) -> None:
    """Append one snapshot per researcher per run date, atomically."""
    history = _load_history(path)
    for row in rows:
        aid = str(row.get("openalex_id") or "").strip()
        if not aid:
            continue
        entries = history.setdefault(aid, [])
        if not isinstance(entries, list):
            raise ValueError(
                f"History entry for {aid} is not a list; history was not changed."
            )
        entries = [
            entry
            for entry in entries
            if not (
                isinstance(entry, dict)
                and str(entry.get("date") or "")[:10] == run_date
            )
        ]
        entries.append(
            {
                "date": run_date,
                "score": row.get(
                    "recommendation_score",
                    row.get("score", 0),
                ),
                "recent_relevant_works": row.get(
                    "recent_relevant_works",
                    row.get("recent_relevant_papers", 0),
                ),
                "recent_relevant_papers": row.get(
                    "recent_relevant_papers", 0
                ),
                "relevant_papers": row.get("relevant_papers", 0),
                "status": row.get("status"),
                "category": row.get("status"),
                "routes": list(row.get("routes") or []),
                "outside_network": bool(row.get("outside_network")),
                "reappearing": bool(row.get("reappearing")),
                "emerging_activity": bool(row.get("emerging_activity")),
                "appearances": int(row.get("appearances", 1) or 1),
                "weeks_seen": int(row.get("weeks_seen", 1) or 1),
                "consecutive_weeks": int(
                    row.get("consecutive_weeks", 1) or 1
                ),
                "first_seen": row.get("first_seen") or run_date,
                "last_seen": row.get("last_seen") or run_date,
            }
        )
        history[aid] = entries[-52:]
    _write_atomic(
        path,
        json.dumps(history, ensure_ascii=False, indent=2) + "\n",
    )


def _history_stats(
    history: dict[str, Any],
    author_id: str,
    run_date: str,
) -> dict[str, Any]:
    """Derive distinct-date and distinct-week recurrence statistics.

    Appearance counts use distinct run dates, so repeated same-date test runs
    never inflate them.  ``REAPPEARING`` requires at least two distinct ISO
    weeks, not merely two entries.
    """
    entries = history.get(author_id) or []
    if not isinstance(entries, list):
        entries = []
    valid = [
        entry for entry in entries if isinstance(entry, dict)
    ]
    dates = {
        value
        for value in (
            str(entry.get("date") or "")[:10] for entry in valid
        )
        if _iso_week_start(value) is not None
    }
    dates.add(run_date)
    prior_dates = dates - {run_date}

    weeks = {
        week
        for week in (_iso_week_start(value) for value in dates)
        if week is not None
    }
    consecutive = 0
    cursor = _iso_week_start(run_date)
    while cursor is not None and cursor in weeks:
        consecutive += 1
        cursor -= timedelta(days=7)

    ordered = sorted(valid, key=lambda entry: str(entry.get("date") or ""))
    recent_history = [
        {
            "date": str(entry.get("date") or "")[:10],
            "status": entry.get("category") or entry.get("status"),
            "relevant_papers": int(entry.get("relevant_papers", 0) or 0),
            "recent_relevant_papers": int(
                entry.get(
                    "recent_relevant_papers",
                    entry.get("recent_relevant_works", 0),
                )
                or 0
            ),
        }
        for entry in ordered[-8:]
        if str(entry.get("date") or "")[:10]
    ]
    category_history = [
        {
            "date": item["date"],
            "status": item["status"],
        }
        for item in recent_history
        if item["status"]
    ]

    return {
        "first_seen": min(dates),
        "last_seen": max(dates),
        "appearances": len(dates),
        "prior_appearances": len(prior_dates),
        "weeks_seen": len(weeks),
        "consecutive_weeks": consecutive,
        # Two distinct weekly runs are meaningful recurrence; repeated
        # same-date runs are not.
        "reappearing": len(weeks) >= 2,
        "recent_history": recent_history,
        "category_history": category_history,
    }


def _emerging_activity(
    relevant_papers: int,
    recent_relevant: int,
    config: dict[str, Any],
) -> tuple[bool, str]:
    """Return a deterministic recent-activity discovery signal.

    This compares the recent publication rate with the earlier rate.  It is
    not age, career stage, seniority, quality, or citation impact.
    """
    dcfg = config.get("researcher_discovery") or {}
    minimum_recent = int(dcfg.get("emerging_min_recent", 2))
    multiplier = float(dcfg.get("emerging_rate_multiplier", 1.5))
    analysis_years = max(1, int(dcfg.get("analysis_years", 5)))
    recent_years = max(1, int(dcfg.get("recent_years", 2)))
    prior_years = max(1, analysis_years - recent_years)
    prior_relevant = max(0, relevant_papers - recent_relevant)

    if recent_relevant < minimum_recent:
        return False, (
            f"{recent_relevant} recent paper(s), below the "
            f"{minimum_recent}-paper activity threshold"
        )
    if prior_relevant == 0:
        return True, (
            f"{recent_relevant} recent paper(s) with no earlier relevant "
            "papers in the analysis window"
        )

    recent_rate = recent_relevant / recent_years
    prior_rate = prior_relevant / prior_years
    if recent_rate >= multiplier * prior_rate:
        return True, (
            f"recent rate {recent_rate:.2f}/year versus earlier "
            f"{prior_rate:.2f}/year"
        )
    return False, (
        f"recent rate {recent_rate:.2f}/year is not above "
        f"{multiplier:.2f}x the earlier {prior_rate:.2f}/year"
    )


def _seed_config(
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    seeds: list[dict[str, Any]] = []
    seed_by_id: dict[str, str] = {}
    group_by_id: dict[str, str] = {}
    for author in config.get("authors") or []:
        if not isinstance(author, dict):
            continue
        name = str(author.get("name") or "").strip()
        aid = _id(author.get("openalex_id"))
        if not name or not aid:
            continue
        seeds.append(
            {
                "id": aid,
                "name": name,
                "group": str(author.get("group") or "Ungrouped"),
            }
        )
        seed_by_id[aid] = name
        group_by_id[aid] = str(author.get("group") or "Ungrouped")
    return seeds, seed_by_id, group_by_id


def _normalise_doi(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("https://doi.org/", "")
        .replace("http://doi.org/", "")
        .replace("doi:", "")
        .strip()
    )


def _work_identities(work: dict[str, Any]) -> tuple[list[str], str | None]:
    """Return strong evidence keys and an optional weak title key."""
    work_id = _id(work.get("id"))
    doi = _normalise_doi(work.get("doi"))
    strong: list[str] = []
    if work_id:
        strong.append(f"id:{work_id}")
    if doi:
        strong.append(f"doi:{doi}")
    title = _normalise(work.get("title"))
    year = work.get("publication_year")
    weak = f"title:{title}|{year}" if title else None
    return strong, weak


def _new_candidate(author_id: str, name: str) -> dict[str, Any]:
    return {
        "openalex_id": author_id,
        "name": name,
        "institutions": set(),
        "paper_keys": set(),
        "paper_aliases": set(),
        "paper_index": {},
        "papers": [],
        "seed_ids": set(),
        "seed_names": set(),
        "groups": set(),
        "keywords": Counter(),
        "queries": set(),
        "topic_papers": 0,
        "seed_papers": 0,
        "latest_publication_date": "",
        "first_publication_year": None,
        "enriched": False,
        "enrichment_failed": False,
        "enriched_relevant": 0,
    }


def _merge_evidence(
    existing: dict[str, Any],
    routes: set[str],
    matched_keywords: list[str],
    matched_queries: list[str],
    score: float,
    source: str,
) -> None:
    existing["routes"] = sorted(set(existing.get("routes") or []) | routes)
    existing["matched_keywords"] = matching_unique(
        list(existing.get("matched_keywords") or []) + matched_keywords
    )
    existing["matched_queries"] = matching_unique(
        list(existing.get("matched_queries") or []) + matched_queries
    )
    existing["score"] = max(float(existing.get("score") or 0), score)
    # Keep the first source label but route information is preserved above.
    if not existing.get("source"):
        existing["source"] = source


def _add_paper(
    row: dict[str, Any],
    paper: dict[str, Any],
    work: dict[str, Any],
    seed_ids_on_paper: set[str],
    seed_by_id: dict[str, str],
    group_by_id: dict[str, str],
    config: dict[str, Any],
    source: str,
) -> None:
    work_type = str(work.get("type") or "").strip().lower()
    if work_type in NON_PAPER_TYPES:
        return

    title = str(work.get("title") or "").strip()
    work_id = _id(work.get("id"))
    doi = _normalise_doi(work.get("doi"))
    strong, weak = _work_identities(work)
    if not (title or work_id or doi):
        return

    index = None
    for key in strong:
        index = row["paper_index"].get(key)
        if index is not None:
            break
    if index is None and not strong and weak:
        candidate_index = row["paper_index"].get(weak)
        if candidate_index is not None:
            existing = row["papers"][candidate_index]
            # Only merge title-only records when neither side has a strong ID.
            if not (
                _id(existing.get("openalex_id"))
                or _normalise_doi(existing.get("doi"))
            ):
                index = candidate_index

    routes = set(paper.get("routes") or [])
    if seed_ids_on_paper:
        routes.add("seed")
    analysis = paper.get("analysis") or {}
    matched_high = [
        str(value) for value in analysis.get("matched_high") or []
    ]
    matched_medium = [
        str(value) for value in analysis.get("matched_medium") or []
    ]
    matched_keywords = matched_high + matched_medium
    matched_queries = [
        str(value) for value in paper.get("matched_queries") or []
    ]
    score = float(analysis.get("score") or 0)

    if index is not None:
        existing = row["papers"][index]
        old_routes = set(existing.get("routes") or [])
        _merge_evidence(
            existing,
            routes,
            matched_keywords,
            matched_queries,
            score,
            source,
        )
        if "topic" in routes and "topic" not in old_routes:
            row["topic_papers"] += 1
        if seed_ids_on_paper and "seed" not in old_routes:
            row["seed_papers"] += 1
        row["keywords"].update(matched_keywords)
        row["queries"].update(matched_queries)
        row["seed_ids"].update(seed_ids_on_paper)
        for seed_id in seed_ids_on_paper:
            row["seed_names"].add(seed_by_id.get(seed_id, seed_id))
            row["groups"].add(group_by_id.get(seed_id, "Ungrouped"))
        return

    canonical = work_id or doi or weak or title
    if canonical in row["paper_keys"]:
        return
    row["paper_keys"].add(canonical)
    for key in strong:
        row["paper_aliases"].add(key)
        row["paper_index"][key] = len(row["papers"])
    if weak:
        row["paper_aliases"].add(weak)
        row["paper_index"][weak] = len(row["papers"])

    row["papers"].append(
        {
            "openalex_id": work_id,
            "title": title or "Untitled",
            "date": str(work.get("publication_date") or "")[:10],
            "doi": str(work.get("doi") or ""),
            "routes": sorted(routes),
            "score": score,
            "matched_keywords": matching_unique(matched_keywords),
            "matched_queries": matching_unique(matched_queries),
            "source": source,
        }
    )
    row["keywords"].update(matched_keywords)
    row["queries"].update(matched_queries)
    row["seed_ids"].update(seed_ids_on_paper)
    for seed_id in seed_ids_on_paper:
        row["seed_names"].add(seed_by_id.get(seed_id, seed_id))
        row["groups"].add(group_by_id.get(seed_id, "Ungrouped"))
    if "topic" in routes:
        row["topic_papers"] += 1
    if seed_ids_on_paper:
        row["seed_papers"] += 1

    publication_date = str(work.get("publication_date") or "")[:10]
    if publication_date > row["latest_publication_date"]:
        row["latest_publication_date"] = publication_date
    year = work.get("publication_year")
    if isinstance(year, int):
        if row["first_publication_year"] is None:
            row["first_publication_year"] = year
        else:
            row["first_publication_year"] = min(
                row["first_publication_year"],
                year,
            )


def matching_unique(values: Iterable[Any]) -> list[str]:
    """Return case-insensitively unique values, preserving first spelling."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _candidates_from_papers(
    papers: Iterable[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    seeds, seed_by_id, group_by_id = _seed_config(config)
    seed_ids = set(seed_by_id)
    dcfg = config.get("researcher_discovery") or {}
    exclude_seed_names = bool(dcfg.get("exclude_seed_names", True))
    seed_name_keys = {
        _person_key(name) for name in seed_by_id.values() if _person_key(name)
    }
    candidates: dict[str, dict[str, Any]] = {}

    for paper in papers:
        if not isinstance(paper, dict):
            continue
        work = paper.get("work") or {}
        if not isinstance(work, dict) or not work:
            continue
        work_type = str(work.get("type") or "").strip().lower()
        if work_type in NON_PAPER_TYPES:
            continue
        if not (
            str(work.get("title") or "").strip()
            or _id(work.get("id"))
            or _normalise_doi(work.get("doi"))
        ):
            continue
        paper_key = _work_key(work)
        seed_ids_on_paper = {
            _id((authorship.get("author") or {}).get("id"))
            for authorship in _authorships(work)
            if _id((authorship.get("author") or {}).get("id"))
            in seed_ids
        }

        for author in _author_records(work):
            aid = author["id"]
            if aid in seed_ids:
                continue
            if exclude_seed_names and _person_key(author["name"]) in seed_name_keys:
                continue
            row = candidates.setdefault(
                aid,
                _new_candidate(aid, author["name"]),
            )
            if author["institutions"]:
                row["institutions"].update(author["institutions"])
            _add_paper(
                row,
                paper,
                work,
                seed_ids_on_paper,
                seed_by_id,
                group_by_id,
                config,
                "phase1",
            )
        # Keep the key explicit; this also makes the dedupe intent clear.
        del paper_key

    return list(candidates.values())


def _recent_from(config: dict[str, Any]) -> str:
    dcfg = config.get("researcher_discovery") or {}
    recent_years = int(dcfg.get("recent_years", 2))
    return (date.today() - timedelta(days=365 * recent_years)).isoformat()


def _preliminary_score(row: dict[str, Any]) -> float:
    return (
        2.0 * min(len(row["paper_keys"]), 5)
        + 2.0 * min(row["topic_papers"], 3)
        + 1.5 * min(len(row["keywords"]), 4)
        + 1.0 * min(len(row["queries"]), 3)
        + 1.0 * (1 if row["seed_papers"] == 0 else 0)
        + 0.5 * min(len(row["seed_ids"]), 3)
        + 0.5 * (2 if len(row["seed_ids"]) >= 2 else 0)
    )


def _enrich_candidate(
    row: dict[str, Any],
    config: dict[str, Any],
    session: requests.Session,
    from_date: str,
    recent_from: str,
    include_types: Iterable[str] | None,
    seed_by_id: dict[str, str],
    group_by_id: dict[str, str],
) -> None:
    """Add bounded publication-history evidence for one candidate."""
    dcfg = config.get("researcher_discovery") or {}
    minimum_score = int(dcfg.get("minimum_work_relevance", 2))
    try:
        works = fetch_author_works(
            session,
            row["openalex_id"],
            from_date,
            200,
            1,
            include_types=include_types,
        )
    except requests.RequestException as error:
        row["enrichment_failed"] = True
        print(
            "Researcher discovery: failed enrichment for "
            f"{row['name']}: {error}"
        )
        return

    for work in works:
        if not isinstance(work, dict):
            continue
        paper_key = _work_key(work)
        if paper_key in row["paper_keys"]:
            continue
        work_type = str(work.get("type") or "")
        if include_types and work_type and work_type not in include_types:
            continue
        relevance = work_relevance(work, config)
        if (
            relevance["score"] < minimum_score
            or relevance["exclusion"]
        ):
            continue

        seed_ids_on_paper = {
            _id((authorship.get("author") or {}).get("id"))
            for authorship in _authorships(work)
            if _id((authorship.get("author") or {}).get("id"))
            in seed_by_id
        }
        analysis = {
            "score": relevance["score"],
            "matched_high": relevance["high"],
            "matched_medium": relevance["medium"],
        }
        _add_paper(
            row,
            {"routes": {"literature"}, "analysis": analysis},
            work,
            seed_ids_on_paper,
            seed_by_id,
            group_by_id,
            config,
            "author_history",
        )
        # _add_paper only counts a topic paper when the paper came from a
        # topic query.  Author-history works are literature-route evidence.
        row["enriched_relevant"] += 1

    row["enriched"] = True


def _finalize_row(
    row: dict[str, Any],
    history: dict[str, Any],
    run_date: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    dcfg = config.get("researcher_discovery") or {}
    minimum_papers = int(dcfg.get("recommendation_min_papers", 2))
    recent_from = _recent_from(config)
    recent_relevant = sum(
        1
        for paper in row["papers"]
        if str(paper.get("date") or "") >= recent_from
    )
    relevant_papers = len(row["paper_keys"])
    routes = set()
    if row["topic_papers"]:
        routes.add("topic")
    if row["seed_papers"]:
        routes.add("seed")
    if relevant_papers >= 2:
        routes.add("literature")

    has_topic_evidence = bool(
        row["topic_papers"] or row["keywords"] or row["queries"]
    )
    status = (
        RECOMMENDED
        if relevant_papers >= minimum_papers and has_topic_evidence
        else WATCH
    )

    outside_network = row["seed_papers"] == 0
    history_info = _history_stats(
        history,
        row["openalex_id"],
        run_date,
    )
    emerging, emerging_reason = _emerging_activity(
        relevant_papers,
        recent_relevant,
        config,
    )
    category_history = list(history_info["category_history"])
    if not any(item["date"] == run_date for item in category_history):
        category_history.append({"date": run_date, "status": status})
    category_history = category_history[-8:]
    recent_history = list(history_info["recent_history"])
    if not any(item["date"] == run_date for item in recent_history):
        recent_history.append(
            {
                "date": run_date,
                "status": status,
                "relevant_papers": relevant_papers,
                "recent_relevant_papers": recent_relevant,
            }
        )
    recent_history = recent_history[-8:]

    signals: list[str] = []
    if outside_network:
        signals.append(OUTSIDE_NETWORK)
    if history_info["reappearing"]:
        signals.append(REAPPEARING)
    if emerging:
        signals.append(EMERGING_ACTIVITY)

    routes_sorted = sorted(routes)
    evidence_works = sorted(
        row["papers"],
        key=lambda paper: (
            str(paper.get("date") or ""),
            float(paper.get("score") or 0),
        ),
        reverse=True,
    )[:6]
    keywords = [
        value for value, _ in row["keywords"].most_common(8)
    ]
    queries = sorted(row["queries"])
    seed_names = sorted(row["seed_names"])
    groups = sorted(row["groups"])

    score = round(
        2.0 * min(relevant_papers, 5)
        + 1.0 * min(recent_relevant, 3)
        + 0.75 * min(len(keywords), 4)
        + 0.5 * min(len(queries), 3)
        + 0.5 * min(len(row["seed_ids"]), 3)
        + (1.5 if outside_network else 0.0)
        + 0.5 * min(history_info["prior_appearances"], 3)
        + 0.5 * max(0, len(routes_sorted) - 1)
        + (1.0 if emerging else 0.0),
        1,
    )

    reasons: list[str] = []
    if routes_sorted:
        reasons.append(
            "routes: "
            + " + ".join(ROUTE_LABELS.get(route, route) for route in routes_sorted)
        )
    reasons.append(
        f"{relevant_papers} relevant paper(s); "
        f"{recent_relevant} in the recent window"
    )
    if keywords:
        reasons.append("matched keywords: " + ", ".join(keywords[:6]))
    if queries:
        reasons.append("matched queries: " + ", ".join(queries[:4]))
    if seed_names:
        reasons.append(
            "seed coauthorship evidence: " + ", ".join(seed_names[:4])
        )
    else:
        reasons.append("no seed-author connection observed")
    if history_info["appearances"] > 1:
        reasons.append(
            f"appeared in {history_info['appearances']} weekly run(s), "
            f"{history_info['consecutive_weeks']} consecutive week(s)"
        )
    if emerging:
        reasons.append("emerging recent activity: " + emerging_reason)

    institution = (
        sorted(row["institutions"])[0]
        if row["institutions"]
        else "Unknown"
    )
    return {
        "openalex_id": row["openalex_id"],
        "name": row["name"],
        "institution": institution,
        "institutions": sorted(row["institutions"]),
        "status": status,
        "signals": signals,
        "categories": [status] + signals,
        "routes": routes_sorted,
        "recommendation_score": score,
        "relevant_papers": relevant_papers,
        "recent_relevant_papers": recent_relevant,
        "topic_papers": row["topic_papers"],
        "seed_coauthored_papers": row["seed_papers"],
        "seed_connections": len(row["seed_ids"]),
        "seed_names": seed_names,
        "groups": groups,
        "matched_keywords": keywords,
        "matched_queries": queries,
        "latest_publication_date": row["latest_publication_date"],
        "first_publication_year_signal": row["first_publication_year"],
        "outside_network": outside_network,
        "reappearing": history_info["reappearing"],
        "first_seen": history_info["first_seen"],
        "last_seen": history_info["last_seen"],
        "appearances": history_info["appearances"],
        "prior_appearances": history_info["prior_appearances"],
        "weeks_seen": history_info["weeks_seen"],
        "consecutive_weeks": history_info["consecutive_weeks"],
        "emerging_activity": emerging,
        "emerging_activity_reason": emerging_reason,
        "recent_history": recent_history,
        "category_history": category_history,
        "enriched": row["enriched"],
        "enrichment_failed": row["enrichment_failed"],
        "reasons": reasons,
        "why": "; ".join(reasons) + ".",
        "evidence_works": evidence_works,
    }


def _group_analysis(
    papers: Iterable[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    _, seed_by_id, group_by_id = _seed_config(config)
    seed_group_topics: dict[str, Counter[str]] = defaultdict(Counter)
    group_edges: Counter[tuple[str, str]] = Counter()

    for paper in papers:
        if not isinstance(paper, dict):
            continue
        work = paper.get("work") or {}
        if not isinstance(work, dict) or not work:
            continue
        present_groups = sorted(
            {
                group_by_id.get(aid, "Ungrouped")
                for authorship in _authorships(work)
                if (aid := _id((authorship.get("author") or {}).get("id")))
                in seed_by_id
            }
        )
        if not present_groups:
            continue
        topic_ids = {
            _id(topic.get("id"))
            for topic in work.get("topics") or []
            if isinstance(topic, dict) and topic.get("id")
        }
        for group in present_groups:
            for topic_id in topic_ids:
                seed_group_topics[group][topic_id] += 1
        for index, group_1 in enumerate(present_groups):
            for group_2 in present_groups[index + 1:]:
                group_edges[tuple(sorted((group_1, group_2)))] += 1

    dcfg = config.get("researcher_discovery") or {}
    threshold = float(dcfg.get("group_similarity_threshold", 0.15))
    result = {
        "close_collaboration": [],
        "similar_without_seed_collaboration": [],
    }
    groups = sorted(seed_group_topics)
    for index, group_1 in enumerate(groups):
        topics_1 = set(seed_group_topics[group_1])
        for group_2 in groups[index + 1:]:
            topics_2 = set(seed_group_topics[group_2])
            union = topics_1 | topics_2
            similarity = (
                len(topics_1 & topics_2) / len(union) if union else 0.0
            )
            pair = tuple(sorted((group_1, group_2)))
            edge_count = group_edges.get(pair, 0)
            item = {
                "group_1": group_1,
                "group_2": group_2,
                "topic_jaccard": round(similarity, 3),
                "relevant_seed_coauthored_works": edge_count,
            }
            if edge_count:
                result["close_collaboration"].append(item)
            elif similarity >= threshold:
                result["similar_without_seed_collaboration"].append(item)

    result["close_collaboration"].sort(
        key=lambda item: (
            item["relevant_seed_coauthored_works"],
            item["topic_jaccard"],
        ),
        reverse=True,
    )
    result["similar_without_seed_collaboration"].sort(
        key=lambda item: item["topic_jaccard"],
        reverse=True,
    )
    return result


def discover_researchers(
    config: dict[str, Any],
    session: requests.Session,
    papers: Iterable[dict[str, Any]] | None = None,
    history: dict[str, Any] | None = None,
    run_date: str | None = None,
    enrich: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return all finalised candidates and group analysis.

    Phase 1 paper records are the primary evidence source.  If no papers are
    supplied, discovery returns no candidates rather than silently falling
    back to a seed-only coauthor crawl.
    """
    dcfg = config.get("researcher_discovery") or {}
    if not dcfg.get("enabled", True):
        return [], {}
    if papers is None:
        print(
            "Researcher discovery: no Phase 1 paper records were supplied; "
            "no candidates were evaluated."
        )
        return [], {}
    papers = list(papers)
    run_date = run_date or date.today().isoformat()
    history = history or {}

    candidates = _candidates_from_papers(papers, config)
    if not candidates:
        return [], _group_analysis(papers, config)

    _, seed_by_id, group_by_id = _seed_config(config)
    candidate_pool_size = int(dcfg.get("candidate_pool_size", 25))
    from_date = (
        date.today() - timedelta(days=365 * int(dcfg.get("analysis_years", 5)))
    ).isoformat()
    recent_from = _recent_from(config)
    include_types = [
        str(value)
        for value in dcfg.get("enrich_include_types")
        or ["article", "preprint", "review"]
        if str(value)
    ]

    candidates.sort(key=_preliminary_score, reverse=True)
    shortlist = candidates[: max(0, candidate_pool_size)] if enrich else []
    for row in shortlist:
        _enrich_candidate(
            row,
            config,
            session,
            from_date,
            recent_from,
            include_types,
            seed_by_id,
            group_by_id,
        )

    rows = [
        _finalize_row(row, history, run_date, config)
        for row in candidates
    ]
    rows.sort(
        key=lambda item: (
            item["recommendation_score"],
            item["recent_relevant_papers"],
            item["relevant_papers"],
            item["name"].casefold(),
        ),
        reverse=True,
    )
    return rows, _group_analysis(papers, config)


def _route_text(row: dict[str, Any]) -> str:
    return " + ".join(
        ROUTE_LABELS.get(route, route) for route in row["routes"]
    )


def _compact_reason(row: dict[str, Any]) -> str:
    parts = [
        f"{row['relevant_papers']} relevant paper(s)",
        f"{row['recent_relevant_papers']} recent",
    ]
    if row["matched_keywords"]:
        parts.append(
            "matched " + ", ".join(row["matched_keywords"][:3])
        )
    if row["matched_queries"]:
        parts.append(
            "queries " + ", ".join(row["matched_queries"][:2])
        )
    if row["seed_names"]:
        parts.append("seed: " + ", ".join(row["seed_names"][:2]))
    else:
        parts.append("no seed-author connection")
    if row["appearances"] > 1:
        parts.append(
            f"{row['appearances']} weekly appearances "
            f"({row['consecutive_weeks']} consecutive)"
        )
    if row.get("emerging_activity"):
        parts.append("emerging recent activity")
    return "; ".join(parts)


def _format_issue_row(row: dict[str, Any]) -> list[str]:
    lines = [f"### {row['name']}", ""]
    lines.append(
        f"- **Status:** {row['status']}"
        + (
            " (" + ", ".join(row["signals"]) + ")"
            if row["signals"]
            else ""
        )
    )
    lines.append(f"- **Routes:** {_route_text(row) or 'topic evidence'}")
    lines.append(f"- **Evidence:** {_compact_reason(row)}")
    if row["evidence_works"]:
        paper = row["evidence_works"][0]
        lines.append(
            "- **Strongest paper:** "
            f"{paper.get('date') or 'Unknown date'} — "
            f"{paper.get('title') or 'Untitled'}"
        )
    if row["institution"] and row["institution"] != "Unknown":
        lines.append(f"- **Institution:** {row['institution']}")
    lines.append(
        f"- **OpenAlex:** https://openalex.org/{row['openalex_id']}"
    )
    lines.append("")
    return lines


def render_issue_researcher_section(
    rows: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> str:
    """Render the concise researcher section used in the GitHub Issue."""
    dcfg = (config or {}).get("researcher_discovery") or {}
    recommended_limit = int(dcfg.get("issue_recommended", 3))
    watch_limit = int(dcfg.get("issue_watch", 2))
    outside_limit = int(dcfg.get("issue_outside", 3))
    reappearing_limit = int(dcfg.get("issue_reappearing", 3))
    emerging_limit = int(dcfg.get("issue_emerging", 3))

    if not rows:
        return (
            "## Researcher Radar\n\n"
            "No researcher candidates met the current evidence requirements."
        )

    recommended = [
        row
        for row in rows
        if row["status"] == RECOMMENDED and not row["reappearing"]
    ][: max(0, recommended_limit)]
    new_watch = [
        row
        for row in rows
        if row["status"] == WATCH and row not in recommended
    ][: max(0, watch_limit)]
    new = recommended + new_watch
    outside = [
        row
        for row in rows
        if row["outside_network"] and row not in new
    ][: max(0, outside_limit)]
    reappearing = [
        row
        for row in rows
        if row["reappearing"]
        and row not in new
        and row not in outside
    ][: max(0, reappearing_limit)]
    emerging = [
        row
        for row in rows
        if row.get("emerging_activity")
        and row not in new
        and row not in outside
        and row not in reappearing
    ][: max(0, emerging_limit)]

    lines = ["## Researcher Radar", ""]
    lines.extend(
        [
            "> Discovery prioritisation only, not a judgment of researcher "
            "quality or career stage.",
            "",
        ]
    )

    if new:
        lines.extend(["### New Researchers Worth Watching", ""])
        for row in new:
            lines.extend(_format_issue_row(row))
    if outside:
        lines.extend(["### Outside Current Network", ""])
        for row in outside:
            lines.extend(_format_issue_row(row))
    if reappearing:
        lines.extend(["### Reappearing Researchers", ""])
        for row in reappearing:
            lines.extend(_format_issue_row(row))
    if emerging:
        lines.extend(
            [
                "### Emerging Activity",
                "",
                (
                    "> A recent-publication-rate signal only; not age, "
                    "career stage, seniority, quality, or citation impact."
                ),
                "",
            ]
        )
        for row in emerging:
            lines.extend(_format_issue_row(row))
    if not (new or outside or reappearing or emerging):
        lines.append("No researcher candidates met the current evidence requirements.")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_report_researcher_section(
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> str:
    """Render the fuller researcher section for weekly_brief.md."""
    lines = ["## Researcher Radar", ""]
    lines.extend(
        [
            (
                "**Researcher candidates:** "
                f"{summary.get('candidates', len(rows))} evaluated; "
                f"{summary.get('recommended', 0)} RECOMMENDED, "
                f"{summary.get('watch', 0)} WATCH, "
                f"{summary.get('outside_network', 0)} outside network, "
                f"{summary.get('reappearing', 0)} reappearing, "
                f"{summary.get('emerging_activity', 0)} emerging activity."
            ),
            "",
            (
                "> These categories are discovery aids. They are not "
                "judgments of researcher quality or career stage."
            ),
            "",
            (
                "> REAPPEARING requires at least two distinct ISO weeks in "
                "history; same-date reruns do not increase it."
            ),
            "",
        ]
    )
    top = [row for row in rows if row["status"] == RECOMMENDED][:10]
    if not top:
        top = rows[:10]
    if not top:
        lines.extend(["No researcher candidates were produced this run.", ""])
        return "\n".join(lines).rstrip()

    for number, row in enumerate(top, 1):
        lines.extend(
            [
                f"### {number}. {row['name']}",
                "",
                f"- **Status:** {row['status']}",
                f"- **Routes:** {_route_text(row) or 'topic evidence'}",
                f"- **Institution:** {row['institution']}",
                (
                    "- **Relevant papers:** "
                    f"{row['relevant_papers']} "
                    f"({row['recent_relevant_papers']} recent)"
                ),
                (
                    "- **Seed connections:** "
                    + (
                        ", ".join(row["seed_names"])
                        if row["seed_names"]
                        else "None observed"
                    )
                ),
                (
                    "- **Matched keywords:** "
                    + (", ".join(row["matched_keywords"][:6]) or "None")
                ),
                (
                    "- **Matched queries:** "
                    + (", ".join(row["matched_queries"][:4]) or "None")
                ),
                (
                    "- **History:** "
                    f"first seen {row['first_seen']}; "
                    f"last seen {row['last_seen']}; "
                    f"{row['appearances']} appearance(s), "
                    f"{row['weeks_seen']} week(s), "
                    f"{row['consecutive_weeks']} consecutive"
                ),
                (
                    "- **Emerging activity:** "
                    + (
                        row["emerging_activity_reason"]
                        if row.get("emerging_activity")
                        else "No"
                    )
                ),
                f"- **Why:** {row['why']}",
                f"- **OpenAlex:** https://openalex.org/{row['openalex_id']}",
                "",
            ]
        )
        if row["evidence_works"]:
            lines.append("**Evidence works:**")
            lines.append("")
            for paper in row["evidence_works"][:5]:
                lines.append(
                    f"- {paper.get('date') or 'Unknown date'} — "
                    f"{paper.get('title') or 'Untitled'}"
                )
            lines.append("")

    watch = [row for row in rows if row["status"] == WATCH][:10]
    if watch:
        lines.extend(["### WATCH candidates", ""])
        for row in watch:
            lines.append(
                f"- **{row['name']}** — {_compact_reason(row)} "
                f"(routes: {_route_text(row) or 'unclassified'})"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def create_radar_report(
    rows: list[dict[str, Any]],
    groups: dict[str, Any],
    config: dict[str, Any],
    summary: dict[str, Any] | None = None,
) -> str:
    """Create the complete Markdown researcher report."""
    summary = summary or {}
    lines = [
        "# Researcher Radar",
        "",
        f"**Generated:** {date.today().isoformat()}",
        "",
        (
            "> Recommendations require publication/topic evidence. "
            "Coauthorship alone is not sufficient. Categories are discovery "
            "aids, not scientific-quality or career-stage judgments."
        ),
        "",
    ]
    counts = Counter(
        signal for row in rows for signal in row.get("signals") or []
    )
    lines.extend(
        [
            "## Summary",
            "",
            (
                "- Candidates evaluated: "
                f"{summary.get('candidates', len(rows))}"
            ),
            (
                "- RECOMMENDED: "
                f"{summary.get('recommended', sum(1 for row in rows if row['status'] == RECOMMENDED))}"
            ),
            (
                "- WATCH: "
                f"{summary.get('watch', sum(1 for row in rows if row['status'] == WATCH))}"
            ),
            (
                "- OUTSIDE_NETWORK: "
                f"{summary.get('outside_network', counts.get(OUTSIDE_NETWORK, 0))}"
            ),
            (
                "- REAPPEARING: "
                f"{summary.get('reappearing', counts.get(REAPPEARING, 0))}"
            ),
            (
                "- EMERGING_ACTIVITY: "
                f"{summary.get('emerging_activity', 0)}"
            ),
            (
                "- Featured in this report: "
                f"{summary.get('featured', len(rows))}"
            ),
            "",
        ]
    )

    recommended = [
        row for row in rows if row["status"] == RECOMMENDED
    ][:10]
    lines.extend(["## Recommended researchers", ""])
    if not recommended:
        lines.extend(
            ["No candidate met the repeated-evidence recommendation rule.", ""]
        )
    for number, row in enumerate(recommended, 1):
        lines.extend(
            [
                f"### {number}. {row['name']}",
                "",
                f"- **OpenAlex:** https://openalex.org/{row['openalex_id']}",
                f"- **Institution:** {row['institution']}",
                f"- **Status:** {row['status']}",
                (
                    "- **Signals:** "
                    + (", ".join(row["signals"]) or "None")
                ),
                f"- **Routes:** {_route_text(row) or 'topic evidence'}",
                (
                    "- **Evidence:** "
                    f"{row['relevant_papers']} relevant paper(s), "
                    f"{row['recent_relevant_papers']} recent; "
                    f"{row['seed_connections']} seed connection(s)"
                ),
                (
                    "- **Matched keywords:** "
                    + (", ".join(row['matched_keywords'][:6]) or "None")
                ),
                (
                    "- **History:** "
                    f"first seen {row['first_seen']}; "
                    f"last seen {row['last_seen']}; "
                    f"{row['appearances']} appearance(s), "
                    f"{row['weeks_seen']} week(s), "
                    f"{row['consecutive_weeks']} consecutive"
                ),
                (
                    "- **Emerging activity:** "
                    + (
                        row.get("emerging_activity_reason") or "No"
                        if row.get("emerging_activity")
                        else "No"
                    )
                ),
                f"- **Why:** {row['why']}",
                "",
            ]
        )
        if row["evidence_works"]:
            lines.extend(["**Evidence works:**", ""])
            for paper in row["evidence_works"][:5]:
                lines.append(
                    f"- {paper.get('date') or 'Unknown date'} — "
                    f"{paper.get('title') or 'Untitled'} "
                    f"[{', '.join(paper.get('routes') or [])}]"
                )
            lines.append("")

    watch = [row for row in rows if row["status"] == WATCH][:15]
    if watch:
        lines.extend(["## WATCH candidates", ""])
        for row in watch:
            lines.append(
                f"- **{row['name']}** — {_compact_reason(row)} "
                f"(routes: {_route_text(row) or 'unclassified'})"
            )
        lines.append("")

    outside = [row for row in rows if row["outside_network"]][:10]
    if outside:
        lines.extend(["## Outside the current seed network", ""])
        for row in outside:
            lines.append(
                f"- **{row['name']}** — {_compact_reason(row)}"
            )
        lines.append("")

    reappearing = [row for row in rows if row["reappearing"]][:10]
    if reappearing:
        lines.extend(["## Reappearing researchers", ""])
        for row in reappearing:
            lines.append(
                f"- **{row['name']}** — {_compact_reason(row)}"
            )
        lines.append("")

    lines.extend(["## Group map", "", "### Closely connected groups", ""])
    for item in (groups.get("close_collaboration") or [])[:10]:
        lines.append(
            f"- **{item['group_1']} ↔ {item['group_2']}**: "
            f"{item['relevant_seed_coauthored_works']} relevant "
            f"seed-coauthored work(s); topic Jaccard "
            f"{item['topic_jaccard']}"
        )
    if not groups.get("close_collaboration"):
        lines.append("- No cross-group seed coauthorship found.")
    lines.extend(
        ["", "### Similar topics without observed collaboration", ""]
    )
    for item in (
        groups.get("similar_without_seed_collaboration") or []
    )[:10]:
        lines.append(
            f"- **{item['group_1']} ↔ {item['group_2']}**: topic Jaccard "
            f"{item['topic_jaccard']}; no relevant seed-coauthored work "
            "observed"
        )
    if not groups.get("similar_without_seed_collaboration"):
        lines.append("- No pair passed the configured similarity threshold.")

    lines.extend(
        [
            "",
            "## Interpretation limits",
            "",
            "- Categories describe evidence strength for discovery, not "
            "scientific quality or career stage.",
            "- Two relevant papers are normally required for RECOMMENDED; "
            "one paper is retained as WATCH.",
            "- History tracks candidates with at least two relevant papers "
            "plus the featured shortlist, to keep weekly state small.",
            "- Reappearance uses distinct run dates in the history file.",
            "- Missing or merged OpenAlex author records can affect results.",
            "",
        ]
    )
    return "\n".join(lines)


def _summary(
    all_rows: list[dict[str, Any]],
    featured: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "candidates": len(all_rows),
        "featured": len(featured),
        "recommended": sum(
            1 for row in all_rows if row["status"] == RECOMMENDED
        ),
        "watch": sum(1 for row in all_rows if row["status"] == WATCH),
        "outside_network": sum(
            1 for row in all_rows if row["outside_network"]
        ),
        "reappearing": sum(
            1 for row in all_rows if row["reappearing"]
        ),
        "emerging_activity": sum(
            1 for row in all_rows if row.get("emerging_activity")
        ),
        "multi_route": sum(
            1 for row in all_rows if len(row["routes"]) >= 2
        ),
        "enriched": sum(1 for row in all_rows if row["enriched"]),
        "enrichment_failed": sum(
            1 for row in all_rows if row["enrichment_failed"]
        ),
    }


def run_researcher_discovery(
    config: dict[str, Any],
    session: requests.Session,
    output_dir: str | Path | None = None,
    dry_run: bool = False,
    papers: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run multi-route discovery and write Markdown/JSON outputs.

    History is updated only after the report files are written and only in a
    non-dry run.  A corrupt history file is reported but never overwritten
    silently.
    """
    destination = Path(output_dir) if output_dir is not None else Path(".")
    destination.mkdir(parents=True, exist_ok=True)
    if hasattr(session, "current_route"):
        session.current_route = "radar"  # type: ignore[attr-defined]

    report_path = destination / OUTPUT_FILE.name
    data_path = destination / DATA_FILE.name
    history_path = destination / HISTORY_FILE.name
    history_read_path = Path(HISTORY_FILE) if dry_run else history_path

    run_date = date.today().isoformat()
    warnings: list[str] = []
    paper_list = list(papers or [])
    try:
        history = _load_history(history_read_path)
    except ValueError as error:
        history = {}
        warnings.append(str(error))

    rows, groups = discover_researchers(
        config,
        session,
        papers=paper_list,
        history=history,
        run_date=run_date,
    )
    dcfg = config.get("researcher_discovery") or {}
    maximum = int(dcfg.get("maximum_researchers", 20))
    featured = rows[: max(0, maximum)]
    summary = _summary(rows, featured)
    featured_ids = {row["openalex_id"] for row in featured}
    history_rows = [
        row
        for row in rows
        if row["relevant_papers"] >= 2
        or row["openalex_id"] in featured_ids
    ]
    stats = getattr(session, "stats", {}) or {}
    by_route = dict(
        stats.get("by_route") or getattr(session, "by_route", {}) or {}
    )
    session_requests = int(
        stats.get("requests", getattr(session, "requests", 0)) or 0
    )

    payload = {
        "schema_version": 3,
        "generated": run_date,
        "source": "phase1_papers",
        "run": {
            "papers_used": len(paper_list),
            "candidates": len(rows),
            "featured": len(featured),
            "enriched": summary["enriched"],
            "api_requests": int(by_route.get("radar", 0)),
            "session_requests": session_requests,
            "history_candidates": len(history_rows),
        },
        "summary": summary,
        "researchers": rows,
        "featured_researchers": featured,
        "groups": groups,
        "warnings": warnings,
    }

    _write_atomic(
        report_path,
        create_radar_report(featured, groups, config, summary),
    )

    history_updated = False
    if dry_run:
        print("Researcher radar dry run: history was not changed.")
    elif history_rows:
        try:
            _save_history(history_rows, run_date, history_path)
            history_updated = True
        except (OSError, ValueError) as error:
            warnings.append(f"Researcher history was not updated: {error}")
            print(f"Researcher radar history was not updated: {error}")

    _write_atomic(
        data_path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )

    issue_section = render_issue_researcher_section(featured, config)
    report_section = render_report_researcher_section(featured, summary)

    print(f"Generated researcher report: {report_path}")
    print(
        "Researcher candidates: "
        f"{len(rows)} total, {len(featured)} featured; "
        f"{summary['recommended']} recommended, "
        f"{summary['watch']} watch, "
        f"{summary['outside_network']} outside network"
    )
    return {
        "rows": featured,
        "summary": summary,
        "issue_section": issue_section,
        "report_section": report_section,
        "warnings": warnings,
        "history_updated": history_updated,
    }
