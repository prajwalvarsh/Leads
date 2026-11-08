# Task 1 — Find Berlin property managers and write them to a Google Sheet

Read `CLAUDE.md` first; its shared rules apply here.

## Goal

Find **as many property management companies (Hausverwaltungen) with an
office in Berlin as possible** and write them to the Google Sheet. Volume
matters, but every row must be a real, active property manager in Berlin,
with no duplicates.

Out of scope for Task 1: KPIs, scoring, contact persons, emails, LinkedIn,
Handelsregister checks, slides, and the n8n API wrapper / Docker / workflow
(built last, `docs/n8n.md`). In scope: every Task 1 stage follows the n8n
stage contract in `CLAUDE.md` from the start.

## Output: Google Sheet tabs

Tab **Companies** — one row per company, sorted by Bezirk, then name:

| Column | Source / rule |
|---|---|
| `company_id` | Stable hash of the dedupe cluster's primary key |
| `company_name` | Legal name from the Impressum (incl. legal form), else best directory name |
| `legal_form` | GmbH, GmbH & Co. KG, UG (haftungsbeschränkt), AG, KG, OHG, GbR, e.K., eG, Einzelunternehmen |
| `street` | From the Impressum |
| `postcode` | From the Impressum; must be a Berlin postcode |
| `bezirk` | One of the 12 Berlin districts, via point-in-polygon |
| `phone` | From the Impressum, E.164 format |
| `website` | Final URL after redirects of the company's own site |
| `impressum_url` | URL the legal data was read from |
| `register_number` | e.g. `Amtsgericht Charlottenburg HRB 123456 B`; empty if none |
| `sources` | `;`-joined: `serper_maps`, `places`, `osm`, `gelbeseiten`, `dasoertliche`, `11880`, `search`, `association` |
| `source_count` | Number of distinct sources |
| `google_place_id` | Google place ID / CID only (`place_ref`, from `serper_maps`; or `places` if enabled) |
| `status` | `confirmed` or `review` |
| `review_reason` | Why a row is `review` |
| `decided_by` | What settled the row: `rules`, `llm` or `agent` |
| `last_checked` | ISO date of the run |

Also:
- **Excluded** — dropped candidates: `name`, `website`, `sources`, `reason`.
- **Not in Berlin** — property managers whose Impressum address is outside Berlin.
- **Sources** — per source: candidates found, unique companies contributed,
  overlap with each other source; plus "© OpenStreetMap contributors".
- **Run log** — per run: timestamp, counts after each stage, paid-API call counts, errors.

## Cost caps (hard counters in code, abort above)

Serper ≤ 1,800 credits per run (free signup credits are 2,500; the rest is kept
for the resolve fallback and the search pilot); credits used are counted and logged.
Tavily ≤ 900 credits per run.
Places (disabled, no billing account): Text Search ≤ 4,500 calls, Place Details ≤ 900 calls.
Investigate agent: ≤ 100 cases, ≤ 8 tool calls per case, ≤ 800 LLM calls per run.
Caps live in `config/berlin.yaml`; reaching one ends the stage with `status=failed` and the counts so far.

## n8n contract for Task 1

Follows the stage contract in `CLAUDE.md`. Task 1 specifics:

- **Stages, in order:** `discover`, `resolve`, `crawl`, `extract`, `classify`, `investigate` (optional, `agent.enabled`), `dedupe`, `geo`, `export`. `all` runs them in order and stops at the first failure; a disabled stage returns `status=skipped`.
- **Source switches:** `sources.<name>.enabled` in config for `serper_maps`, `places` (false), `osm`, `gelbeseiten`, `11880`, `dasoertliche`, `search`, `associations`. The overlap tests and the search pilot are development-time decisions; their outcome is stored as this switch, not decided at run time.
- **Counts each stage reports in `StageResult.counts`:**
  - `discover`: candidates per source, new candidates this run; for `serper_maps` also calls, credits, places returned, unique places, new places per query
  - `resolve`: with website, without website, Place Details calls
  - `crawl`: sites fetched, via httpx / playwright / tavily, failed
  - `extract`: Impressum found, fields filled per field, LLM calls
  - `classify`: kept, dropped, review
  - `investigate`: cases, resolved kept, resolved dropped, unresolved, decisions rejected by validation, tool calls, LLM calls
  - `dedupe`: clusters, merged records
  - `geo`: in Berlin, not in Berlin, not geocoded
  - `export`: rows per tab
