# Signal Scanner

A list build and scoring pipeline for outbound. It discovers a universe of local
businesses from public data, enriches them from free APIs, filters on email
deliverability, scores them against a configurable ICP model, and exports a
campaign ready CSV for a sequencer.

Eight steps, each a standalone script that reads the previous step's file and
writes its own. Every API response is cached on first success, so a second run
costs nothing and hits no network.

## The ICP it was built for

The demo targets **independent dental practices in Phoenix**, for a client
selling an AI phone receptionist that answers missed calls and books
appointments.

That product only pays for itself where the phone is genuinely the bottleneck,
which is what every signal is chosen to detect. The model lives in
[`config/icp.yaml`](config/icp.yaml) and never in code.

| Signal | Weight | Why it predicts a buyer |
|---|---|---|
| `hiring_reception` | **+4** | They are already spending money on this problem, in headcount. The strongest buying signal available, because it is a budget that already exists. |
| `no_online_booking` | **+3** | Booking runs through the phone, which is precisely what the product replaces. With online booking, most of the value is already captured. |
| `three_plus_providers` | **+3** | More chairs means more inbound calls, so each missed one costs more. Scales the value of the product. |
| `accepting_new_patients` | **+2** | They are actively capturing demand, so a missed call is lost revenue rather than a full book. |
| `multiple_locations` | **+2** | Front desk load is spread thin across sites. |
| `dso_owned` | **−4** | A dental service organisation has already centralised call handling. The problem is solved and the buyer is not local. |
| `solo_provider` | **−3** | Call volume is too low for the product to pay back. |

A practice qualifies at **4** or above. `require_website` is a hard filter
rather than a weight: the channel is email, so no domain means there is nothing
to send to — an unreachable account, not a weak one.

**DSO detection generalises.** Rather than a hardcoded list of chains, each
distinct practice name is measured against the national NPPES registry: how many
locations it runs and across how many states. Aspen Dental shows 158 locations
in 33 states, Western Dental 200+ in 9. Independent practices show 0 to 3. The
thresholds are config, and a curated list exists only as a backstop for known
exceptions.

## Running it

```bash
pip install requests beautifulsoup4 pyyaml pandas dnspython rapidfuzz
python run.py
```

That is the whole thing. `run.py` chains all eight steps and writes
`data/export.csv`.

```bash
python run.py --city phoenix --limit 25   # smaller run
python run.py --from-step score           # resume, reusing what is on disk
python run.py --refresh                   # ignore caches and refetch
```

A cold run takes about six minutes, almost all of it the 187 NPPES chain lookups
and 42 site fetches. A warm run takes **70 seconds**, of which 69 is retrying
the sites that are genuinely unreachable — failures are deliberately not cached.

No API keys are required. `brief.py` will use Claude if `ANTHROPIC_API_KEY` is
set and the `anthropic` package is installed, and falls back to templates
otherwise. See [`.env.example`](.env.example).

## The pipeline

| # | Step | Does | Writes |
|---|---|---|---|
| 1 | `discover.py` | Overpass API, dentists in a bounding box | `data/discover.json` |
| 2 | `dso_check.py` | National NPPES footprint per chain name | `data/dso_check.json` |
| 3 | `enrich_web.py` | Fetch homepage, careers, contact, locations | `data/enrich_web.json` |
| 4 | `enrich_npi.py` | Fuzzy match to NPPES, count providers | `data/enrich_npi.json` |
| 5 | `verify.py` | MX per domain, sift role from personal | `data/verify.json` |
| 6 | `score.py` | Apply `icp.yaml`, rank | `data/score.json` |
| 7 | `brief.py` | One opening angle per qualified account | `data/brief.json` |
| 8 | `export.py` | Sequencer ready CSV | `data/export.csv` |

## Results on Phoenix

`run.py` prints this after every step, built from the data files rather than
from anything hardcoded. Each line is a strict subset of the one above it.

```
FUNNEL
  discovered             208  ########################################
  has website             50     -158  ##########
  not DSO owned           42       -8  ########
  deliverable domain      34       -8  #######
  qualified                3      -31  #

COVERAGE  (not cuts, nothing is dropped for these)
  site reachable          32 / 42     76%
  NPI org matched         11 / 42     26%
  provider count known    20 / 42     48%
```

