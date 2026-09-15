# Signal Scanner

## What this is
A list build and scoring pipeline for outbound. It discovers a universe of local businesses,
enriches them from public APIs, filters on email deliverability, scores them against a
configurable ICP model, and exports a campaign ready CSV for a sequencer.

Demo ICP: independent dental practices in Phoenix, for a client selling an AI phone
receptionist that answers missed calls and books appointments.

## Design rules
- The ICP model lives in `config/icp.yaml` and never in code. Scoring logic reads the config.
  If a weight or a signal needs changing, that happens in YAML only.
- Every pipeline step writes its output to `data/<step>.json` and reads the previous step's
  file. Each step must be runnable on its own.
- Cache every API response to disk on first success. Re-runs should hit cache, not the network.
- One failing account never kills a run. Catch, log, skip, continue.
- Every request gets a timeout, a real user agent, and a delay between calls.
- No API keys in the repo. `.env` only, with `.env.example` committed.

## Pipeline
1. `discover.py`    Overpass API, dental practices in a bounding box. Name, address, website, phone.
2. `dso_check.py`   National NPPES footprint per chain name. Flags `dso_owned` from thresholds, not a hardcoded list.
3. `enrich_web.py`  Fetch homepage plus careers and contact pages. Extract signals.
4. `enrich_npi.py`  Match each practice to the NPPES NPI Registry. Provider count, specialty, enumeration date.
5. `verify.py`      MX lookup per domain. Filter role addresses, flag free mail.
6. `score.py`       Apply `icp.yaml`. Rank. Record which signals fired per account.
7. `brief.py`       Short opening angle per qualified account.
8. `export.py`      CSV with sequencer ready column names and personalisation variables.

`dso_check.py` runs at chain grain, not practice grain: it checks each distinct
chain name once and writes verdicts keyed by name. It only needs `discover.json`,
so it runs second, before the enrichment chain that depends on its verdicts.

`enrich_web.py` builds the addressable audience once, applying
`filters.require_website` and dropping DSO owned practices. Steps 4 and 5 inherit
that audience by reading the previous step's file rather than re-deriving it.

`verify.py` does not attempt catch-all detection. Establishing it requires SMTP
probing of third party mail servers, which is intrusive and gets sending IPs
blocklisted, so `catch_all` is recorded as null.

`run.py` chains them and accepts `--city`, `--limit` and `--from-step`.

## Data sources
- **Overpass API**: free, no key. Be conservative with bounding boxes and set a timeout in the query.
- **NPPES NPI Registry**: free, no key. Query by city, state and taxonomy. Results are paged.
- **DNS**: `dnspython` for MX records.
- **Anthropic API**: optional, for `brief.py` only. Falls back to a template if no key is set.

## The hard part
Overpass and NPPES describe the same businesses with no shared identifier. Matching is fuzzy
on name and address together, using `rapidfuzz`, with a confidence floor. Below the floor,
leave the NPI fields blank. Never guess a match.

## Stack
Python 3.11. `requests`, `beautifulsoup4`, `pyyaml`, `pandas`, `dnspython`, `rapidfuzz`.
No web framework, no ORM, no async. Plain scripts, readable over clever.

## Working with me on this
- Propose a plan before writing a new module, and wait for me to confirm it.
- Ask before adding a dependency.
- Do not edit `config/icp.yaml`. The ICP model is mine.
- Keep functions small and name them after what they do in GTM terms, not generic terms.