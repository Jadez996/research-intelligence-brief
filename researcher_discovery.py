"""Seed-author researcher discovery for the Nanoconfined Water Bulletin.

The module deliberately separates evidence from interpretation. It recommends
researchers only when their OpenAlex works show topic relevance; coauthorship is
used as one signal, never as the sole criterion.
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
from typing import Any

import requests

OPENALEX_API = "https://api.openalex.org"
OUTPUT_FILE = Path("researcher_radar.md")
DATA_FILE = Path("researcher_radar.json")
HISTORY_FILE = Path("researcher_history.json")


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
            positions.extend((i, str(word)) for i in indexes if isinstance(i, int))
    return " ".join(word for _, word in sorted(positions))


def _contains(keyword: str, text: str) -> bool:
    keyword = _normalise(keyword)
    if len(keyword) <= 4 and keyword.isalpha():
        return re.search(rf"\b{re.escape(keyword)}\b", text, re.I) is not None
    return keyword in text


def work_relevance(work: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Score topical evidence without awarding points for author identity."""
    kcfg = config.get("keywords") or {}
    high = [str(x) for x in kcfg.get("high_priority") or []]
    medium = [str(x) for x in kcfg.get("medium_priority") or []]
    exclusion = [str(x) for x in kcfg.get("exclusion") or []]
    topic_names = [
        str(t.get("display_name") or "")
        for t in work.get("topics") or [] if isinstance(t, dict)
    ]
    keyword_names = [
        str(k.get("display_name") or "")
        for k in work.get("keywords") or [] if isinstance(k, dict)
    ]
    text = _normalise(" ".join([
        str(work.get("title") or ""), _abstract(work.get("abstract_inverted_index")),
        " ".join(topic_names), " ".join(keyword_names),
    ]))
    mh = [k for k in high if _contains(k, text)]
    mm = [k for k in medium if _contains(k, text)]
    me = [k for k in exclusion if _contains(k, text)]
    score = 3 * len(mh) + len(mm) - 4 * len(me)
    topic_ids = {_id(t.get("id")) for t in work.get("topics") or [] if isinstance(t, dict) and t.get("id")}
    return {"score": score, "high": mh, "medium": mm, "exclusion": me,
            "topic_ids": topic_ids, "topic_names": topic_names}