- **Incremental mode:** `discover` reruns all enabled sources; only candidates not seen before go through `resolve` → `classify`. Pages are re-crawled only if older than `crawl.max_age_days` (config, default 30). `dedupe`, `geo` and `export` always run on the full data.
- **Change tracking:** each company row stores `first_seen_run` and `last_changed_run`, so a later step can list companies added or changed in a given run.

## Data model (DuckDB)

- `candidate(candidate_id, source, source_ref, name, street, postcode, city, phone, website, lat, lon, raw_category, fetched_at)` — non-Google sources.
- `serper_maps_tmp(ref, name, address, website, phone, lat, lon, type)` — dropped at end of run.
- `places_tmp(place_id, name, formatted_address, website, lat, lon)` — dropped at end of run (Places disabled).
- `place_ref(candidate_id, place_id)` — persistent; holds the Google place ID / CID (Serper ref, or Places ID).
- `page(url, domain, page_type, status, text, fetched_via, fetched_at)` — `page_type` ∈ home, impressum, about, services, contact; `fetched_via` ∈ httpx, playwright, tavily.
- `fact(candidate_id, field, value, evidence_quote, source_url, method)` — `method` ∈ regex, llm, directory, osm.
- `company(company_id, ...sheet columns...)`, `excluded(candidate_id, reason)`, `run_log(run_id, stage, key, value, ts)`.
- `agent_trace(run_id, candidate_id, step, tool, args, result_excerpt, tokens_in, tokens_out, ts)` — one row per agent step.

## Stages

### 1. discover

**Build order** (build, run and print counts for each before starting the next):
1. Serper Maps (`serper_maps`)
2. OpenStreetMap
3. Gelbe Seiten
4. Overlap test for 11880 and Das Örtliche (see Directories)
5. Search pilot, only if time allows (see Search)
6. Associations, if a member list file exists

Google Places is optional and disabled (no billing account); it is not in the build order.

**Serper Maps (`serper_maps`)** — Google Maps results via Serper; no Google billing account needed.
- `POST https://google.serper.dev/maps`, header `X-API-KEY`, JSON body
  `{q, ll: "@<lat>,<lng>,14z", gl: "de", hl: "de", page}`.
- Parser written from one saved real response (`tests/fixtures/serper_maps_sample.json`); never guess field names.
- Grid: points `serper_maps.spacing_km` apart (start at 3 km), kept only if inside Berlin
  (union of `data/geo/bezirke.geojson`). Queries from config: `Hausverwaltung`, `Immobilienverwaltung`.
- Pagination: page 1 up to `serper_maps.max_pages` (3). Stop early when a page returns no
  results or fewer than 3 places not seen before in this run.
- **Pilot first** (development time): 5 points spread across Berlin; report calls, credits,
  places, new places per call. Choose the spacing so the full run stays under the credit cap;
  write the choice and the pilot numbers to the Run log and store the spacing in config.
  If no spacing fits under the cap, stop and ask.
- Keep only results with a Berlin postcode (10115–14199) or coordinates inside Berlin.
- Storage: `candidate` row with source `serper_maps`, `source_ref` = CID/place ID and nothing else
  from Google; all other fields in `serper_maps_tmp` (used by `resolve` to get the website, dropped
  at end of run); `place_ref(candidate_id, ref)`. Sheet column `google_place_id` holds this ref.
- Tests: parser on the saved fixture; every grid point inside Berlin; pagination stop rule;
  `serper_maps_tmp` dropped at the end of a run.

