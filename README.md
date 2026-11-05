# Research Intelligence Pipeline

Automated research-monitoring and researcher-discovery pipeline using OpenAlex and GitHub Actions.

The project tracks selected researchers, finds relevant new publications, generates weekly research briefs, and discovers potentially relevant researchers and research-group connections.

## Core Workflow

    config.yml
        ↓
    generate_brief.py
<<<<<<< HEAD
<<<<<<< HEAD
        ├─ Query tracked researchers' recent papers (seed route)
        ├─ Query configured topics on OpenAlex (topic-first route)
        ├─ Merge and deduplicate results
        ├─ Score papers by configured keywords, routes, and recency
        ├─ Build structured paper data and the run-status manifest
        ├─ Write JSON/CSV and the complete/Issue Markdown reports
        ├─ Update seen_works.json with qualifying papers only
        └─ Run researcher discovery (optional)
                ↓
        researcher_discovery.py
                ├─ Reuse the papers already retrieved by both paper routes
                ├─ Combine seed, topic-first, and recurring-literature evidence
                ├─ Enrich only the shortlisted candidates with their recent works
                ├─ Classify RECOMMENDED/WATCH with OUTSIDE_NETWORK/REAPPEARING/EMERGING_ACTIVITY signals
                ├─ Maintain historical researcher data
                └─ Render researcher JSON/Markdown and a concise Issue section
=======
=======
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
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
<<<<<<< HEAD
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
=======
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e

GitHub Actions runs the pipeline automatically every week.

## Key Files

- `generate_brief.py`
<<<<<<< HEAD
<<<<<<< HEAD
  Main pipeline: OpenAlex retrieval (seed and topic routes), relevance scoring, duplicate handling, report generation, persistent state updates, and researcher-discovery invocation.

- `researcher_discovery.py`
  Combines seed/coauthor, topic-first, and recurring-literature evidence into a ranked researcher radar. It reuses the papers retrieved for the weekly brief, enriches only shortlisted candidates, and writes the structured radar and historical state.

- `config.yml`
  Main user-editable configuration including tracked researchers, groups, research queries, keywords, thresholds, and discovery settings.
=======
=======
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
  Main pipeline: OpenAlex retrieval, relevance scoring, duplicate handling, report generation, persistent state updates, and researcher-discovery invocation.

- `researcher_discovery.py`
  Discovers potentially relevant researchers and analyses researcher/group connections.

- `config.yml`
  Main user-editable configuration including tracked researchers, groups, keywords, thresholds, and discovery settings.
<<<<<<< HEAD
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
=======
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e

- `.github/workflows/weekly-brief.yml`
  GitHub Actions automation.

- `seen_works.json`
<<<<<<< HEAD
<<<<<<< HEAD
  Persistent record of already processed qualifying OpenAlex works.
=======
  Persistent record of already processed OpenAlex works.
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
=======
  Persistent record of already processed OpenAlex works.
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e

- `researcher_history.json`
  Persistent historical researcher data.

- `researcher_radar.json`
  Structured researcher-discovery output.

<<<<<<< HEAD
<<<<<<< HEAD
- `weekly_papers.json`
  Authoritative structured output for the current weekly run: papers with evidence, run statistics, warnings, and config fingerprint.

- `weekly_papers.csv`
  Flat paper table for spreadsheet inspection.

- `run_manifest.json`
  Structured run-status manifest: complete/partial/failed, route success and failure, OpenAlex requests/retries/failures/cost, state update, counts, and warnings.

=======
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
=======
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
## Generated Reports

The main generated outputs are:

    weekly_brief.md
    issue_brief.md
<<<<<<< HEAD
<<<<<<< HEAD
    weekly_papers.json
    weekly_papers.csv
    researcher_radar.md
    researcher_radar.json
    run_manifest.json

`weekly_brief.md` is the complete literature report.

`issue_brief.md` is the concise GitHub-Issue version: selected top papers,
selected tracked-author papers, and a short researcher section (new watch
candidates, outside-network discoveries, reappearing and emerging researchers),
without abstracts. The Issue is an executive summary; the complete evidence
remains in `weekly_papers.json`, `weekly_papers.csv`, and
`researcher_radar.json`.

`run_manifest.json` records whether the run was complete or partial, which
routes succeeded or failed, OpenAlex usage, state update status, and warnings.
A partial run never looks identical to a complete run.

The workflow creates the GitHub Issue from `issue_brief.md` and commits/uploads the structured outputs alongside it.
=======
=======
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
    researcher_radar.md
    researcher_radar.json

`weekly_brief.md` is the complete literature report.

`issue_brief.md` is intended to provide a shorter GitHub-Issue-safe version when the complete report becomes too long.
<<<<<<< HEAD
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
=======
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e

## Important Design Principles

### Literature monitoring

Paper relevance is based on configurable keywords and tracked researchers.

The relevance score represents similarity to my research interests. It does NOT represent scientific quality or importance.

<<<<<<< HEAD
<<<<<<< HEAD
Papers are discovered through two routes:

