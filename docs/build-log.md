# Build log

Decisions and findings worth remembering, newest last.

---

## Step 1 — `discover.py`

**What it does.** Queries Overpass for `amenity=dentist` and `healthcare=dentist`
in a bounding box, writes `data/discover.json`, caches the raw response under
`data/cache/` keyed on a hash of the query.

**Result on Phoenix:** 208 practices, 17 unnamed elements dropped, 0 duplicates.

### Decisions

- **Two tags, not one.** `amenity=dentist` is dominant but `healthcare=dentist`
  catches records it misses. Overpass dedupes co-tagged elements within a union.
- **`nwr`, not `node`.** Many practices are tagged on the building way rather
  than a POI node. `out center` collapses those to one coordinate.
- **Fields beyond the original four.** `osm_id` was added as the stable join key
  for later steps, so nothing downstream has to re-match on fuzzy strings.
  `lat`/`lon` were added because `multiple_locations` is far more reliable to
  detect by clustering coordinates than by parsing address text. `operator` and
  `brand` were added as the cheapest available input to `dso_owned`.
- **Phone is stored verbatim.** Discovery is the raw data layer. Normalisation
  to E.164 belongs in `export.py`, where the sequencer actually needs it.
- **Bounding boxes live in `config/cities.yaml`,** not in code and not in
  `icp.yaml` — a bbox is geography, not ICP.

### Findings

- **The bbox is not Phoenix proper.** Of 127 addressed records: 61 Phoenix,
  27 Glendale, 15 Peoria, 8 Scottsdale, 5 Tempe. A rectangle cannot trace
  Phoenix's real boundary. Post-filtering on `addr:city` would not fix it, since
  81 records have no address at all and would all be discarded. The fix, if we
  want it, is an Overpass `area` query on the admin boundary relation instead of
  a bbox. **Left as-is for now.**
- **Only 24% have a website** (50 of 208); 61% have an address. This is the
  biggest open risk in the pipeline: `no_website` is a −3 exclusion, but absence
  in OSM is not absence in reality. As things stand `score.py` would penalise
  three quarters of the list for an OSM gap rather than a business fact. Likely
  needs a website-resolution step between 1 and 2.
- **Dedupe bug caught before the live run.** A practice tagged on both a POI node
  and its building way keyed twice, because the node carried `addr:unit=210` and
  the way did not. Suite suffixes are now stripped from the dedupe key.
  Practices with no street address key on `osm_id` instead, so two same-named
  chain locations are never silently merged.
- Overpass returned a 504 on the first live attempt; the retry succeeded.

---

## Step 2 — `dso_check.py`

**What it does.** Takes each distinct chain name in `data/discover.json`, asks
NPPES how many locations it runs nationally and across how many states, and
compares that footprint to the floors in `icp.yaml`. Writes `data/dso_check.json`.

**Why it exists.** `operator`/`brand` alone is not a DSO signal — the same tags
hold solo dentists' own names ("Nafys Samandari, DDS", "Dr. Chris Murphy"). The
question "is this a chain?" is answerable nationally, so it generalises to any
market instead of relying on a list of names we happen to know.

### Findings that shaped the design

- **NPPES matches `organization_name` against the "Doing Business As" field, not
  just the legal name.** This is the whole reason the check works. DSOs register
  every practice as its own legal entity — `32 DENTAL, LLC`, `A & Z DENTAL, PC` —
  sharing one DBA. The DBA is the chain fingerprint.
- **Exact match badly undercounts.** `WESTERN DENTAL` returns 6 locations because
  the real entity is `WESTERN DENTAL SERVICES, INC.`. Without a trailing
  wildcard, the largest DSO in the Phoenix data reads as independent — a false
  negative on the heaviest negative weight in the model.
- **Punctuation is a hard API error, not an empty result.** `BRIGHT NOW! DENTAL`
  returns `Field contains special character(s)` and misattributes it to a
  `last_name` field, so the error text is no help. Names are sanitised before
  querying.
- **`result_count` is the page count, not a total.** NPPES clamps `limit` to 200.
  Hitting 200 means "at least 200", which is why `truncated` is recorded. Any
  floor above 200 can never trip, and the module warns if one is set.

### Design

- **Wildcard to widen recall, verification to restore precision.** `WESTERN
  DENTAL*` also returns prefix collisions like `EMILY LEE DDS PC`; only records
  whose legal name or a DBA actually starts with the normalised core are counted.