**Google Places Text Search (New)** — *optional, disabled (no billing account).*
`sources.places.enabled: false`. Not built further until billing is available.
- `POST https://places.googleapis.com/v1/places:searchText`
- Headers: `X-Goog-Api-Key`, `X-Goog-FieldMask: places.id,places.displayName,places.formattedAddress,places.location,places.businessStatus,nextPageToken`
- Body: `textQuery`, `languageCode: "de"`, `regionCode: "DE"`, `pageSize: 20`, `locationRestriction.rectangle` (`low`/`high`), `pageToken` for pages 2–3.
- Queries: `Hausverwaltung`, `Immobilienverwaltung`, `WEG-Verwaltung`, `Mietverwaltung`.
- **Adaptive grid:** Berlin bbox (lat 52.3383–52.6755, lon 13.0884–13.7611) split 4×4. If a tile returns 60 results for a query, split it into 4 and re-query; stop below ~300 m tile edge. Log tiles and calls.
- Drop results whose `businessStatus` is not `OPERATIONAL`. Write to `places_tmp`; keep `place_id` in `place_ref`.

**OpenStreetMap (Overpass)** — `https://overpass-api.de/api/interpreter`, one query: `area["name"="Berlin"]["admin_level"="4"]`, then `nwr(area)["office"="property_management"]` plus `nwr(area)["name"~"Hausverwaltung|Immobilienverwaltung|WEG-Verwaltung",i]`; `out center tags;`. Map `addr:*`, `website`, `contact:website`, `phone`.

**Directories** — robots.txt first for each site; if the needed paths are disallowed, skip the site and log it. Inspect the site's listing pages and pagination before writing a parser. Terms: `Hausverwaltung`, `Immobilienverwaltung`, `WEG-Verwaltung`; location: each of the 12 Bezirke. Capture name, street, postcode, city, phone, website, category label, listing URL.
- **Gelbe Seiten:** build the full parser.
- **11880 and Das Örtliche — overlap test first:** fetch one results page for one Bezirk (`Hausverwaltung`), parse it, and match the listings against everything already found (same matching rules as dedupe). Log `listings`, `already found`, `new %` per site in the Run log and Sources tab.
  - new % clearly above ~10% → build the full parser for that site.
  - otherwise → skip it and record the overlap rate as the reason.

**Search pilot (only if time allows)** — use Serper if `SERPER_API_KEY` is set, else Tavily Search.
1. Run 12 queries: `Hausverwaltung Berlin-<Bezirk>`.
2. Keep URLs whose registrable domain looks like a company site (exclusion list in config: directories, portals, social media, news, review sites). Candidate = domain + page title.
3. From portal pages that list many Berlin property managers, extract names and websites (LLM with evidence).
4. Count companies that are new after classify + dedupe.
5. **Stop rule:** if the pilot adds fewer than ~5% new companies, stop and log the result. Otherwise extend to `WEG-Verwaltung` and `Immobilienverwaltung` per Bezirk, and go down to Ortsteil level (list in `config/berlin.yaml`) only for Bezirke whose queries still added new companies.
6. Log queries run, results, new companies per query.

Tavily Extract is reserved for the crawl fallback; prefer Serper for all searches when its key is present.

**Associations** — if `data/manual/associations.csv` exists (name, website, association), load it as source `association`.

### 2. resolve (website per candidate)

Website from the candidate's own source (for `serper_maps`: `serper_maps_tmp.website`) → Place Details (only if Places is enabled) (`GET https://places.googleapis.com/v1/places/{id}`, FieldMask `websiteUri`, only if no other source gave one, within the cap) → search `"<name>" Berlin Impressum`, accept the top result whose domain fuzzy-matches the name (token_set_ratio ≥ 80). Shared hosts (jimdo, wixsite, facebook, instagram, linkedin, google…) are never company domains.

### 3. crawl

- httpx: timeout 15 s, follow redirects, 20 hosts in parallel, 1 req/s per host.
- Impressum link by anchor text/path (`impressum`, `imprint`, `rechtliches`, `anbieterkennzeichnung`); fallback paths `/impressum`, `/impressum/`, `/impressum.html`, `/impressum.php`, `/de/impressum`.
- At most 2 more pages for classification: about/über uns, leistungen/services.
- Text via trafilatura (fallback selectolax). Text < 200 chars or JS-only → Playwright; still failing → Tavily Extract. Record `fetched_via`. Cache by URL hash.

