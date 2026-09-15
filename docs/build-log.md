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

## Step 4 — `dso_check.py`

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
++
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