- **Prefix verification is a filter, not proof.** It still admits longer names
  that merely share a prefix, so an unrelated "Western Dental Clinic" counts
  towards Western Dental's footprint. This inflates generic names ("FAMILY
  DENTAL", "SMILE CENTER") more than distinctive ones, and is the main reason
  `known_independent_names` exists as a backstop.
- **Chain name falls back to the practice name.** Only 13 of 208 records carry
  `operator` or `brand`, so keying on those alone would leave 94% of the market
  never checked. Distinct names to check: **187**, so 187 calls on a cold cache
  (~3 min at a 1s delay), 0 on every run after.
- **Thresholds live in `icp.yaml` under `dso_detection`** and are the ICP owner's
  to set. The module refuses to run without them and prints the block to add.
  Curated lists are overrides only, never the primary path.
- **DNS failure is reported as DNS failure.** The `cms.hhs.gov` zone SERVFAILs on
  some networks while everything else resolves, so a generic connection error
  would send you hunting in the wrong place.

### Observed separation (floors of 25 locations / 3 states)

| Chain | Locations | States | Verdict |
|---|---|---|---|
| Aspen Dental | 158 | 33 | DSO |
| Western Dental | ≥200 (capped) | 9 | DSO |
| Dental Depot | 57 | 9 | DSO |
| A Reason to Smile | 3 | 2 | independent |
| Nafys Samandari, DDS | 0 | 0 | independent |

### Open

- Credentials written with periods normalise imperfectly: `Dr. Gregory A. Libby,
  D.D.S.` becomes `DR GREGORY A LIBBY D D S`. Harmless, since it returns 0
  matches either way, but untidy.

---

## First full `dso_check` run

Floors set to 25 locations / 3 states, `require_both: true`. **187 chains
checked, 12 flagged `dso_owned`, 0 failed lookups**, ~3 minutes cold, free after.

### What worked

Seven of the twelve are real DSO brands, found with no hardcoded list — which
was the point of the exercise:

| Chain | Locations | States | In market |
|---|---|---|---|
| Gentle Dental | ≥200 (capped) | 32 | 1 |
| Western Dental | ≥200 (capped) | 9 | 4 |
| Aspen Dental | 158 | 33 | 6 |
| Bright Now Dental | 133 | 11 | 4 |
| Perfect Teeth | 75 | 3 | 1 |
| Dental Depot | 57 | 9 | 4 |
| Risas Dental | 25 | 4 | 2 |

`require_both` also did real work: `ARIZONA DENTAL` has 93 locations but all in
one state, and was correctly not flagged.

### What did not
**Five false positives, all generic names appearing once locally:**
`DENTAL CENTER` (100/30), `ALL SMILES DENTAL` (54/24), `GATEWAY DENTAL` (48/21),
`SOUTHERN SMILES` (40/12), `DIAMOND DENTAL` (37/20). These are unrelated
practices sharing a common name across 20-30 states, not chains. This is the
prefix-inflation the code comment warns about, showing up exactly as predicted.

**A discriminator was tried and rejected.** Hypothesis: real DSOs should show
many legal entities sharing one identical DBA, unrelated practices should not.
The ratios do not separate — Aspen 75%, but Bright Now 16%, Dental Depot 7%,
Risas 0%, against suspects running 6-26%. Complete overlap. **Do not revisit
this idea without new evidence.** Local market count correlates (real DSOs often
have 4-6 local sites, the suspects have 1) but breaks on Gentle Dental and
Perfect Teeth, both genuine DSOs with a single Phoenix location.

Resolution: twelve names is small enough to eyeball, and `known_independent_names`
is precisely the backstop for this. Curation beats another threshold here.

**Name variants can straddle a floor.** `RISAS DENTAL` (25/4) flags DSO while
`RISAS DENTAL AND BRACES` (23/4) does not. Same company, opposite verdicts,
either side of the floor of 25. Nothing currently reconciles variants of one
chain into a single footprint.

### Funnel impact

`filters.require_website
: true` is a far harder cut than the -3 weight it
replaced:

```
discovered                     208
after require_website           50   (-158)
after dso_owned exclusion       42    (-8)
```

**42 of 208 practices, 20%, reach scoring.** The reasoning holds — no domain
means nothing to send to — but most of the 158 dropped are practices where *OSM*
lacks a website, not ones without a website in reality. A website-resolution
step would widen this far more than going metro-wide would.

---

## Steps 3, 4 and 5 — `enrich_web`, `enrich_npi`, `verify`

Built together against the 42 practice audience (has a website, not DSO owned).

**Ordering fix.** `dso_check` moved from step 4 to step 2. It only reads
`discover.json`, and leaving it at 4 meant `enrich_web` at step 2 needed the
output of step 4 to build its audience — a backwards dependency. The audience is
now computed once, in `enrich_web`, and inherited downstream by reading the
previous step's file.