### 4. extract (Impressum → legal data)

The LLM is the primary extractor; regex is an independent cross-check.

- **The LLM** (`models.extract`, default a Claude model) reads every Impressum and returns `company_name, legal_form, street, postcode, city, phone, register_court, register_number`, each `{value, quote}`. Prompt loaded from `prompts/extract_v<N>.md` (version set in config).
- **Regex** extracts postcode + city, phone, register (`HR[AB]\s*\d+\s*[A-Z]?` + court) and legal form from the same text.
- **Evidence check:** a value is valid only if `quote` appears verbatim in the page text (whitespace-normalised) and contains `value`.
- **Merge per field:** both valid and equal → keep (`method=llm+regex`); only one valid → keep it; both valid but different → keep the LLM's value, mark the row `review` with reason "regex disagrees on <field>".
- Write kept values to `fact`; log per-field agreement rate (LLM vs regex) in `StageResult.counts`.

### 5. classify (is it a property manager?)

The LLM classifies every crawled candidate; keyword rules are a cross-check.

- **LLM** (`models.classify`, prompt `prompts/classify_v<N>.md`) reads the name plus home/about/services text and returns `is_property_manager` (yes / no / unclear), `services` (WEG, Miet, SE, Gewerbe, other), `is_housing_company`, and a supporting `quote`. No valid quote → `unclear`.
- **Rules:** keep-words (Hausverwaltung, Immobilienverwaltung, WEG-Verwaltung, Mietverwaltung, Sondereigentumsverwaltung, Grundstücksverwaltung, Property Management) and drop-words (pure Makler, Hausmeister, Gebäudereinigung, Winterdienst, software vendors, law firms, associations).
- **Decision:** LLM and rules agree → apply; they disagree or the LLM says unclear → `review` (goes to `investigate`). Candidates with no crawled website are classified by rules only, `decided_by=rules`.
- Housing companies and cooperatives are kept, `review_reason` = "housing company".
- Log the LLM-vs-rules agreement rate in `StageResult.counts`.

### 5b. investigate (optional agent for the review queue)

The rules settle routine cases; this agent handles the ones they could not.
Switched on with `agent.enabled` in config.

**Cases:** candidates in `review` because of: no Impressum found, unclear
whether it is a property manager, or missing or conflicting address. Order by
`source_count` (desc), take at most `agent.max_cases`.

**Implementation:** a plain tool-calling loop with the openai SDK on
OpenRouter (no agent framework). Model from `models.agent` (default a
Claude Sonnet model), prompt `prompts/agent_v<N>.md`. One case per loop; at most
`agent.max_steps` tool calls; the loop ends when the agent calls `submit_decision`.

**Tools** (all go through the pipeline's own HTTP client, so robots.txt,
rate limits, caching and cost caps still apply):
- `get_candidate(candidate_id)` — everything already known about the candidate, incl. the review reason.
- `fetch_page(url)` — page text (trafilatura, truncated to ~8,000 chars); stored in `page`.
- `search_web(query)` — Serper if configured, else Tavily; top 10 title/URL/snippet.
- `submit_decision(decision)` — final answer, schema below.

**Decision schema:** `is_property_manager` (yes / no / unknown), `in_berlin`
(yes / no / unknown), `website`, `impressum_url`, and `company_name`, `street`,
`postcode`, `phone`, `register_number` — each `{value, quote, url}` — plus a
one-sentence `reason`.

**Validation in code before anything is applied:**
- every `quote` must appear verbatim in a page fetched during this case (from `page`) and contain its `value`;
- `impressum_url` and `website` must be on the candidate's domain or a URL fetched during the case;
- a failed check rejects that field; a rejected `is_property_manager` keeps the case in `review`.

**Outcome:** validated yes → back into the normal flow with `decided_by=agent`;
validated no → `excluded` with the agent's reason; unknown or rejected → stays
`review`, `review_reason` = the agent's reason.