**Four practices clear the score threshold, but only three are reachable.**
`score.py` ranks the whole audience, so a practice can qualify on signals while
having no MX record — Bischoff Family Dentistry scores 6 and is held back by
`export.py`. The funnel intersects each cut with the one above it, so the last
line means "qualified *and* reachable", which is what the campaign actually
gets. Reachability and NPI coverage sit below the funnel because they are not
cuts: nothing is dropped for being unreachable, it just carries null signals.

The export holds 34 rows — every deliverable account, ranked, not only the
qualified ones.

| Rank | Score | Practice | Signals fired |
|---|---|---|---|
| 1 | 7 | Center For Dental Rehabilitation | `three_plus_providers`, `hiring_reception` |
| 2 | 6 | Bischoff Family Dentistry | `three_plus_providers`, `no_online_booking` |
| 3 | 6 | Solomon Pediatric Dental | `three_plus_providers`, `no_online_booking` |
| 4 | 4 | Glendale Gentle Dentistry | `hiring_reception` |
| 5 | 3 | Arcadia Dental Arts | `no_online_booking` |

A sample export is committed at
[`samples/phoenix-dental.csv`](samples/phoenix-dental.csv).

## Known limitations

These are real and worth stating plainly, because they bound what the output is
good for.

- **OSM records a website for only 24% of practices** (50 of 208). This is the
  binding constraint on the whole pipeline. Absence in OpenStreetMap is not
  absence in reality — most of these practices do have a website — but
  `require_website` is a hard filter, so 158 practices are dropped for a data
  gap rather than a business fact. Fixing this would roughly quadruple the
  addressable list and matters more than any other item here.
- **10 of the 42 reachable-in-principle sites could not be fetched.** Three
  domains are genuinely dead; the rest return 403 from bot protection that a
  real browser user agent does not get past. Those practices have null web
  signals and are scored on NPI data alone. They are marked with a
  `signal_coverage` of 0.57 rather than silently treated as signal-free.
- **Only 11 of 34 exported rows carry an email address**, and 10 of those 11 are
  role mailboxes (`info@`, `office@`). Exactly one personal address was found
  across all 42 sites. The list is rankable but not yet sendable at volume;
  address discovery is the gap, not scoring.
- **No catch-all detection.** Establishing whether a domain accepts mail at any
  address requires opening SMTP conversations with third party servers and
  probing addresses known not to exist. That is intrusive and gets sending IPs
  blocklisted, so `catch_all` is recorded as `null` with a reason rather than
  guessed.
- **The bounding box is a rectangle, not a city.** It pulls in Glendale, Peoria,
  Scottsdale and Tempe alongside Phoenix. Querying the OSM administrative
  boundary relation would fix it.
- **Five of the twelve DSO flags are false positives** — generic names like
  "Dental Center" and "All Smiles Dental" that appear in 20 to 30 states as
  unrelated practices sharing a name. `known_independent_names` in `icp.yaml` is
  the intended remedy.

## What I would add next

In the order I would actually do it:

1. **Website resolution.** A Google Places or search-based lookup for the 158
   practices with no website in OSM. Everything downstream is gated on this, and
   it is the difference between a 42 account list and something near 200.
2. **Email discovery.** Pattern inference against verified MX, plus scraping
   team pages for named staff. Without it the list ranks well and sends poorly.
3. **Boundary-accurate discovery.** Swap the bbox for an Overpass `area` query
   on the admin relation, so "Phoenix" means Phoenix.
4. **Chain name reconciliation.** `RISAS DENTAL` and `RISAS DENTAL AND BRACES`
   are one company scoring either side of the threshold. Variants of one chain
   should share a footprint.
5. **A signal freshness date.** A careers page scraped three months ago is not
   evidence that they are hiring today, and `hiring_reception` is the heaviest
   positive weight in the model.

## Notes

`docs/build-log.md` records the decisions behind each step, the bugs found and
fixed, and one discriminator that was tried and rejected with the evidence
against it.

Stack: Python 3.11+, `requests`, `beautifulsoup4`, `pyyaml`, `dnspython`,
`rapidfuzz`. No web framework, no ORM, no async.

Licensed under the MIT Licence. See [LICENSE](LICENSE).