### Results

```
discovered                    208
audience (website, not DSO)    42
site reachable                 32   (10 unreachable)
NPI organisation matched       11
provider count known           20
deliverable (has MX)           33
```

| Signal | True | Unknown |
|---|---|---|
| `no_online_booking` | 9 | 10 |
| `hiring_reception` | 3 | 10 |
| `accepting_new_patients` | 4 | 10 |
| `multiple_locations` | 1 | 10 |
| `three_plus_providers` | 5 | 22 |
| `solo_provider` | 6 | 22 |

The 10 unknowns on web signals are the unreachable sites. The 22 on NPI signals
are practices with no postcode or no organisation above the confidence floor.

### Bug found and fixed: postcode extraction

`postcode_of` matched any five digit group, which grabbed the **street house
number** on addresses that carry no postcode. "11851 N 28th Dr" became postcode
11851, and ten of the supposed twenty-eight postcodes were East Coast zips that
no Phoenix practice is in. The regex now requires a preceding state code and an
end-of-string anchor. Real figures: **35 of 42 have a postcode, across 22
distinct Arizona zips.** The fix recovered genuine matches that had been
querying empty postcodes, including Valley Orthodontic Group at confidence 95.
Cache files for the bogus zips were deleted.

### Decisions

- **NPI queries are keyed by postcode, not city.** Phoenix has 2000+ dentists
  and city-wide paging runs past `skip=1800` without exhausting. A postcode
  returns a small, complete set.
- **Provider count uses address agreement alone,** not name similarity. An
  individual dentist is enumerated under their own name, so requiring the
  practice name to match would reject every provider at a practice not named
  after a person. This is why 20 practices have a provider count while only 11
  have a matched organisation.
- **Booking detection records evidence, not just a boolean.** A scheduling
  vendor (NexHealth, Weave, LocalMed and similar) or explicit "book online"
  language counts as online booking. A "request an appointment" form does not —
  the phone still does the work, which is exactly what the product replaces.
  `booking_evidence` carries the vendor, the phrase, and whether only a request
  form was found, so `score.py` can weigh them differently.
- **Catch-all detection is deliberately not attempted.** It requires SMTP
  probing of third party mail servers with addresses known not to exist, which
  is intrusive and gets sending IPs blocklisted. `catch_all` is null with a
  stated reason rather than guessed. The CLAUDE.md pipeline description was
  updated to match.

### Open

- **10 of 42 sites are unreachable** (24%): three domains are dead
  (`r2smile.com`, `savon-dental.com`, `westcactusdental.com` — NXDOMAIN or no
  nameservers), the rest are 403s from bot protection. A full browser user agent
  does not help; these block by IP reputation. Those 10 have null web signals.
- **Only 1 personal email address was found across all 42 sites**, against 10
  role addresses. Practice sites overwhelmingly publish `info@` and `office@`.
  Whatever `export.py` does for personalisation cannot rely on a named contact.
- **`book.kidtasticdental.com`** shows that domain extraction does not reduce to
  the registrable domain. MX lives on the apex, so a booking subdomain in OSM
  yields the wrong lookup. One case here, worth fixing before scale.
- Five practices have no postcode and two have no address at all, so their NPI
  fields stay blank by design.

---

## Steps 6, 7 and 8 — `score`, `brief`, `export`, and first full run

### Domain extraction fixed

`verify` now reduces a hostname to its registrable apex before the MX lookup.
`book.kidtasticdental.com` was being queried directly; mail is addressed at the
apex, so the lookup asked a name never meant to answer it. Split into
`host_from_website` and `registrable_domain`, with a small two-label public
suffix table rather than a Public Suffix List dependency. Both `site_host` and
`domain` are kept so the reduction stays auditable. This recovered one account:
deliverable went from 33 to 34.

### Decisions

- **Unknown is not false.** `score.py` credits a signal only when it is exactly
  `True`. Enrichment writes null when it could not establish a fact, which for
  ten practices means the site was unreachable. Scoring null as an absence would
  punish practices for our coverage gaps rather than their behaviour. Each row
  carries `signal_coverage` so a rank built on fewer facts is visible, and ties
  break on coverage before alphabetically.
- **`brief.py` uses templates by default and needs no key.** The Anthropic SDK
  is imported lazily inside the Claude path, so the pipeline runs on the
  declared stack without it. `.env.example` is committed; enabling Claude briefs
  needs `pip install anthropic` as well as a key. Templates only ever state
  signals that actually fired, so an unreachable site cannot produce an invented
  claim about a practice.