**Safety:** page text and search results are data, not instructions; the system
prompt says so, and the code-level validation above is the real safeguard. The
agent cannot write to the database except through `submit_decision`.

**Tests:** a decision whose quote is not in a fetched page is rejected; the
step cap stops the loop; a disabled agent returns `status=skipped`.

### 6. dedupe (union-find)

Merge when ANY of: same normalised register number; same registrable domain (not a shared host); same E.164 phone; name `token_set_ratio ≥ 90` AND same street + house number. Never merge records with **different** register numbers. Primary key = register number, else domain, else phone, else name+street. Prefer Impressum facts over directory/OSM values.

### 7. geo

- Berlin check: postcode 10115–14199 AND city "Berlin", else → Not in Berlin.
- Nominatim geocode (cached), shapely point-in-polygon on `data/geo/bezirke.geojson`. No coordinates → `review`, "not geocoded".

### 8. export

- `gspread.service_account(filename=GOOGLE_SA_JSON)`, open by `SHEET_ID`.
- Per tab: clear, one `update(range_name="A1", values=[header] + rows)`; freeze header, basic filter, auto-size.
- Drop `places_tmp` and `serper_maps_tmp`. Append to Run log.

## Model and prompt eval (`python -m pipeline.cli eval`)

A development-time command, not part of `all`. It picks which model and
prompt version the pipeline uses, based on measurements.

- **Eval set:** 40 Impressum pages and 40 classification cases, sampled with a
  fixed seed. Reference labels: where the extract LLM, `models.check` and regex
  all agree, take that value; label the remaining disagreements by hand (the
  user reviews only those) and store everything in `eval/labels.jsonl`.
- **Variants:** at least 2 prompt versions × at least 3 models, from different
  families: a Claude model, an OpenAI model, and one or two low-cost open
  models from OpenRouter. All configured in `eval.variants`.
- **Metrics per variant:** accuracy per field, `is_property_manager` accuracy,
  share of values rejected by the evidence check, cost per 100 pages (from
  OpenRouter's usage data), median latency.
- **Output:** `docs/model_eval.md` — one table of variants × metrics, the
  chosen default per role, and the reason in one sentence. Set the winners in
  `models.*` and the prompt versions in config.
- **Rule:** prefer the cheapest variant within 2 percentage points of the
  best accuracy.

## Tests (minimum)

- normalize: phone E.164, registrable domain, name normalisation, register parsing.
- dedupe: domain merge, phone merge, fuzzy name+street merge, different-register no-merge.
- extract: evidence check rejects a quote not in the text; merge rule keeps the LLM value and flags `review` when LLM and regex disagree.
- geo: a known Mitte and a known Spandau address map correctly.

## Definition of done

- `python -m pipeline.cli all --mode full --json` runs end to end from an empty database and prints a valid `StageResult` per stage.
- A second run with `--mode incremental` succeeds, crawls only new candidates, and changes no company IDs.
- Starting a run while another is active fails fast with a clear error.
- No two `confirmed` rows share a register number, domain or phone.
- Every `confirmed` row has `company_name`, Berlin `postcode`, `bezirk`, `impressum_url`, and `phone` or `website`.
- No Google field other than the place ID / CID exists after the run (`places_tmp` and `serper_maps_tmp` dropped).
- Run log has counts per stage and paid-API counts within caps.
- Spot check, done by Claude Code, independent of the pipeline: 20 random `confirmed` rows (fixed, saved seed); Impressum re-fetched fresh (no cache); `company_name`, `street`, `postcode`, `phone`, `register_number` and "is a property manager" re-extracted with `models.check` (must be a different model family from `models.extract`; default an OpenAI model) without seeing the pipeline's values; compared field by field. Results in `docs/spotcheck.md`: mismatches listed first, then the full table (row, field, sheet value, check value, match, evidence quote), accuracy per field, and the fix for each mismatch. The user reviews only the mismatches. If the agent resolved any rows, include at least 5 of them and report their accuracy separately.