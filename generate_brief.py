import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml


CONFIG_FILE = Path("config.yml")
SEEN_FILE = Path("seen_works.json")
OUTPUT_FILE = Path("weekly_brief.md")

OPENALEX_API = "https://api.openalex.org"


def load_yaml(path):
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def load_seen_works():
    if not SEEN_FILE.exists():
        return set()

    try:
        with SEEN_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
            return set(data)
    except (json.JSONDecodeError, TypeError):
        return set()


def save_seen_works(work_ids):
    with SEEN_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            sorted(work_ids),
            file,
            ensure_ascii=False,
            indent=2,
        )


def reconstruct_abstract(inverted_index):
    if not inverted_index:
        return ""

    positions = []

    for word, indexes in inverted_index.items():
        for index in indexes:
            positions.append((index, word))

    positions.sort(key=lambda item: item[0])

    abstract = " ".join(word for _, word in positions)
    return html.unescape(abstract).strip()


def normalise_text(text):
    if not text:
        return ""

    text = html.unescape(text)
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def keyword_is_present(keyword, text):
    keyword = normalise_text(keyword)

    if not keyword:
        return False

    if len(keyword) <= 4 and keyword.isalpha():
        pattern = rf"\b{re.escape(keyword)}\b"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None

    return keyword in text


def score_work(work, config, tracked_author_name):
    title = work.get("title") or ""
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    topics = work.get("topics") or []
    keywords_metadata = work.get("keywords") or []

    topic_text = " ".join(
        topic.get("display_name", "")
        for topic in topics
        if isinstance(topic, dict)
    )

    keyword_metadata_text = " ".join(
        keyword.get("display_name", "")
        for keyword in keywords_metadata
        if isinstance(keyword, dict)
    )

    searchable_text = normalise_text(
        " ".join(
            [
                title,
                abstract,
                topic_text,
                keyword_metadata_text,
            ]
        )
    )

    keyword_config = config.get("keywords", {})

    high_priority = keyword_config.get("high_priority", [])
    medium_priority = keyword_config.get("medium_priority", [])
    exclusions = keyword_config.get("exclusion", [])

    score = 2
    matched_high = []
    matched_medium = []
    matched_exclusion = []

    for keyword in high_priority:
        if keyword_is_present(keyword, searchable_text):
            score += 3
            matched_high.append(keyword)

    for keyword in medium_priority:
        if keyword_is_present(keyword, searchable_text):
            score += 1
            matched_medium.append(keyword)

    for keyword in exclusions:
        if keyword_is_present(keyword, searchable_text):
            score -= 4
            matched_exclusion.append(keyword)

    return {
        "score": score,
        "matched_high": matched_high,
        "matched_medium": matched_medium,
        "matched_exclusion": matched_exclusion,
        "abstract": abstract,
        "tracked_author": tracked_author_name,
    }


def get_primary_source(work):
    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    return source.get("display_name") or "未注明来源"


def get_authors(work):
    names = []

    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        name = author.get("display_name")

        if name:
            names.append(name)

    return names


def get_work_url(work):
    doi = work.get("doi")

    if doi:
        return doi

    primary_location = work.get("primary_location") or {}
    landing_page = primary_location.get("landing_page_url")

    if landing_page:
        return landing_page

    return work.get("id") or ""