- **`export.py` holds back undeliverable rows by default.** A row the sequencer
  cannot send to is worse than no row, because the bounce costs domain
  reputation. `--include-undeliverable` overrides it.

### Bug: a step that dropped its own audience

`brief.py` first wrote only the practices it briefed, so `export.py`, which
reads the previous step's file, saw 3 practices instead of 42 and exported 2
rows. Every step must carry the full set forward and mark rows rather than drop
them. Fixed: all 42 are written, unbriefed ones with `brief_source: "not
briefed"`. Export went from 2 rows to 34.

### First full pipeline run

```
discovered (Overpass)             208
chains checked for DSO            187   (12 flagged)
audience: website + not DSO        42
  sites reachable                  32
  NPI organisation matched         11
  deliverable (has MX)             34
qualified (score >= 6)              3
exported rows                      34   (11 with an email)
```

Top of the ranking:

| Rank | Score | Coverage | Practice | Signals |
|---|---|---|---|---|
| 1 | 7 | 0.86 | Center For Dental Rehabilitation | `three_plus_providers`, `hiring_reception` |
| 2 | 6 | 0.86 | Bischoff Family Dentistry | `three_plus_providers`, `no_online_booking` |
| 3 | 6 | 0.86 | Solomon Pediatric Dental | `three_plus_providers`, `no_online_booking` |
| 4 | 4 | 0.57 | Glendale Gentle Dentistry | `hiring_reception` |
| 5 | 3 | 0.86 | Arcadia Dental Arts | `no_online_booking` |

### Open

- **22 of 42 practices score zero**, and three score -3 on `solo_provider`. The
  model is working; the inputs are thin. Nothing above 7 out of a possible 14.
- **Only 11 of 34 exported rows have an email address**, and none of the three
  qualified accounts do. The list is rankable but not yet sendable. Practice
  sites publish `info@` and `office@` or nothing, so the gap is address
  discovery, not scoring.
- **`hiring_reception` fired 3 times and is worth 4 points**, the heaviest
  positive weight. It depends entirely on reaching a careers page, and ten sites
  were unreachable. This signal is the most sensitive to fetch coverage.
- `run.py` is still not built. Steps run individually in pipeline order.

---

## Close out — `qualified_at: 4`, `run.py`, README

**Threshold dropped to 4.** Qualified accounts went from 3 to 4; Glendale Gentle
Dentistry joins on `hiring_reception` alone. Everything else in the funnel is
unchanged, since `qualified_at` only gates briefing, not export.

**`run.py`** chains all eight steps with `--city`, `--limit`, `--from-step` and
`--refresh`. Each step declares which flags it accepts, so an unsupported flag
is never passed down rather than being rejected by the child's argparse. A
failing step stops the run: later steps read the file an earlier one writes, so
continuing would score a stale or partial list.

Warm full run: **70 seconds**, of which 69 is `enrich_web` retrying the ten
genuinely unreachable sites. Failures are deliberately not cached, per the
caching rule — only successes are. That is the right trade, but it means the
unreachable set is paid for on every run.

**README** covers the ICP and why each signal was chosen, two-command setup, the
funnel with real numbers, the six known limitations, and a ranked list of what
to add next. Sample export committed at `samples/phoenix-dental.csv`.

### Still open at close out

- `market` in `icp.yaml` reads "Phoenix metro, AZ" while `cities.yaml` queries
  the city-proper bbox, and `known_independent_names` is still empty, so the
  five known false positives are still flagged as DSO owned.
- Website resolution remains the highest-value next step by a wide margin: 158
  of 208 practices are dropped for an OSM gap rather than a business fact.

---

## Live funnel in `run.py`

Redrawn after every step from the data files on disk, nothing hardcoded. Stages
that have no data yet print `-` and fill in as the run proceeds.

**The funnel is built by intersection, not by independent counts.** Each stage
is cut from the one above it, which surfaced a real discrepancy: `qualified` is
not a subset of `deliverable`. `score.py` ranks the whole audience, so Bischoff
Family Dentistry scores 6 and qualifies while having no MX record, and
`export.py` holds it back. Counted independently the funnel would end on 4
qualified; intersected it ends on **3 qualified and reachable**, which is what
the campaign actually gets. Counting stages independently would have hidden
that, which is the argument for intersecting even where subsets look obvious.

Reachability and NPI match coverage are shown below the funnel, not in it.
Nothing is dropped for being unreachable or unmatched — the practice stays in
the list carrying null signals — so they are coverage, not cuts.