def _get(session: requests.Session, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    email = os.getenv("OPENALEX_EMAIL", "").strip()
    api_key = os.getenv("OPENALEX_API_KEY", "").strip()
    if email:
        params["mailto"] = email
    if api_key:
        params["api_key"] = api_key
    response = session.get(f"{OPENALEX_API}/{endpoint}", params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def fetch_author_works(session: requests.Session, author_id: str, from_date: str,
                       per_page: int, pages: int = 1) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cursor = "*"
    for _ in range(max(1, pages)):
        payload = _get(session, "works", {
            "filter": f"authorships.author.id:{_id(author_id)},from_publication_date:{from_date}",
            "per_page": min(200, max(1, per_page)),
            "cursor": cursor,
            "sort": "publication_date:desc",
            "select": "id,title,publication_date,publication_year,cited_by_count,authorships,abstract_inverted_index,topics,keywords",
        })
        batch = [x for x in payload.get("results") or [] if isinstance(x, dict)]
        results.extend(batch)
        cursor = str((payload.get("meta") or {}).get("next_cursor") or "")
        if not batch or not cursor:
            break
        time.sleep(0.1)
    return results


def fetch_author_profile(session: requests.Session, author_id: str) -> dict[str, Any]:
    return _get(session, f"authors/{_id(author_id)}", {
        "select": "id,display_name,works_count,cited_by_count,summary_stats,last_known_institutions,topics,counts_by_year"
    })


def _authorships(work: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in work.get("authorships") or [] if isinstance(a, dict)]


def _load_history() -> dict[str, Any]:
    try:
        value = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_history(rows: list[dict[str, Any]], run_date: str) -> None:
    history = _load_history()
    for row in rows:
        aid = row["openalex_id"]
        entries = history.setdefault(aid, [])
        entries.append({"date": run_date, "score": row["recommendation_score"],
                        "recent_relevant_works": row["recent_relevant_works"]})
        history[aid] = entries[-52:]
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def discover_researchers(config: dict[str, Any], session: requests.Session) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    dcfg = config.get("researcher_discovery") or {}
    if not dcfg.get("enabled", True):
        return [], {}
    seeds = [a for a in config.get("authors") or [] if isinstance(a, dict) and a.get("openalex_id")]
    seed_ids = {_id(a["openalex_id"]) for a in seeds}
    seed_by_id = {_id(a["openalex_id"]): a for a in seeds}
    analysis_years = int(dcfg.get("analysis_years", 5))
    works_per_seed = int(dcfg.get("works_per_seed", 100))
    pages = max(1, (works_per_seed + 199) // 200)
    from_date = (date.today() - timedelta(days=365 * analysis_years)).isoformat()
    minimum_work_score = int(dcfg.get("minimum_work_relevance", 2))

    candidates: dict[str, dict[str, Any]] = {}
    seed_group_topics: dict[str, Counter[str]] = defaultdict(Counter)
    group_edges: Counter[tuple[str, str]] = Counter()

    for seed in seeds:
        sid = _id(seed["openalex_id"])
        group = str(seed.get("group") or "Ungrouped")
        try:
            works = fetch_author_works(session, sid, from_date, works_per_seed, pages)
        except requests.RequestException as exc:
            print(f"Researcher discovery: failed seed {seed.get('name')}: {exc}")
            continue
        for work in works[:works_per_seed]:
            rel = work_relevance(work, config)
            if rel["score"] < minimum_work_score or rel["exclusion"]:
                continue
            for tid in rel["topic_ids"]:
                seed_group_topics[group][tid] += 1
            present_seed_groups = sorted({
                str(seed_by_id[aid].get("group") or "Ungrouped")
                for a in _authorships(work)
                if (aid := _id((a.get("author") or {}).get("id"))) in seed_ids
            })
            for i, g1 in enumerate(present_seed_groups):
                for g2 in present_seed_groups[i + 1:]:
                    group_edges[tuple(sorted((g1, g2)))] += 1
            for authorship in _authorships(work):
                author = authorship.get("author") or {}
                aid = _id(author.get("id"))
                if not aid or aid in seed_ids:
                    continue
                row = candidates.setdefault(aid, {
                    "openalex_id": aid,
                    "name": str(author.get("display_name") or aid),
                    "seed_ids": set(), "seed_names": set(), "seed_groups": set(),
                    "coauthored_relevant_works": 0, "keyword_counts": Counter(),
                    "topic_counts": Counter(), "evidence_works": [],
                })
                row["seed_ids"].add(sid)
                row["seed_names"].add(str(seed.get("name") or sid))
                row["seed_groups"].add(group)
                row["coauthored_relevant_works"] += 1
                row["keyword_counts"].update(rel["high"] + rel["medium"])
                row["topic_counts"].update(rel["topic_ids"])
                if len(row["evidence_works"]) < 5:
                    row["evidence_works"].append({"title": work.get("title"), "date": work.get("publication_date"), "score": rel["score"]})
        time.sleep(0.15)

    # Only enrich plausible candidates. This keeps API use bounded.
    pool_size = int(dcfg.get("candidate_pool_size", 50))
    shortlist = sorted(candidates.values(), key=lambda r: (
        len(r["seed_ids"]), r["coauthored_relevant_works"], sum(r["keyword_counts"].values())
    ), reverse=True)[:pool_size]

    recent_years = int(dcfg.get("recent_years", 2))
    recent_from = (date.today() - timedelta(days=365 * recent_years)).isoformat()
    early_career_years = int(dcfg.get("early_career_first_publication_years", 8))
    max_results = int(dcfg.get("maximum_researchers", 20))
    rows = []
    for row in shortlist:
        aid = row["openalex_id"]
        try:
            candidate_works = fetch_author_works(session, aid, from_date, 200, 1)
            profile = fetch_author_profile(session, aid)
        except requests.RequestException as exc:
            print(f"Researcher discovery: failed candidate {row['name']}: {exc}")
            continue
        recent_relevant = []
        prior_relevant = []
        recent_topics = Counter()
        first_year = None
        for work in candidate_works:
            year = work.get("publication_year")
            if isinstance(year, int):
                first_year = year if first_year is None else min(first_year, year)
            rel = work_relevance(work, config)
            if rel["score"] >= minimum_work_score and not rel["exclusion"]:
                publication_date = str(work.get("publication_date") or "")
                if publication_date >= recent_from:
                    recent_relevant.append((work, rel))
                    recent_topics.update(rel["topic_ids"])
                else:
                    prior_relevant.append((work, rel))
                row["keyword_counts"].update(rel["high"] + rel["medium"])
        # OpenAlex profile may expose an earliest publication year in counts_by_year.
        years = [x.get("year") for x in profile.get("counts_by_year") or [] if isinstance(x, dict) and isinstance(x.get("year"), int)]
        if years:
            first_year = min(years)
        current_year = date.today().year
        early_signal = bool(first_year and current_year - first_year <= early_career_years)
        seed_connections = len(row["seed_ids"])
        group_connections = len(row["seed_groups"])
        topic_strength = len(row["keyword_counts"])
        recent_count = len(recent_relevant)
        prior_years = max(1, analysis_years - recent_years)
        recent_rate = recent_count / max(1, recent_years)
        prior_rate = len(prior_relevant) / prior_years
        emerging_topic_signal = recent_count >= 2 and (prior_rate == 0 or recent_rate >= 2 * prior_rate)
        score = round(
            2.0 * min(row["coauthored_relevant_works"], 5)
            + 3.0 * max(0, seed_connections - 1)
            + 2.0 * max(0, group_connections - 1)
            + 1.5 * min(topic_strength, 6)
            + 1.5 * min(recent_count, 5), 1)
        institutions = [str(x.get("display_name")) for x in profile.get("last_known_institutions") or [] if isinstance(x, dict) and x.get("display_name")]
        reasons = []
        if row["coauthored_relevant_works"]:
            reasons.append(f"{row['coauthored_relevant_works']} topic-relevant coauthored work(s) with seed researchers")
        if seed_connections > 1:
            reasons.append(f"connected to {seed_connections} seed researchers")
        if group_connections > 1:
            reasons.append(f"bridges {group_connections} configured groups")
        if recent_count:
            reasons.append(f"{recent_count} relevant work(s) in the recent analysis window")
        if emerging_topic_signal:
            reasons.append("recent topic-publication rate is at least twice the earlier-window rate")
        if row["keyword_counts"]:
            reasons.append("matched topics: " + ", ".join(k for k, _ in row["keyword_counts"].most_common(5)))
        rows.append({
            "openalex_id": aid, "name": row["name"], "institution": institutions[0] if institutions else "Unknown",
            "recommendation_score": score, "seed_connections": seed_connections,
            "seed_names": sorted(row["seed_names"]), "groups": sorted(row["seed_groups"]),
            "coauthored_relevant_works": row["coauthored_relevant_works"],
            "recent_relevant_works": recent_count, "first_publication_year_signal": first_year,
            "early_career_signal": early_signal, "emerging_topic_signal": emerging_topic_signal,
            "recent_topic_rate_per_year": round(recent_rate, 2), "prior_topic_rate_per_year": round(prior_rate, 2), "top_keywords": [k for k, _ in row["keyword_counts"].most_common(8)],
            "reasons": reasons, "evidence_works": row["evidence_works"],
            "works_count": profile.get("works_count"), "cited_by_count": profile.get("cited_by_count"),
        })
        time.sleep(0.15)

    rows.sort(key=lambda r: (r["recommendation_score"], r["recent_relevant_works"]), reverse=True)
    rows = rows[:max_results]

    groups = sorted(seed_group_topics)
    group_analysis = {"close_collaboration": [], "similar_without_seed_collaboration": []}
    for i, g1 in enumerate(groups):
        s1 = set(seed_group_topics[g1])
        for g2 in groups[i + 1:]:
            s2 = set(seed_group_topics[g2])
            union = s1 | s2
            similarity = len(s1 & s2) / len(union) if union else 0.0
            pair = tuple(sorted((g1, g2)))
            edge_count = group_edges.get(pair, 0)
            item = {"group_1": g1, "group_2": g2, "topic_jaccard": round(similarity, 3), "relevant_seed_coauthored_works": edge_count}
            if edge_count:
                group_analysis["close_collaboration"].append(item)
            elif similarity >= float(dcfg.get("group_similarity_threshold", 0.15)):
                group_analysis["similar_without_seed_collaboration"].append(item)
    group_analysis["close_collaboration"].sort(key=lambda x: (x["relevant_seed_coauthored_works"], x["topic_jaccard"]), reverse=True)
    group_analysis["similar_without_seed_collaboration"].sort(key=lambda x: x["topic_jaccard"], reverse=True)
    return rows, group_analysis


def create_radar_report(rows: list[dict[str, Any]], groups: dict[str, Any], config: dict[str, Any]) -> str:
    lines = ["# Researcher Radar", "", f"**Generated:** {date.today().isoformat()}", "",
             "> Recommendations require topic-relevant publication evidence. Coauthorship alone is not sufficient. "
             "The early-career flag is a publication-history signal, not a claim about age or career stage.", ""]
    lines += ["## Recommended researchers", ""]
    if not rows:
        lines += ["No candidates met the current evidence requirements.", ""]
    for i, r in enumerate(rows, 1):
        lines += [f"### {i}. {r['name']}", "",
                  f"- **OpenAlex:** https://openalex.org/{r['openalex_id']}",
                  f"- **Institution:** {r['institution']}",
                  f"- **Recommendation score:** {r['recommendation_score']}",
                  f"- **Connected seeds:** {', '.join(r['seed_names'])}",
                  f"- **Configured groups:** {', '.join(r['groups'])}",
                  f"- **Recent relevant works:** {r['recent_relevant_works']}",
                  f"- **Emerging-topic signal:** {'Yes' if r['emerging_topic_signal'] else 'No'} (recent rate {r['recent_topic_rate_per_year']}/year; earlier rate {r['prior_topic_rate_per_year']}/year)",
                  f"- **Early-career publication signal:** {'Yes' if r['early_career_signal'] else 'No/insufficient evidence'}"
                  + (f" (first-year signal: {r['first_publication_year_signal']})" if r['first_publication_year_signal'] else ""),
                  "- **Why recommended:** " + "; ".join(r["reasons"]), ""]
        if r["evidence_works"]:
            lines += ["**Evidence works:**", ""]
            for w in r["evidence_works"]:
                lines.append(f"- {w.get('date') or 'Unknown date'} — {w.get('title') or 'Untitled'}")
            lines.append("")
    lines += ["## Group map", "", "### Closely connected configured groups", ""]
    for x in (groups.get("close_collaboration") or [])[:10]:
        lines.append(f"- **{x['group_1']} ↔ {x['group_2']}**: {x['relevant_seed_coauthored_works']} relevant seed-coauthored work(s); topic Jaccard {x['topic_jaccard']}")
    if not groups.get("close_collaboration"):
        lines.append("- No cross-group seed coauthorship found in the analysis window.")
    lines += ["", "### Similar topics without observed seed-group collaboration", ""]
    for x in (groups.get("similar_without_seed_collaboration") or [])[:10]:
        lines.append(f"- **{x['group_1']} ↔ {x['group_2']}**: topic Jaccard {x['topic_jaccard']}; no relevant seed-coauthored work observed")
    if not groups.get("similar_without_seed_collaboration"):
        lines.append("- No pair passed the configured similarity threshold.")
    lines += ["", "## Interpretation limits", "",
              "- ‘Core researcher’ here means strongly connected and topic-relevant within this seed-defined graph, not globally authoritative.",
              "- ‘Sudden entry’ requires repeated historical runs. The JSON history file stores weekly snapshots; confidence improves after several runs.",
              "- Missing or merged OpenAlex author records can affect results.", ""]
    return "\n".join(lines)


def run_researcher_discovery(config: dict[str, Any], session: requests.Session) -> None:
    rows, groups = discover_researchers(config, session)
    report = create_radar_report(rows, groups, config)
    OUTPUT_FILE.write_text(report, encoding="utf-8")
    DATA_FILE.write_text(json.dumps({"generated": date.today().isoformat(), "researchers": rows, "groups": groups}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _save_history(rows, date.today().isoformat())
    print(f"Generated researcher report: {OUTPUT_FILE}")
    print(f"Researchers included: {len(rows)}")