def fetch_recent_works(author_id, start_date, end_date, email):
    author_id = author_id.strip()

    if author_id.startswith("https://openalex.org/"):
        author_id = author_id.rsplit("/", 1)[-1]

    filters = (
        f"authorships.author.id:{author_id},"
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
            "abstract_inverted_index,topics,keywords,type"
        ),
    }

    if email:
        params["mailto"] = email

    response = requests.get(
        f"{OPENALEX_API}/works",
        params=params,
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    return payload.get("results", [])


def truncate_abstract(text, maximum_length=1200):
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return "OpenAlex 中未提供摘要。"

    if len(text) <= maximum_length:
        return text

    shortened = text[:maximum_length]
    last_full_stop = max(
        shortened.rfind(". "),
        shortened.rfind("。"),
    )

    if last_full_stop > 500:
        shortened = shortened[: last_full_stop + 1]

    return shortened.rstrip() + "……"


def relevance_label(score):
    if score >= 8:
        return "高度相关"
    if score >= 4:
        return "较相关"
    return "可能相关"


def create_report(config, papers, start_date, end_date):
    brief_config = config.get("brief", {})
    title = brief_config.get(
        "title",
        "科研情报简报",
    )

    lines = [
        f"# {title}",
        "",
        f"**检索日期范围：** {start_date} 至 {end_date}",
        "",
        f"**符合筛选条件的论文数：** {len(papers)}",
        "",
        "> 本简报由 OpenAlex 元数据自动生成。"
        "相关性分数来自本仓库配置的关键词规则，"
        "不代表论文质量、学术影响力或同行评价。",
        "",
    ]

    if not papers:
        lines.extend(
            [
                "本期没有发现达到最低相关性分数的新论文。",
                "",
            ]
        )
        return "\n".join(lines)

    grouped = {
        "高度相关": [],
        "较相关": [],
        "可能相关": [],
    }

    for paper in papers:
        grouped[relevance_label(paper["analysis"]["score"])].append(paper)

    paper_number = 1

    for category in ["高度相关", "较相关", "可能相关"\]:
        category_papers = grouped[category]

        if not category_papers:
            continue

        lines.extend([f"## {category}", ""])

        for paper in category_papers:
            work = paper["work"]
            analysis = paper["analysis"]

            title_text = work.get("title") or "无标题"
            publication_date = work.get("publication_date") or "日期未知"
            source = get_primary_source(work)
            authors = get_authors(work)
            work_url = get_work_url(work)

            displayed_authors = authors[:8]
            authors_text = ", ".join(displayed_authors)

            if len(authors) > 8:
                authors_text += f" 等，共 {len(authors)} 位作者"

            matched_keywords = (
                analysis["matched_high"]
                + analysis["matched_medium"]
            )

            matched_keywords_text = (
                ", ".join(f"`{keyword}`" for keyword in matched_keywords)
                if matched_keywords
                else "仅因被追踪作者身份进入候选结果"
            )

            lines.extend(
                [
                    f"### {paper_number}. {title_text}",
                    "",
                    f"- **追踪来源：** {analysis['tracked_author']}",
                    f"- **作者：** {authors_text or '作者信息缺失'}",
                    f"- **发表日期：** {publication_date}",
                    f"- **期刊或来源：** {source}",
                    f"- **文献类型：** {work.get('type') or '未知'}",
                    f"- **相关性分数：** {analysis['score']}",
                    f"- **命中关键词：** {matched_keywords_text}",
                ]
            )

            if analysis["matched_exclusion"\]:
                exclusions = ", ".join(
                    f"`{keyword}`"
                    for keyword in analysis["matched_exclusion"]
                )
                lines.append(f"- **命中排除词：** {exclusions}")

            if work_url:
                lines.append(f"- **论文链接：** {work_url}")

            lines.extend(
                [
                    "",
                    "**摘要：**",
                    "",
                    truncate_abstract(analysis["abstract"]),
                    "",
                    "---",
                    "",
                ]
            )

            paper_number += 1

    return "\n".join(lines)


def main():
    if not CONFIG_FILE.exists():
        print("错误：找不到 config.yml")
        sys.exit(1)

    config = load_yaml(CONFIG_FILE)
    brief_config = config.get("brief", {})

    lookback_days = int(brief_config.get("lookback_days", 10))
    minimum_score = int(brief_config.get("minimum_score", 2))
    maximum_papers = int(brief_config.get("maximum_papers", 30))

    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=lookback_days)

    start_date_text = start_date.isoformat()
    end_date_text = today.isoformat()

    contact_email = os.getenv("OPENALEX_EMAIL", "").strip()

    seen_works = load_seen_works()
    newly_seen_works = set(seen_works)

    collected = {}

    authors = config.get("authors", [])

    for tracked_author in authors:
        name = tracked_author.get("name", "").strip()
        author_id = tracked_author.get("openalex_id", "").strip()

        if not name or not author_id:
            print(f"跳过配置不完整的作者：{tracked_author}")
            continue

        if "请替换" in author_id:
            print(f"跳过尚未配置 ID 的作者：{name}")
            continue

        print(f"正在查询：{name} ({author_id})")

        try:
            works = fetch_recent_works(
                author_id=author_id,
                start_date=start_date_text,
                end_date=end_date_text,
                email=contact_email,
            )
        except requests.RequestException as error:
            print(f"查询 {name} 时失败：{error}")
            continue

        for work in works:
            work_id = work.get("id")

            if not work_id:
                continue

            if work_id in seen_works:
                continue

            analysis = score_work(
                work=work,
                config=config,
                tracked_author_name=name,
            )

            newly_seen_works.add(work_id)

            if analysis["score"] < minimum_score:
                continue

            existing = collected.get(work_id)

            if existing:
                previous_names = existing["analysis"]["tracked_author"]
                if name not in previous_names:
                    existing["analysis"]["tracked_author"] = (
                        previous_names + "; " + name
                    )
                continue

            collected[work_id] = {
                "work": work,
                "analysis": analysis,
            }

        time.sleep(0.2)

    papers = list(collected.values())

    papers.sort(
        key=lambda item: (
            item["analysis"]["score"],
            item["work"].get("publication_date") or "",
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

    OUTPUT_FILE.write_text(report, encoding="utf-8")
    save_seen_works(newly_seen_works)

    print(f"已生成：{OUTPUT_FILE}")
    print(f"本期收录论文：{len(papers)}")


if __name__ == "__main__":
    main()
