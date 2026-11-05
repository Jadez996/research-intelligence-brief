# Research Intelligence Pipeline

Automated research-monitoring and researcher-discovery pipeline using OpenAlex and GitHub Actions.

The project tracks selected researchers, finds relevant new publications, generates weekly research briefs, and discovers potentially relevant researchers and research-group connections.

## Core Workflow

    config.yml
        ↓
    generate_brief.py
        ├─ Query OpenAlex
        ├─ Retrieve tracked researchers' papers
        ├─ Score papers by configured keywords
        ├─ Remove previously processed works
        ├─ Generate weekly reports
        └─ Run researcher discovery
                ↓
        researcher_discovery.py
                ├─ Discover candidate researchers
                ├─ Analyse their own publications
                ├─ Analyse researcher/group connections
                └─ Maintain historical researcher data

GitHub Actions runs the pipeline automatically every week.

## Key Files

- `generate_brief.py`
  Main pipeline: OpenAlex retrieval, relevance scoring, duplicate handling, report generation, persistent state updates, and researcher-discovery invocation.

- `researcher_discovery.py`
  Discovers potentially relevant researchers and analyses researcher/group connections.

- `config.yml`
  Main user-editable configuration including tracked researchers, groups, keywords, thresholds, and discovery settings.

- `.github/workflows/weekly-brief.yml`
  GitHub Actions automation.

- `seen_works.json`
  Persistent record of already processed OpenAlex works.

- `researcher_history.json`
  Persistent historical researcher data.

- `researcher_radar.json`
  Structured researcher-discovery output.

## Generated Reports

The main generated outputs are:

    weekly_brief.md
    issue_brief.md
    researcher_radar.md
    researcher_radar.json

`weekly_brief.md` is the complete literature report.

`issue_brief.md` is intended to provide a shorter GitHub-Issue-safe version when the complete report becomes too long.

## Important Design Principles

### Literature monitoring

Paper relevance is based on configurable keywords and tracked researchers.

The relevance score represents similarity to my research interests. It does NOT represent scientific quality or importance.

Already processed papers are tracked using `seen_works.json` and should not normally appear again.

### Researcher discovery

A researcher must NOT be recommended simply because they collaborated with a tracked researcher.

Coauthorship is only used to discover candidates.

The candidate's own publication history must provide topic-relevant evidence before they are recommended.

Recommendation scores are prioritisation heuristics, not measurements of researcher quality.

### Research groups

Configured researcher groups can be compared using topic overlap and observed relevant collaboration.

The system is intended to identify both:

- groups with strong relevant collaboration;
- groups working on similar topics with little observed collaboration.

### Historical tracking

`researcher_history.json` is intended for longitudinal researcher analysis.

Do not replace historical data with only the latest result.

## Persistent State

Treat these files carefully:

    seen_works.json
    researcher_history.json

They affect future runs.

Do NOT automatically delete, reset, or recreate them during refactoring.

If OpenAlex requests completely fail, persistent state should not be updated as though the run succeeded.

Researcher-discovery failure should not prevent the main weekly literature report from being generated.

## OpenAlex

OpenAlex is the primary bibliographic data source.

Optional credentials are provided through:

    OPENALEX_EMAIL
    OPENALEX_API_KEY

Do not hard-code credentials.

Avoid unnecessary API requests. Prefer filtering candidates before requesting additional researcher information.

## Instructions for AI Coding Agents

Before modifying this project:

1. Read this README.
2. Inspect the relevant current source code.
3. Explain the current behaviour before making significant changes.
4. Identify the actual limitation or bug.
5. Make the smallest reliable change that solves the problem.
6. Preserve existing functionality unless explicitly asked to change it.
7. Avoid large architectural refactors unless they are necessary.
8. Protect persistent state and historical data.
9. Prefer configuration in `config.yml` over hard-coded research-specific values.
10. Preserve useful structured data in JSON rather than storing important information only in Markdown.
11. Keep OpenAlex API usage reasonably low.
12. Never assume this README is more current than the source code. Verify implementation details directly from the code.

When given a new development request, first inspect the affected code path instead of immediately editing files.

## Known Issue to Check

`generate_brief.py` generates `issue_brief.md` for GitHub Issue length handling, but the current GitHub Actions workflow uses `weekly_brief.md` when creating the GitHub Issue.

Check the current implementation before modifying the report-length/truncation system.

## Long-Term Goal

The project is intended to evolve into a personal research-intelligence system that can help answer:

- What important relevant papers appeared recently?
- What are my tracked researchers working on?
- Which new researchers are becoming relevant to my topics?
- Which researchers repeatedly appear around my research network?
- Which research groups work on similar topics?
- Which groups might have useful but currently limited collaboration?
- How are relevant topics and researcher networks changing over time?

The system should support scientific discovery and prioritisation, not replace scientific judgement.