- **Seed route**: recent papers by configured tracked researchers.
- **Topic-first route**: recent OpenAlex results for the `paper_discovery.queries` in `config.yml`, filtered by configured keywords.

Results from both routes are merged and deduplicated by OpenAlex ID, DOI, and normalised title/year. Each reported paper carries its own evidence: matched keywords, matched queries, discovery route(s), and tracked authors where applicable. Citation counts are stored in the structured output but are not part of the relevance score, so recent papers are not penalised for having few citations.

### Topic-first discovery

`paper_discovery` controls topic-first discovery:

- `enabled`
- `queries`
- `lookback_days`
- `results_per_query` (maximum 100 per query)
- `query_bonus` / `max_query_bonus`
- `recent_days` / `recent_bonus`
- `include_types`
- `exclude_retracted`
- `require_keyword_match`
- `candidate_authors_limit`

A topic-only paper must match at least one configured keyword when `require_keyword_match` is true; this prevents broad OpenAlex topic names from creating noise. One-paper authors are retained by the researcher radar as `WATCH` candidates with `outside_network` evidence. The legacy preliminary author block was removed from `weekly_papers.json` in Phase 3; `candidate_authors_limit` remains only for backward compatibility.

### Researcher discovery

Three complementary routes contribute evidence:

- **Seed/network route**: a tracked researcher's relevant paper → its coauthors.
- **Topic-first route**: a configured query → relevant paper → authors, including
  researchers with no seed-network connection.
- **Literature-recurrence route**: multiple relevant papers → an author who
  recurs across them.

Evidence from the routes is merged per OpenAlex author ID rather than collapsed
into one unexplained number. The structured radar records the routes, relevant
and recent paper counts, strongest papers, matched keywords and queries,
seed-author connections, institution, first/last seen, weekly appearances, and
a readable reason for the recommendation.

A researcher must NOT be recommended simply because they collaborated with a
tracked researcher. Coauthorship only discovers candidates; the candidate's own
publication evidence must support the recommendation. One strongly relevant
paper normally produces `WATCH`, not `RECOMMENDED`; a one-paper coauthor is
never promoted for coauthorship alone.

OpenAlex sometimes holds more than one author ID for the same person. Candidate
records whose normalised name matches a configured seed are excluded when
`exclude_seed_names` is true (the default), so a duplicate ID cannot reappear as
a new outside-network discovery.

Categories and signals are discovery aids, overlapping rather than mutually
exclusive, and are not judgments of researcher quality or career stage:

- `RECOMMENDED`: repeated or strong topic-relevant evidence.
- `WATCH`: promising but insufficient evidence, including one-paper candidates.
- `OUTSIDE_NETWORK`: no meaningful seed-author connection.
- `REAPPEARING`: seen in at least two distinct ISO weeks in the history.
- `EMERGING_ACTIVITY`: recent relevant-paper rate is above the configured
  threshold. This is a discovery signal only; it says nothing about age,
  career stage, seniority, or researcher quality.

Same-week reruns replace the current week's history entry rather than adding
another appearance. A first appearance is therefore not `REAPPEARING`.

Evidence records are deduplicated by strong OpenAlex Work ID or normalised DOI;
a title-only match is used only when neither record has a strong identifier.
Dataset and paratext records are excluded. When duplicate route records are
merged, the routes are preserved on the surviving evidence item.

Only the shortlisted candidates are enriched with one author-works request each;
the rest of the candidate set remains in `researcher_radar.json` with the
evidence already available from the weekly papers.
=======
=======
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
Already processed papers are tracked using `seen_works.json` and should not normally appear again.

### Researcher discovery

A researcher must NOT be recommended simply because they collaborated with a tracked researcher.

Coauthorship is only used to discover candidates.

The candidate's own publication history must provide topic-relevant evidence before they are recommended.

Recommendation scores are prioritisation heuristics, not measurements of researcher quality.
<<<<<<< HEAD
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
=======
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e

### Research groups

Configured researcher groups can be compared using topic overlap and observed relevant collaboration.

The system is intended to identify both:

- groups with strong relevant collaboration;
- groups working on similar topics with little observed collaboration.

### Historical tracking

<<<<<<< HEAD
<<<<<<< HEAD
`researcher_history.json` stores a compact per-author history keyed by OpenAlex
author ID. Each dated entry records the score, status, routes, outside-network,
reappearing and emerging-activity signals, first/last seen, total appearances,
distinct weeks seen, consecutive weeks, category history, and relevant/recent
paper counts. Existing entries from the older schema remain readable; a corrupt
history file is reported and is not overwritten.

The emerging-activity rule is deterministic and uses only relevant-paper counts:
a candidate needs at least `emerging_min_recent` recent relevant papers and
either no earlier relevant papers or a recent yearly rate at least
`emerging_rate_multiplier` times the earlier rate. Citation counts are not used.

To keep weekly state bounded, history is written for candidates with at least
two relevant papers and for the featured shortlist, not for every one-paper
author. Dry runs never modify it. History is written atomically after the radar
outputs, and a history-write failure does not prevent the weekly brief from being
generated.
=======
`researcher_history.json` is intended for longitudinal researcher analysis.

Do not replace historical data with only the latest result.
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
=======
`researcher_history.json` is intended for longitudinal researcher analysis.

Do not replace historical data with only the latest result.
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e

## Persistent State

Treat these files carefully:

    seen_works.json
    researcher_history.json

They affect future runs.

Do NOT automatically delete, reset, or recreate them during refactoring.

<<<<<<< HEAD
<<<<<<< HEAD
`seen_works.json` records **qualifying** papers that were successfully written to the authoritative structured output. Papers below the score threshold, excluded papers, and topic-only papers without a required keyword are not added. Papers omitted from the shortened Issue because of display limits are still recorded, so Issue length never determines scientific state.

The state is written atomically only after all reports, structured outputs, and
the run manifest succeed. A corrupt `seen_works.json` aborts the run before any
OpenAlex request rather than silently resetting the file. If OpenAlex requests
completely fail, persistent state is not updated and a failure manifest is
written. A state-write failure leaves the generated reports in place, marks the
manifest partial, and exits non-zero.

Use a dry run to inspect a full week without writing state:

    python generate_brief.py --dry-run
    python generate_brief.py --dry-run --with-radar

Dry-run output goes to `.dryrun` unless `--output-dir` is supplied. `--ignore-seen` manually re-evaluates already processed works without changing the normal production behaviour.

Researcher-discovery failure does not prevent the main weekly literature report from being generated.
=======
If OpenAlex requests completely fail, persistent state should not be updated as though the run succeeded.

Researcher-discovery failure should not prevent the main weekly literature report from being generated.
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
=======
If OpenAlex requests completely fail, persistent state should not be updated as though the run succeeded.

Researcher-discovery failure should not prevent the main weekly literature report from being generated.
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e

## OpenAlex

OpenAlex is the primary bibliographic data source.

Optional credentials are provided through:

    OPENALEX_EMAIL
    OPENALEX_API_KEY

Do not hard-code credentials.

<<<<<<< HEAD
<<<<<<< HEAD
A normal weekly run uses:

- one author query per configured tracked researcher;
- one topic search per configured query;
- researcher-radar enrichment only for the shortlisted candidates.

With the default configuration the observed Phase 3 dry run used 52 requests
(23 seed, 4 topic, 25 radar), about 88 OpenAlex credits, and $0.0088 reported by
the provider. The Phase 3 validation run itself was partial because two topic
queries were throttled anonymously; it used 52 requests, 68 credits, and
$0.0068, and `run_manifest.json` recorded `status: partial`. The run logs the
observed request count, route breakdown, retries, failures, and the
provider-reported cost when the API returns it. The soft budget is 100 requests
per weekly run; a warning is emitted when it is exceeded.

Topic search is rate-limited more aggressively without an API key. If a topic query is throttled, the run records a warning and continues with the seed route rather than failing the whole brief. A partial run is safe for state, but it is not equivalent to a complete weekly sweep; configure `OPENALEX_API_KEY` for reliable unattended runs.

## Tests

Offline tests live in `test_generate_brief.py` and
`test_researcher_discovery.py`:

    python -m unittest discover -s . -p "test_*.py" -v

They cover config validation, seed/topic request construction, deduplication,
relevance and qualification, state round-trips and corruption handling, empty
results, API failure, total-failure and partial-run manifests, Markdown/JSON/CSV
output, the workflow/Issue file choice and test ordering, radar dry-run state
protection, multi-route researcher evidence, one-paper WATCH handling,
outside-network/reappearing/emerging classification, week and consecutive-run
history, legacy history compatibility, evidence deduplication and route
preservation, atomic history writes, and concise Issue researcher output.
=======
Avoid unnecessary API requests. Prefer filtering candidates before requesting additional researcher information.
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
=======
Avoid unnecessary API requests. Prefer filtering candidates before requesting additional researcher information.
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e

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

<<<<<<< HEAD
<<<<<<< HEAD
## GitHub Issue Length

The previously documented mismatch is fixed. `generate_brief.py` writes both a complete `weekly_brief.md` and a concise `issue_brief.md`; the workflow creates the Issue from `issue_brief.md`.

The Issue is limited by selection and by `reporting.issue_max_chars`, not by increasing GitHub's body limit. Full paper evidence remains in `weekly_papers.json` and `weekly_papers.csv`, which are committed and uploaded as artifacts.
=======
=======
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
## Known Issue to Check

`generate_brief.py` generates `issue_brief.md` for GitHub Issue length handling, but the current GitHub Actions workflow uses `weekly_brief.md` when creating the GitHub Issue.

Check the current implementation before modifying the report-length/truncation system.
<<<<<<< HEAD
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e
=======
>>>>>>> 4403d8d9ec7e1d9011d34982554f7e6af900630e

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