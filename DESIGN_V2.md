# munger — Design v2.2

Supersedes `DESIGN.md` for everything it covers, and supersedes the
earlier "Design v2" and "Design v2.1" docs. Written after four weeks of
paper trading produced a trade journal that falsified several of v1's
core assumptions.

v1 was a design written from philosophy. v2 is a design written from
evidence. Where they conflict, this document wins.

**v2.2** folds every §4 reviewer recommendation into the design body and
the milestone plan, and resolves the two questions v2.1 left open:
sizing stays equal-weight with `MAX_SINGLE_POSITION_WEIGHT` deleted, and
the margin-decline flag moves out of the optional tranche into M39.

**Revision note (2026-09-02):** a second, independent review round —
staff-engineer-reviewer, pm-reviewer, and warren-buffett, each reading
this document fresh rather than deferring to §4's own embedded reviews
— found and this revision fixed: staleness against `main` (M24 status,
the `ruff format` count); the settlement pass's "idempotent and
blocking" language specified into actual partial-fill/query-failure/
escalation rules (§3.3); the `state.json` migration's lossy-translation
policy stated explicitly (§3.1, M35); the "evaluate touches broker: No"
self-contradiction (§3.1); a named data source for corporate-action
detection (§3.2) and for Tier-2 event retrieval (§3.8), closing two
previously-unconfirmed external dependencies; numeric thresholds added
where a gate was named without one (XBRL disagreement tolerance §3.4,
filing-agent precision/recall bar §3.5); and Tier-2's structural-threat
flags routed to a logged human decision instead of auto-halving
position weight (§3.5) — the one place a model's judgment was taking a
fully automated portfolio action. Not resolved in this pass, by design
— handed to a dedicated pm-reviewer planning pass instead, since they're
scope and sequencing questions, not content fixes.

**Planning pass (2026-09-02, same day):** the pm-reviewer split M26 into
five independently-shippable pieces (M26a–e) and M29 into three
(M29a–c), each sized against this document's own M44–M47 granularity.
It also found and fixed a real internal inconsistency: §3.7 gates the
restart on §3.1–3.3 landing, but the milestone table sequenced the
restart (Epic B) in Tranche 1, *ahead of* Epic C (§3.1) — contradicting
§3.7's own stated gate. The fix moves Epic D (data integrity) to run in
parallel with Epic A rather than after it (the two epics share no
modules), and moves the restart itself to a new Tranche 3, gated on
Epic A + Epic C + Epic D all complete — so the paper record takes one
clean discontinuity, not two. The tranche count went from three to four
as a result (Tranche 4 is the same optional qualitative layer previously
labeled Tranche 3). The six-month estimate was also re-derived from this
project's own demonstrated velocity rather than re-asserted — see
"Revised time estimate" at the end of §5.

## 1. What Production Actually Showed

Between 2026-07-27 and 2026-08-10 the system placed 20 buys and 6 sells
across 15 trading days, against a README that states most runs should
place zero sell orders. The journal is the primary evidence for
everything in this document.

**Round trips that should not have happened:**

| Ticker | Bought | Sold | Re-bought | Elapsed |
|---|---|---|---|---|
| NMIH | Jul 27 | Aug 1 (strikes=2) | Aug 3, score 79.2 | 7 days |
| SIG | Jul 28 | Jul 31 | Aug 4, then sold again Aug 9 | two round trips in 12 days |
| G | Jul 27 | Aug 1 | Aug 4 | 8 days |

A system that liquidates a position as a broken thesis and re-buys it 48
hours later at a higher score is not detecting deterioration. It is
trading on data jitter. This is the Munger inaction principle inverted,
and it happened repeatedly.

**A liquidation that never occurred.** On Aug 8 the journal recorded FOX
and LPG as sold. On Aug 9 the log reported FOX: broker reports a
position but journal doesn't expect one, and the same for LPG. The DAY
limit orders at −2% never filled. `process_sells` had already reset the
strike counters on the assumption the position closed. Journal, P&L
page, and broker have been divergent since. `state.json` still carries
`{"FOX": 1, "LPG": 1}` — positions the system believes it sold,
re-struck as holdings.

**A journal that misreports itself.** `bot.py` hardcodes
`NEW_POSITION score=` on every buy, so six top-ups (HIG ×2, ASO, LPG,
HRMY ×2) are recorded as new positions. Any position count or entry
basis derived from the journal is wrong.

**A top pick the screen could not evaluate.** HRMY scored 82.6, the
highest on the board, and the position was built to roughly $6.7k. Its
10-K states the company is substantially dependent on a single product,
WAKIX, which is licensed from Bioprojet rather than owned, with
royalties payable. In February 2026 a Paragraph IV bench trial against
AET Pharma concluded with post-trial judicial comments unfavorable
enough to trigger analyst downgrades and a ~23% one-month decline. The
low trailing P/E the screen rewarded was *produced by* that risk. The
screen read the consequence of the danger as evidence of value.

**Metrics that measure nothing.** NMIH reported an operating margin
(76.7%) exceeding its gross margin (76.4%). It is a mortgage insurer
with no cost of goods sold; yfinance manufactured the number. HIG and
TROW have the same problem, as does current ratio for all three. They
passed gates that never actually tested them.

## 2. Root Causes

The individual bugs are symptoms. Seven structural causes produced them,
and v2 is organized around eliminating those rather than patching the
symptoms. RC1–RC5 explain the bugs; RC6 and RC7 explain why the screen
picked what it picked.

**RC1 — One loop, one cadence, three jobs.** Screening, sell evaluation,
and buy execution all run on a single daily loop. Strikes are counted in
runs, so "two consecutive quality failures" became two calendar days.
`REBALANCE_DRIFT_BAND_PCT` exists only to suppress the buy-side churn
the same coupling causes. Every timing bug traces here.

**RC2 — Three distinct states collapsed into one.** "No data," "bad
data," and "bad business" were all treated as a quality failure. A
delisting, a symbol change, an acquisition close, and a yfinance timeout
are indistinguishable at the call site, and all of them struck holdings
toward liquidation.

**RC3 — Open-loop execution.** Orders are submitted and immediately
journaled as though they succeeded. Nothing confirms fills. State
transitions on intent, not outcome. FOX and LPG are the proof.

**RC4 — Measurement validity never established.** yfinance is the sole
source, its numbers were never reconciled against filings, and the
gates apply one set of ratios to every sector regardless of whether
those ratios mean anything for that sector.

**RC5 — No falsification criteria.** No benchmark, no horizon, no
pre-registered definition of failure. Without these, every outcome can
be rationalized after the fact, and four weeks of broken-process paper
trading can be mistaken for validation.

**RC6 — No durability measure.** Every quality term in the composite is
a point-in-time level: ROE *today*, gross margin *today*. Nothing
measures whether high returns have persisted, which is the only
observable evidence that a moat exists. This is why HRMY (20.5% ROE, one
licensed product, ~6 years of operating history) scored 82.4 while GNTX
(two decades of returns, 79% global share, 980 US patents) scored 66.5.
The composite ranked the fragile business above the durable one. For a
system named after Munger, whose entire contribution over Graham is
durability, this is the most consequential omission in v1.

**RC7 — Blind between filings.** The only inputs are periodic
fundamentals. Nothing watches for discrete material events, and nothing
at all watches for events that happen *to* a holding rather than being
disclosed *by* it. HRMY's adverse Paragraph IV ruling, Takeda's
competing orexin agonist, and the camera-monitor regulations threatening
GNTX are all invisible to a design that reads only balance sheets and
annual reports.

## 3. Revised Architecture

### 3.1 Decouple the cadences (addresses RC1)

Three independent schedules, each doing one job:

| Job | Cadence | Touches broker | Rationale |
|---|---|---|---|
| **Screen** | Daily | No | Cheap, informative, feeds the site. Nothing about it needs to be slow. |
| **Evaluate holdings** | Quarterly, ~3 weeks after quarter-end | Read-only | Fetches current holdings live (this system's own long-standing rule — holdings are never read from local state) to know what to evaluate, and checks corporate-action status per §3.2. Never places an order. Fundamentals change quarterly; evaluating them daily manufactures noise. Timed so fresh 10-Q data has propagated. |
| **Execute** | Monthly | Write | Buys and sells batch here. Bounded, reviewable, infrequent. |

**"Read-only" vs. "Write" above is a real distinction, not a rounding error
(staff-engineer-reviewer finding: the original "No" for Evaluate
contradicted itself — you cannot evaluate holdings without first knowing
what's held).** Evaluate calls the broker's position-list endpoint and
nothing else; Execute is the only cadence that can submit an order. Both
still get their own workflow and credentials per the isolation rule
below — "read-only" is not an exemption from that.

**Strikes become time-based, not run-based.** A strike is recorded
against a *fiscal period*, not a run. Two strikes means two consecutive
quarters of failed quality — the design's original intent, now literally
true regardless of how often anything runs. `state.json` stores
`{"version": 2, "strikes": {ticker: [list of period identifiers
struck]}}` — versioned per M35 — rather than a bare integer counter, so
the meaning survives any future cadence change and a future format
change has somewhere to record itself.

This deletes the need for `REBALANCE_DRIFT_BAND_PCT` as a noise
suppressor. It may remain as a transaction-cost floor, but it is no
longer load-bearing.

**Each cadence is its own workflow, with its own account credentials.**
Three schedules means three GitHub Actions workflows, and M20 established
that separate workflows are the only paper/live isolation mechanism that
holds by construction rather than by convention. Every new workflow
inherits that requirement; no shared local state between them.

**Stated policy — inaction is visible and is not a bug.** Because the
screen runs daily but holdings are evaluated quarterly, the site will
show a holding as failing quality for up to three months before the
system acts on it. That gap is the discipline working, not a lag to be
fixed. The site labels it explicitly on the holding's row, so neither
you nor a reader mistakes deliberate patience for a broken pipeline. Any
future change that closes this gap is a change to the philosophy and
should be argued as one.

### 3.2 A real state machine for holdings (addresses RC2)

Every holding is in exactly one state per evaluation, and the states are
not collapsible:

- **HEALTHY** — data retrieved, quality floors passed. Reset strikes.
- **DETERIORATING** — data retrieved, quality floors failed. Record a
  strike for this period.
- **UNREADABLE** — no data. **No strike, no reset.** Raise an alert for
  human review. An unreadable check is evidence of nothing.
- **CORPORATE_ACTION** — detected delisting, symbol change, merger, or
  spin-off. Never auto-traded. Always a human decision.

CORPORATE_ACTION gets active detection rather than being inferred from
absence: check the broker's own position record and the exchange listing
status before concluding a ticker has vanished. Concretely, both checks
reuse infrastructure the system already has rather than adding a new
external dependency (pm-reviewer finding: this needed a named source,
not just "the exchange"): Alpaca's Assets API
(`GET /v2/assets/{symbol}`, already reachable from `execution.py`)
carries a `status`/`tradable` field per symbol, cross-checked against
the broker's own position record already required above. If Alpaca
reports the asset inactive or untradable while a position or a strike
history still references it, that's CORPORATE_ACTION, not UNREADABLE.
Absence of data must never again be a trading signal.

### 3.3 Closed-loop execution (addresses RC3)

The journal splits into two tables with different meanings:

- **orders** — what was *submitted*: symbol, side, notional or qty,
  limit price, `client_order_id`, timestamp, reason.
- **fills** — what actually *happened*: fill price, filled qty, status,
  settlement timestamp.

A separate **settlement pass** runs after each execution window, queries
order status by `client_order_id`, and writes the fills rows. Three
rules follow:

1. **State transitions only on confirmed fills.** Strike counters reset
   when a liquidation *fills*, not when it is submitted.
2. **Unfilled orders are surfaced, not forgotten.** A DAY limit that
   expires unfilled raises an alert and is re-decided in the next
   window, never silently retried.
3. **Reconciliation is authoritative.** A mismatch between broker
   positions and the fills table halts execution until resolved. It is
   already the check that caught this bug; it should have teeth.

**Settlement is idempotent and blocking — specified, not just named
(staff-engineer-reviewer finding: "idempotent and blocking" was a policy
statement, not a spec; it named the right property without saying what
to do in the two cases that actually occur).**

1. **A query that fails is not the same as an order that is genuinely
   still pending, and the settlement pass must be able to tell them
   apart.** A broker-unreachable/timeout/5xx response on the status
   query is a *query failure* — retry with backoff, and if retries are
   exhausted, treat this run's settlement as incomplete (case 3 below).
   A successful query that reports the order still `open`/`pending_new`
   is a *genuinely pending* order — not an error, just not yet resolved;
   leave it for the next settlement pass, no retry needed.
2. **Partial fills are a third outcome, not folded into "confirmed" or
   "unfilled."** A `partially_filled` status writes a fills row for the
   filled quantity (real shares, real cost basis — this must be
   journaled, not discarded) and leaves the order's remaining quantity
   in the *genuinely pending* bucket above until it fills, expires, or
   is canceled. Strike-reset and cost-basis logic key off filled
   quantity, never off "was this client_order_id in the fills table at
   all" — a 10%-filled liquidation is not a confirmed exit.
3. **An incomplete settlement pass (query failures exhausted retries)
   blocks the next execution window, reusing this system's existing
   kill-switch mechanism rather than inventing new blocking behavior**
   (staff-engineer-reviewer finding) — on exhausted retries, settlement
   sets the same per-account `KILL_SWITCH_FLAG_FILE_PATH` execute would
   otherwise check, so "blocked" is the same screen-only state an
   operator can already recognize and clear by hand, not a new code
   path to learn. Failing closed here is cheap: the cost is a delayed
   monthly execution, and the alternative is trading on a position
   picture known to be wrong.
4. **A block that outlives one cycle is itself an alert, not silence.**
   If the kill-switch flag settlement set is still present when the
   *next* execution window would otherwise run, that raises a Critical
   alert on its own (distinct from the routine "screen-only, kill switch
   active" log line) — a settlement stuck for a full monthly cycle means
   a human hasn't noticed yet, which under a monthly cadence is a
   materially longer silent gap than this system tolerates today.
5. **Re-running settlement against the same `client_order_id` set is
   always safe.** Writing a fills row is `INSERT ... WHERE NOT EXISTS`
   (or the schema's equivalent uniqueness constraint) on
   `client_order_id`, so a settlement pass re-run after a partial
   failure re-checks every order in the set but only ever writes each
   fill once.

Also fixed here: the journal reason string is derived from the actual
decision (`NEW_POSITION`, `TOP_UP`, `SELL_QUALITY`,
`SELL_CORPORATE_ACTION`), never hardcoded.

Order type is reconsidered too. A −2% DAY limit on a $2B-cap name is thin
protection *and* a meaningful fill risk, as demonstrated. Marketable
limits with a wider band, plus an average-daily-volume ceiling so no
order exceeds a small fraction of ADV.

### 3.4 Data integrity (addresses RC4)

**XBRL companyfacts becomes the primary source.** SEC's own tagged data,
straight from filings, free, point-in-time, authoritative. yfinance
drops to a fallback for fields XBRL doesn't cover, and any field where
the two disagree beyond a tolerance is flagged rather than silently
preferred. This alone resolves the GNTX operating-margin discrepancy
(yfinance 21.8% vs. the filed 18.7% — an 3.1-point gap, above the
tolerance below).

**Disagreement tolerance, stated as a number (staff-engineer-reviewer
and pm-reviewer findings: this was named as a gate with no threshold
attached).** A field disagrees if it differs from the XBRL-derived value
by more than the larger of 5% relative or 1 percentage point absolute
(the latter matters for ratio/margin fields already near zero, where a
5%-relative bar is too loose to catch a real problem). This is a
starting default, not a number derived from data that doesn't exist
yet — M36's own shadow cycle is what calibrates it for real, and this
figure is what that cycle validates or revises, not a fixed constant.

**EDGAR itself needs an empirical volume/rate-limit check before M36
is built on top of it, not assumed (pm-reviewer finding, matching this
project's own D0-gate precedent in `DESIGN_DISTRIBUTED.md` — confirm an
external constraint before building the machinery that depends on it).**
Concretely: confirm the `companyfacts`/`submissions` endpoints sustain a
full-universe daily fetch (~1,500 tickers) plus a one-time 10-year
backfill for the moat-persistence component (§3.6) within SEC's stated
fair-access limit (10 requests/second, `User-Agent` identification
required) in a reasonable window. If they don't, scope XBRL to the
buyable/near-buyable subset rather than the full screening universe —
still closes RC4 for every ticker whose numbers actually decide a trade,
at a fraction of the request volume.

**Sector-aware gates.** Financials, REITs, and insurers do not have
meaningful gross margins or current ratios. Two options, decided per
sector rather than globally: exclude the sector, or give it its own gate
set (for insurers: combined ratio, book value per share growth, reserve
development). Applying manufacturing ratios to an insurer and calling
the result a pass is worse than not screening it at all.

**Trend terms, computed deterministically.** From multi-year XBRL data:
margin direction over three years, organic versus acquired revenue
growth, revenue per share. GNTX's headline 9.6% growth was the VOXX
acquisition; core sales *declined* 2% and operating margin fell for the
third straight year. A snapshot screen cannot see this. A trend term
can, without any model.

**Average earnings, not trailing.** Graham wanted ten years of earnings
precisely because trailing P/E flatters cyclicals at peak margins. EOG,
MGY, INSW, and LPG all entered on trailing multiples at what look like
cycle highs. Compute the P/E gate against average net income across
available years.

**The source swap runs in shadow first.** Both sources run in parallel
for a full cycle, disagreements are logged per field, and the report is
inspected by hand before XBRL becomes authoritative. A silent source
swap on a system with persisted state produces a discontinuity nobody
can explain six months later.

**When the buyable list shrinks below target, hold cash.**
Average-earnings P/E plus sector-aware gates plus the moat floor will
materially reduce the candidate pool, quite possibly below the
15-position target. The response is to hold cash and run fewer positions
— never to relax a threshold. This is written down here specifically
because the pressure will arrive later, in the moment, framed as a small
reasonable adjustment, and that is exactly how a value screen quietly
becomes a momentum screen. Threshold changes require a dated entry
explaining the reasoning, not a config edit.

### 3.5 Qualitative risk layer (addresses the HRMY class)

Per the filing-agent addendum, with its core principle unchanged: **the
model extracts, the code decides**, and the signal is **negative-only**
— it may veto or flag, never boost.

An LLM asserting a business looks wonderful restates what the numeric
screen already saw. An LLM surfacing an active ANDA proceeding
contributes information found nowhere else in the pipeline. Asymmetric
value, so asymmetric authority.

Extraction is confined to checkable particulars with a verbatim source
span and Item number, verified by string match against the parsed
section before acceptance. Tier-1 disqualifiers (single product >50% of
revenue with active patent litigation at trial stage or later; core IP
licensed rather than owned with single-product dependence; material
weakness in ICFR; going concern) remove a candidate outright.

**Tier-2 flags split into two classes by consequence, not treated
uniformly (pm-reviewer and warren-buffett findings, independently
converging on the same gap: "flag and halve" was too soft for a
disclosed structural threat, and the document's own "veto or flag, never
boost" framing understated what actually happens once a flag halves a
live position's weight automatically).**

- **Routine flags** — customer concentration, acquired-growth
  dependence, three-year margin decline — halve the maximum position
  weight automatically, exactly as before. These are the kind of
  disclosure most companies carry some version of; treating each one as
  a stop-and-decide event would make the layer useless through alert
  fatigue, the same failure mode §3.8 names for the event monitor.
- **Structural-threat flags** — named regulatory obsolescence risk (the
  GNTX class: a disclosed, dated, specific threat to the company's core
  product category or revenue mechanism, not a generic risk-factor
  boilerplate line) — do **not** auto-halve. They hold the candidate at
  its *last* good decision (existing holding: unchanged weight, no new
  top-up; not-yet-held candidate: not opened) and raise a named alert
  requiring a **logged human decision** — hold at full weight, hold at
  half weight, or exclude — before the position's weight can change in
  either direction. This is the layer 3 judgment §3.6 and §6 already say
  "remains yours"; the mechanism now actually routes to a human instead
  of quietly resolving itself in code. The decision and its reasoning
  are journaled as a manual override (§3.7), so it's counted alongside
  every other override, not a silent exception to that metric.

Applied retroactively: HRMY trips Tier 1 twice. GNTX trips two routine
Tier-2 flags (customer concentration, acquired-growth dependence — both
auto-halve) and one structural-threat flag (the mirror-replacement
regulation) — which holds the position at its last decision and forces
the logged human call, rather than the design silently deciding "half
weight" on your behalf for the one risk in the document it explicitly
says it cannot judge.

**Extraction quality is measured before it influences anything, against
a stated bar (pm-reviewer finding: "measured" named a gate with no pass
threshold).** A labeled golden set of roughly 30 filings, with precision
and recall computed in CI, gates this layer before it influences any
order: **≥90% precision on Tier-1 disqualifiers, ≥80% recall.** The bars
are asymmetric on purpose — this is a negative-only, veto-capable
signal, so a false positive silently removes a real candidate from the
buy queue with no other layer to catch it, while a false negative is
more often recoverable (a missed disqualifier likely also fails a
quantitative gate, or turns up in a later filing cycle). Without this
gate a model upgrade silently changes portfolio behavior and the first
symptom is a P&L page nobody can account for. The rule layer gets unit
tests; the model gets a benchmark.

**Moat mechanism (layer 2 of §3.6)** is extracted here as a display-only
classified field — brand, switching costs, network effects, scale,
regulatory, or none-stated — with its Item 1 source span. Display-only is
deliberate: the mechanism a company *claims* is self-reported and
unfalsifiable from the filing alone, so it informs your reading without
moving the score. The score reads persistence, which is observed rather
than asserted.

### 3.6 Portfolio construction and the moat component

**Moat evidence becomes the largest block in the score (addresses RC6).**

A moat is not a vibe. It is economic profit that competitors fail to
compete away, and its existence is therefore *measurable as
persistence*. Three layers are worth separating, because only the third
resists automation:

1. **Evidence a moat exists** — returns that stayed high for a long
   time. Fully measurable.
2. **Mechanism** — brand, switching costs, network effects, scale,
   regulatory capture. Stated plainly in Item 1 of most 10-Ks;
   extractable by the §3.5 agent as a classified field with a source
   span.
3. **Durability against a specific future threat** — does not automate,
   and stays a human judgment.

v1 measured none of these. It measured current profitability *level*
and called it quality. Three deterministic components fix layer 1, all
computable from XBRL:

| Component | Definition | Full credit at |
|---|---|---|
| ROIC persistence | Share of the last 10 fiscal years with ROIC above a 10% hurdle (a fixed WACC proxy — deliberately not a per-company estimate, which would add false precision) | 10 of 10 years |
| Margin stability | Inverse coefficient of variation of gross margin across those years — pricing power that survives a downturn | CV ≤ 0.10 |
| Revenue per diluted share, CAGR | Catches both genuine per-owner growth and buyback-flattered headline growth | 8% |

XBRL companyfacts reaches back to roughly 2009 for most filers, so a
ten-year window is achievable. yfinance's four years never was — another
reason §3.4's migration gates this work.

**Insufficient history scores zero, not excluded.** A company with six
years of filings cannot demonstrate ten-year persistence, so it receives
no credit for durability it has not yet shown. This is the same
conservatism as "no data, no buy," and it is the term that would have
ranked HRMY correctly.

**Revised weights:**

| Term | v1 | v2 | Note |
|---|---|---|---|
| ROIC persistence | — | 0.25 | moat evidence |
| Margin stability | — | 0.10 | moat evidence |
| Revenue/share CAGR | — | 0.05 | moat evidence |
| Return on equity (level) | 0.30 | 0.10 | demoted; see below |
| Gross margin (level) | 0.20 | 0.10 | |
| Operating margin (level) | 0.15 | 0.05 | |
| FCF yield | 0.20 | 0.20 | unchanged |
| Low debt | 0.15 | 0.15 | unchanged |

Durability 0.40, current profitability level 0.25, cash generation 0.20,
balance sheet 0.15.

ROE falls from the heaviest term to a minor one for two reasons. It is
levered, so shrinking equity raises it mechanically — GNTX retired about
6% of its shares last year. And a single year's ROE says nothing about
whether the return survives. ROIC persistence subsumes what ROE was
meant to proxy for and does it better. Where ROE is retained, it is
cross-checked against ROIC so the score distinguishes earning returns
from returning capital.

**Acceptance test for this change:** on the same data, GNTX must
outrank HRMY. If it does not, the component is not doing its job.

**Two biases accepted deliberately, and disclosed on the site.**

The 10% ROIC hurdle is a blunt WACC proxy. It will favor asset-light
businesses and penalize capital-intensive ones whose true cost of
capital is lower. That is a real distortion, not a rounding error, and
it is accepted as the price of avoiding per-company WACC estimation —
which would substitute false precision for honest bluntness. The
methodology page says so.

The ten-year window structurally excludes every company younger than ten
years, including genuinely durable young businesses. That is chosen, not
accidental: a company that cannot demonstrate persistence does not get
credit for it. Recording the choice here matters because otherwise it
reads like an oversight, and someone — possibly you, in a year — will
"fix" it without realizing it was the point.

**What this still does not fix.** GNTX's own 10-K discloses that
regulators in Europe, Japan, Korea, and China now permit camera monitor
systems to replace mirrors outright. No historical series prices a
regime change, and no amount of ROIC persistence sees it coming. That is
layer 3, and it remains a human judgment — see §6.

**Sector cap.** No more than 25% of portfolio value in one GICS sector.
Nothing currently prevents concentration emerging as a side effect of
whichever sector is at peak margins — the Aug 10 buys added CF and MGY
alongside existing EOG and INSW, which is four commodity-cyclical
positions arrived at by accident rather than choice. Munger endorsed
*chosen* concentration. When the cap binds, take the highest-scoring
name in the sector and skip the remainder.

**Sizing stays equal-weight; the dead cap is removed. (Decision, not an
open question.)** `min(portfolio_value/15, portfolio_value*0.12)` never
binds, since equal weight is 6.67% — so `MAX_SINGLE_POSITION_WEIGHT` is
code that reads like a risk control and is not one. Two options existed:
make sizing score-weighted so the cap becomes load-bearing, or delete the
cap.

Delete it. Score-weighted sizing would let position size depend on a
composite that §3.7 concedes has never been shown to predict anything,
compounding an unproven assumption with real money behind it. Equal
weight is the honest default until the validation framework produces
evidence otherwise, and it is one fewer place for a scoring error to
become a concentration error. Revisit only if §3.7 yields evidence the
score ranks well — and revisit explicitly, as a dated decision.

The `MAX_SINGLE_POSITION_WEIGHT` constant is deleted rather than left
inert, because a dormant risk control is worse than none: it invites the
belief that something is being enforced.

### 3.7 Validation framework (addresses RC5)

This section is new in v2 and is the most important one.

**The paper record to date is not a validation dataset.** It was
generated by a process broken in at least three independent ways.
Judging the strategy from it means judging it by the output of a system
that wasn't running it.

**Therefore: reset the paper account and restart clean** once §3.1–3.4
land — cadence decoupling, closed-loop execution, the holding state
machine, *and* data integrity (pm-reviewer planning finding: an earlier
milestone plan sequenced the restart ahead of the data-integrity work,
contradicting this gate as originally stated; §5's Tranche 3 now reflects
the corrected gate). Restarting before the data source itself is fixed
would give the "clean" record a second major discontinuity almost
immediately once XBRL lands — the same risk §3.4's own shadow-mode
requirement exists to prevent, one level up.

**Pre-register, before restarting, in a committed file:**

- **Benchmark.** Equal-weight S&P Composite 1500, or a value ETF. Chosen
  now, not after results exist.
- **Horizon.** Minimum three years before any performance judgment.
  Value strategies underperform for long stretches; a shorter horizon
  guarantees reacting to noise.
- **Process metrics, reviewed quarterly and meaningful immediately** —
  unlike returns, these are measurable now: turnover (target under
  20%/yr), sells per year (target near zero), fill rate, reconciliation
  mismatch count, Tier-1 veto rate, quote-verification failure rate,
  alerts per holding per quarter (§3.8), and **manual override rate**.
  The override rate deserves particular attention. Journaling every
  human deviation makes "how often do I overrule my own system" a
  counted number rather than a matter of recollection. A high rate does
  not mean you were wrong; it means the rules are, and that is only
  learnable if the overrides are recorded at the time.
- **Kill criteria.** Written in advance: what result makes you stop.
  Without this, every outcome gets rationalized.

**Process conformance is the near-term test, not returns.** Whether the
system follows its own rules is answerable in weeks. Whether the rules
make money is not answerable for years. v1 conflated these; v2 does not.

### 3.8 Material-event monitoring (addresses RC7)

The filing agent in §3.5 reads annual reports. Annual reports are stale
by up to twelve months and describe only what a company discloses about
itself. The HRMY development that mattered most — a Paragraph IV bench
trial concluding with unfavorable judicial comments — appeared in
neither.

**The distinction this layer turns on: events, not sentiment.**

Sentiment is largely price in disguise. "Analysts are bearish,"
"coverage has turned negative," "the stock fell 23%" are downstream of
price movement. A system whose founding principle is that price
movements are never signals cannot consume them through a side door
without becoming a momentum system wearing a value system's clothes —
and the drift would be gradual enough to go unnoticed.

The HRMY trial conclusion was not sentiment. It was a discrete, dated,
verifiable fact. The downgrades were the reaction; the ruling was the
event. This layer monitors events and explicitly discards moods.

**Three tiers, descending in signal quality:**

**Tier 1 — 8-K filings (deterministic, no model).** An 8-K is the
SEC-mandated notification that something material happened. Structured,
free, and available within days. Poll EDGAR for each holding and
classify by item number:

| Item | Meaning | Severity |
|---|---|---|
| 4.02 | Non-reliance on previously issued financials | Critical |
| 4.01 | Auditor change | High |
| 1.03 | Bankruptcy or receivership | Critical |
| 5.02 | Departure of principal officers | Medium |
| 2.01 / 2.05 / 2.06 | Asset disposition, exit costs, material impairment | Medium |
| 2.02 | Results of operations | Low — routine, usually suppressed |

Fifteen holdings means fifteen polls. No LLM, no hallucination surface,
no judgment. This is the highest value-per-unit-complexity item in the
entire v2 design — it belongs in Tranche 1 (Epic E), alongside the other
work that has no dependency on the cadence/data-integrity epics landing
first, not waiting behind them.

**Tier 2 — Third-party events (model-assisted, extraction only).** The
genuine gap: things that happen *to* a company that it does not file. A
court ruling against it. A competitor's trial readout. A regulation
permitting substitutes for its product. Applied to the current holdings,
this tier is what would have surfaced the AET Pharma ruling, Takeda's
competing orexin agonist, and the camera-monitor rule changes.

The extraction target is an **event record**, never a score:

```json
{
  "ticker": "HRMY",
  "event_type": "litigation_ruling",
  "date": "2026-02-20",
  "description": "Paragraph IV bench trial concluded; adverse post-trial commentary",
  "source_url": "...",
  "affects_thesis": "exclusivity_horizon"
}
```

The taxonomy is a **closed enumeration in code, with a test asserting
that an unmatched record is discarded** — not an instruction in a
prompt. This distinction is load-bearing. Every practical implementation
of "monitor the news" drifts toward sentiment, because sentiment is
abundant and genuine events are rare; a prompt instruction erodes under
that pressure and a type check does not.

Classification runs against that **fixed taxonomy** — litigation ruling,
regulatory change, competitor approval, patent or exclusivity
development, management departure, accounting irregularity, credit event
— rather than asking a model what seems important. Models exhibit strong
recency bias and will render everything urgent if asked an open
question. An event with no date, no source URL, or no taxonomy match is
discarded, not downgraded.

**Tier 3 — Everything else.** Filtered out, not summarized. Price
commentary, analyst rating changes, and general market narrative are
explicitly excluded. If a rating change is the only evidence, there is
no event.

**The hard constraint: alert-only, never trade-triggering.**

The §3.5 filing agent is negative-only — it may veto. This layer is one
step weaker still: it may only *inform*. No event record ever produces
an order, adjusts a score, or records a strike. The correct response to
"the system cannot see X" is not "make the system trade on X"; it is
"make the system tell me about X so I can decide." That preserves
inaction as the default while closing the blind spot, and it keeps a
fast-moving, model-mediated input structurally incapable of driving
turnover.

An event alert routes to Discord and to the ticker's page on the site.
Acting on it is a human decision, recorded in the journal as a manual
override with a reason, so overrides are auditable and countable
alongside the automated decisions.

**Alert fatigue is the failure mode to design against.** An alert
channel that fires weekly gets ignored, which is worse than not having
one, because it produces false confidence that something is watching.
Rate-limit per holding, require a dated event with a working source
link, suppress Tier-1 Item 2.02 by default, and track alerts-per-quarter
as a process metric under §3.7. If the rate climbs, the taxonomy is too
loose.

**Most of the plumbing already exists, including retrieval — named
explicitly, not left open (pm-reviewer finding: §4.3 calls Epic G
"genuinely open-ended," but M48/M49 read as if the retrieval mechanism
were already settled; it should be, and it already can be).**
`news_update.py` runs on a schedule, retrieves via Alpaca's News API
(`NewsClient.get_news()`, already integrated and already paginating
correctly at this system's ticker volume — confirmed against the
`alpaca-py` source, not assumed, per this project's own M22 review),
calls the Anthropic API to extract, posts to Discord, and has a
soft-fail posture that cannot break the job. This is largely a retarget:
from a general performance digest to per-holding event extraction
against §3.8's closed taxonomy, with a tighter cadence and the
alert-only rule enforced in code rather than by convention — not a new
data source to source and validate.

## 4. Review

Every recommendation below is incorporated into §3 and §5 rather than
left outstanding; the reviews are retained as the reasoning behind those
choices. Two open questions were resolved against the reviewers'
arguments: sizing stays equal-weight with the dead position cap deleted
(§3.6), and the margin-decline flag moved from the optional tranche into
M39.

### 4.1 Staff engineer review

*Concurs with the architecture. Findings on the plan itself:*

The settlement pass in §3.3 introduces a second failure mode the design
should name: settlement runs, the broker is unreachable, and now neither
orders nor fills reflects reality. Settlement must be idempotent and
re-runnable, and a settlement pass that cannot complete must block the
next execution window rather than letting it proceed on stale state.

Migrating `state.json` from integer counters to period lists is a
breaking change against persisted production state on the `bot-state`
branch. It needs a real migration with a schema version, not a format
that happens to parse. The repo has been bitten by this before — the
M20a journal account column required an explicit
`PRAGMA table_info`-gated `ALTER TABLE` because
`CREATE TABLE IF NOT EXISTS` was a no-op against the live database.

Three cadences means three workflows, which means three more places for
the paper/live isolation to leak. The M20 finding — that separate
workflows are the only isolation mechanism that holds by construction —
applies to each new one.

The XBRL migration needs a shadow period: run both sources, log
disagreements, and inspect them before switching over. A silent source
swap on a system with persisted state is how you get a discontinuity you
can't explain six months later.

Extraction quality has no measurement. The rule layer gets unit tests;
the model does not. A labeled golden set of ~30 filings with
precision/recall in CI is required before the filing agent influences
any order, or a model upgrade will change portfolio behavior and you
will find out from the P&L page.

Finally: `ruff format --check` reports files needing reformatting on
main while `ruff check` is green — **3 files as of the M24 merge**
(`report.py`, `tests/test_config.py`, `tests/test_report.py`; this
number was 5 when first observed and has since drifted down as other
work touched two of those files incidentally — re-verify against `main`
before M30 rather than trusting either historical figure). That gap
suggests CI runs one and not the other.

*Disposition:* settlement idempotence and the blocking rule are in §3.3;
the versioned migration is M35, tested against real `bot-state` data
rather than a fixture; per-workflow isolation is in §3.1 and M34; the
shadow period is §3.4 and M36; the golden-set gate is §3.5 and M45; CI
formatting is M30.

### 4.2 Portfolio construction review

*Concurs. Additional findings:*

The quarterly evaluation cadence in §3.1 creates an information-timing
question the design should answer explicitly. If sells are evaluated
quarterly but the screen runs daily, the site will display a holding as
failing quality for up to three months before the system acts. That is
defensible — it is the whole point of patience — but it should be a
stated policy rather than an emergent surprise, and the site should
label it so a reader doesn't mistake inaction for a bug.

§3.4's average-earnings change will materially shrink the buyable list,
possibly below the 15-position target. The design should say what
happens then: hold cash, reduce position count, or relax a gate. Holding
cash is the correct answer and should be explicit, because the
alternative is threshold-loosening under pressure, which is how a value
screen quietly becomes a momentum screen.

The sector cap needs a tie-break rule. When the cap binds, does the
system take the highest-scoring name in the sector and skip the rest, or
distribute? Unspecified means arbitrary.

Score-weighted sizing (§3.6) deserves scepticism. The composite has
never been validated as predictive; weighting position size by an
unvalidated score compounds an unproven assumption. Equal weight is the
honest default until §3.7 produces evidence otherwise.

*On the new moat component:* the direction is right and it is the
single most defensible change in v2 — ROIC persistence is close to how
Morningstar has assigned economic-moat ratings for two decades, and it
is the term that would have ranked HRMY and GNTX correctly. Three
cautions.

First, a fixed 10% ROIC hurdle is a blunt WACC proxy that will
systematically favor asset-light businesses and penalize
capital-intensive ones whose genuine cost of capital is lower. That is a
real bias, not a rounding error; accept it consciously as the price of
avoiding per-company WACC estimation, and say so on the site.

Second, a ten-year persistence window structurally excludes every
company younger than ten years. Mostly that is the point. But it also
means the screen can never buy a genuinely durable young business, which
is a permanent, deliberate blind spot rather than a bug — worth stating
so it does not get "fixed" later by someone who forgot it was chosen.

Third, persistence is backward-looking by construction, and this design
is now weighting it at 0.40. A business whose moat broke last year still
scores well for years afterward. The §3.5 Tier-2 flags — three-year
margin decline in particular — are the counterweight, which means the
qualitative layer is no longer as optional as §4.3 treats it. Consider
promoting the margin-trend flag out of Tranche 3, since it is computable
from XBRL without any model.

*On §3.8:* the events-not-sentiment line is the single most important
sentence in that section and it will be under constant pressure. Every
practical implementation of "monitor the news" drifts toward sentiment,
because sentiment is abundant and events are rare. The taxonomy is the
defense, and it should be a closed enumeration in code with a test
asserting that an unmatched record is discarded — not a prompt
instruction, which will erode.

The manual-override journaling in M43 is doing more work than it
appears to. It makes human deviations countable, which means "how often
do I overrule my own system" becomes a measurable process metric rather
than a matter of recollection. If that number is high, the system's
rules are wrong, and you will only learn that if the overrides are
recorded.

*Disposition:* the timing policy is stated and labelled on the site
(§3.1); the hold-cash rule is explicit (§3.4); the sector tie-break is
specified (§3.6); score-weighted sizing is rejected and
`MAX_SINGLE_POSITION_WEIGHT` deleted (§3.6); both moat biases are
disclosed on the methodology page (§3.6); the closed taxonomy is
enforced by a type check with a test (§3.8); override and alert rates
are process metrics (§3.7); and the margin-decline flag is promoted into
M39.

### 4.3 Project manager review

*Scope concern.* v2 as written is roughly six months of part-time work.
The correctness fixes are urgent and small; the data migration and
filing agent are large and are not. Sequencing them together risks the
urgent work waiting on the interesting work.

*Recommendation:* three tranches, gated. Tranche 1 is correctness only
and should land before the paper account restarts. Tranche 2 is the
cadence and data work that makes results trustworthy. Tranche 3 is the
qualitative layer, which is genuinely optional — the system is
defensible without it as long as it is honest about what it cannot see.

*Dependency to flag:* §3.7's clean restart gates everything downstream.
Every day the paper account runs on the current broken process generates
data that cannot be used. Restart early rather than late.

*Risk:* the repo's own history shows five milestones where "done"
required a user visual confirmation that lagged the code by days or
weeks. Budget for that explicitly rather than treating it as overhead.

*On §3.8:* splitting this across two tranches is right, and the split
is not arbitrary. Epic E needs no model, touches fifteen tickers, and
reuses existing Discord plumbing — it is days of work for the
highest-severity coverage in the document. Epic G is genuinely
open-ended and should be treated as such. If Epic G never ships, Epic E
still closes the accounting-irregularity and auditor-change blind spots
entirely, which is most of the downside protection at a fraction of the
cost.

*Sequencing note:* alert-only enforcement should land with the 8-K
poller, not after it. An alert channel built without that constraint
enforced in code will acquire trade-triggering behavior the first time
an event feels urgent enough, and that is precisely the drift §3.8
exists to prevent.

*Disposition:* merged into a single milestone, M42 — the poller cannot
ship without the enforcement test. The confirmation-lag budget is
stated in the §5 preamble.

## 5. Epics and Milestones

Continuing the repo's numbering from M23. Reviewer recommendations from
§4 are incorporated here and in §3 — the sequencing below reflects them
rather than deferring them. A dedicated pm-reviewer planning pass
(2026-09-02) further split M26 and M29 into independently-shippable
pieces and resequenced Epic D — see the notes inline below for the
reasoning.

**Budget for confirmation lag.** The repo's history shows five
milestones where "done" required a visual confirmation that trailed the
code by days or weeks. That lag is normal for a solo project with a
public site, and it is planned for rather than treated as overhead: a
milestone is not closed until its exit criterion is observed, not merely
implemented.

**Budget for structural calendar floors, separately from confirmation
lag.** Several exit criteria below cannot close faster than a real
calendar interval this design itself introduces — the quarterly Evaluate
cadence (§3.1), the shadow-mode cycle before the XBRL switchover (§3.4),
and, largest of all, M33's own bar of "hand-verified... for one full
quarter" of restarted paper data. These are not engineering effort and
should not be estimated as if more hours would close them faster. See
the revised time estimate at the end of this section.

### Tranche 1 — Correctness and data integrity (parallel streams)

Epic A and Epic D touch disjoint modules — Epic A is `portfolio.py`/
`execution.py`/`journal.py`/`bot.py`; Epic D is `screener.py`/`data.py`.
Neither blocks the other technically, so they are worked as two parallel
streams rather than sequenced. The one coordination point: land Epic D's
M41 (sizing/sector-cap changes to `portfolio.py`) after Epic A's M24
(already done) and M26c (state-transition wiring, also in `portfolio.py`)
to avoid rebasing sizing logic against gate logic mid-flight — a merge
convenience, not a functional dependency. Both streams gate the paper
restart in Tranche 3.

**Epic A — Stop trading against the rules.**

| ID | Milestone | Exit criteria |
|---|---|---|
| M24 | Land the `fix/live-trading-safety` branch | **Done** — merged to `main` before this branch was cut (406 tests pass; top-up gating, unresolved-holdings handling, strike recalibration, `LIVE_TRADING_ENABLED` enforcement all shipped). Carries two open findings forward: **(a)** a crash between `process_sells` resetting a strike streak and the liquidation actually filling loses the record that a liquidation was decided — M26c's confirmed-fill-only reset rule is the real fix, verify it closes this specific case, not just the general pattern; **(b)** the `bot-state-live` reconciliation has only ever been exercised via direct function calls, never through a dispatched workflow with a real breach — fold into M27's regression coverage. |
| M25 | Reconcile the FOX/LPG divergence by hand | Broker, journal, and `state.json` agree before any new code touches persisted state. Since M24, the paper account was independently reset to zero positions by hand and `state.json` cleared to `{}` — confirm this milestone is that reconciliation already, not separate work, before scheduling it. |
| M26a | Orders/fills journal schema + migration | New `orders` (submitted: symbol, side, notional/qty, limit price, `client_order_id`, timestamp, reason) and `fills` (fill price, filled qty, status, settlement timestamp) tables per §3.3, with a uniqueness constraint on `client_order_id` in `fills`. Migration is a real `PRAGMA table_info`-gated `ALTER`/table-split (matching the M20a `account`-column precedent, not a `CREATE TABLE IF NOT EXISTS` no-op), tested against a real copy of `bot-state`'s `journal.db`, not a synthetic fixture. Migration policy for existing rows stated explicitly and followed, the same discipline M35 uses for `state.json`. |
| M26b | Settlement-pass polling and outcome classification | Given mocked broker responses covering all four §3.3 cases (query failure, genuinely pending, partial fill, full fill), the settlement pass writes the correct `fills` rows; a partial fill journals real filled quantity and cost basis and leaves the remainder pending; re-running settlement against the same `client_order_id` set is demonstrated idempotent — writes each fill at most once. Depends on M26a's schema. |
| M26c | State transitions wired to confirmed fills only | A regression test reproduces the exact FOX/LPG failure shape (order submitted, journaled, never fills) and shows strike/position state does not change until settlement confirms a fill; a partial fill updates cost basis for the filled quantity only and never resets a strike streak. Depends on M26b. |
| M26d | Kill-switch blocking + stale-block escalation | Simulated exhausted-retry query failure sets the account's `KILL_SWITCH_FLAG_FILE_PATH`; a dispatched-workflow-level test (not only a direct-call unit test, per the lesson already carried forward from M24/M27) confirms the next execute run refuses to place orders while the flag is set; a flag still present at the *following* execution window raises a distinct Critical alert, separate from the routine screen-only log line; a hand-cleared flag allows the next window to proceed normally. Depends on M26b. |
| M26e | Order construction: marketable limit + ADV ceiling | Marketable limit at a wider band replaces the −2% DAY limit; no submitted order's notional exceeds a configured fraction of average daily volume, checked against a sample of real recent volume data; both are named constants, not literals. **Independently shippable** — no dependency on M26a–d, can land first if convenient. |
| M27 | Reconciliation gets teeth | A broker/journal mismatch halts execution; the FOX/LPG divergence is reproducible as a regression test; the live-account reconciliation path (carried forward from M24) gets a dispatched-workflow regression test, not only direct-call coverage. |
| M28 | Journal reason strings derived, not hardcoded | Top-ups journal as `TOP_UP`; a test asserts no code path hardcodes `NEW_POSITION`. |
| M29a | Holding state machine core (HEALTHY / DETERIORATING / UNREADABLE) | Given real historical fetch outcomes (passing gates, failing gates, no data), each holding classifies into the correct one of the three states; a strike is recorded once per failed fiscal period, not per run; UNREADABLE never records or resets a strike. |
| M29b | Corporate-action detection (Alpaca Assets API) | On a synthetic/injected fixture — a real delisting on a live holding isn't guaranteed on any development timeline, the same reasoning M42/M49 already use for their own unforceable cases — CORPORATE_ACTION correctly triggers off Alpaca's Assets API `status`/`tradable` field cross-checked against the broker's position record (§3.2), never off absence alone; a test asserts no order is producible from this state. Depends on M29a. |
| M29c | Wire the state machine into evaluate output + site | All four states are distinguishable in the persisted evaluate-run artifact and on the corresponding site page. **Note:** until Epic C (M34) lands, this runs inside the existing single daily loop's output — M34 later moves it into its own quarterly cadence artifact, it does not create the display. Depends on M29a/b. |
| M30 | Add `ruff format --check` to CI | Every pre-existing formatting diff on `main`, re-counted at the time this milestone starts (not assumed from §4.1's historical figure), resolved; formatting and linting no longer diverge. |

**Epic D — Data integrity.** *(No technical dependency on Epic A or Epic
C — runs in parallel, not after. See the note below the table.)*

| ID | Milestone | Exit criteria |
|---|---|---|
| M36 | EDGAR volume/rate-limit check (§3.4), then XBRL companyfacts integration, shadow mode | EDGAR sustains the full-universe + 10-year-backfill request volume within its fair-access limit, measured directly (or the backfill is rescoped to the buyable/near-buyable subset, per §3.4); both sources then run a full cycle in parallel; per-field disagreement report (≥5%-relative-or-1pp-absolute tolerance in §3.4) reviewed by hand before switchover. |
| M37 | XBRL primary; GNTX margin discrepancy resolved | Screen figures match filed figures, within the §3.4 tolerance, for a 20-name sample. |
| M38 | Sector-aware gates | Insurers/REITs either excluded or gated on sector-appropriate metrics; NMIH's gross-margin artifact cannot recur. |
| M39 | Trend terms, average-earnings P/E, and the three-year margin-decline flag | Margin direction and organic-growth terms in the score; on one archived screen's data, the buyable set under average-earnings P/E is a strict subset of the buyable set the same day would have produced under trailing P/E — run both, diff them; hold-cash rule (§3.4) exercised if the buyable list falls below target. |
| M40 | Moat persistence component (§3.6): ROIC persistence, margin stability, revenue/share CAGR; weights rebalanced | Acceptance test passes — GNTX outranks HRMY on the same data; companies with under 10 years of filings score zero on persistence rather than being excluded; both accepted biases, plus the backward-looking-persistence limitation named in §6, disclosed on the methodology page. |
| M41 | Sector cap, ROIC cross-check, remove `MAX_SINGLE_POSITION_WEIGHT` | No sector exceeds 25% of portfolio value in any post-execution snapshot; when the cap binds, the log names the skipped lower-scoring same-sector candidate; sizing stays equal-weight per §3.6. **Sequence after M24 and M26c** (both touch `portfolio.py`'s buy-queue path) — a merge-coordination preference, not a functional dependency on the rest of Epic A. |

*Why the margin-decline flag moved into M39.* Persistence at 0.40 weight
is backward-looking by construction: a business whose moat broke last
year still scores well for years afterward. The margin-trend flag is the
counterweight, it needs no model, and it is computable from the same
XBRL data — so it ships alongside the trend terms rather than waiting on
the optional tranche.

*Why Epic D moved to run alongside Epic A, not after it (pm-reviewer
planning finding).* Epic D has no technical dependency on Epic A or
Epic C: it touches `screener.py`/`data.py`, disjoint from Epic A's
`portfolio.py`/`execution.py`/`journal.py` and Epic C's workflow files
and `state.json` format. The reason to land it *before* the restart
(M32) rather than after — the earlier draft placed it in a later
tranche entirely — is not module independence, it's that M32's whole
purpose is to produce a clean, single-discontinuity paper record. If
Epic D lands after the restart, that just-reset record takes a second
major discontinuity almost immediately: the XBRL swap shrinks the
buyable universe, sector gates exclude names, and the moat reweighting
flips the GNTX/HRMY ranking — the exact failure mode §3.4's own
shadow-mode language exists to prevent for the *data source*, recurring
one level up against the *validation record* if Epic D isn't also
settled first. Running it in parallel with Epic A means the restart
happens once, on top of both corrected execution and corrected
screening.

**Epic E — Material-event monitoring, deterministic half (§3.8 Tier 1).**
No dependency on Epic A, C, or D — reuses existing Discord plumbing and
touches neither `screener.py` nor `portfolio.py`/`execution.py`. Can be
built any time; recommended, not required, to land before the restart
so the "alerts per holding per quarter" process metric has a real
baseline from day one of the clean paper record rather than starting
mid-window.

| ID | Milestone | Exit criteria |
|---|---|---|
| M42 | 8-K polling for held tickers, classified by item number, **with alert-only enforcement shipped in the same change** | On a synthetic/injected 8-K fixture per item type (a real Critical-severity 8-K on a held name isn't guaranteed within any development timeline, same reasoning as M29b), classification and alerting fire correctly within one business day of the poll; Item 2.02 suppressed by default; no model involved; a test asserts no code path lets an event record produce an order, adjust a score, or record a strike. Re-confirm against a real 8-K opportunistically once one occurs, but don't block the milestone on waiting for one. |
| M43 | Manual-override journaling + rate limiting | Overrides journaled with a reason and counted as a process metric; alerts per holding per quarter tracked, with a rising rate treated as a taxonomy defect rather than a market signal. |

*Alert-only enforcement lands with the poller, not after it.* An alert
channel built without that constraint enforced in code acquires
trade-triggering behavior the first time an event feels urgent enough —
which is precisely the drift §3.8 exists to prevent.

### Tranche 2 — Cadence decoupling

**Epic C — Cadence decoupling.** *Genuinely depends on Epic A*: M34
deploys the corrected settlement pass (M26) and state machine (M29)
into their own quarterly/monthly-cadence workflows, so there's nothing
meaningful to split into separate jobs until those exist. Start once
M26 and M29 land — M27/M28/M30 do not need to be done first.

| ID | Milestone | Exit criteria |
|---|---|---|
| M34 | Split into three workflows (screen / evaluate / execute) | Paper/live isolation verified per workflow; no shared local state; the daily-screen/quarterly-evaluate gap labelled on the site per §3.1; the evaluate workflow now runs M29a–c's state machine on its own cadence. |
| M35 | Period-based strikes with versioned `state.json` migration | Schema version recorded as a top-level `{"version": 2, "strikes": {...}}` field (the current file has no such field at all — this is a shape change, not a value change); migration policy stated and followed, not left implicit — **every in-flight integer streak is migrated to an empty period list, not a synthesized placeholder period.** A fabricated historical period would misrepresent when the strike actually occurred; the honest choice is that any ticker mid-streak at migration time starts clean and must fail quality again, under the new rule, to re-accumulate. Tested against real persisted state from `bot-state`, not a synthetic fixture. |

### Tranche 3 — Restart and validation

**Epic B — Restart validation.** *Gate corrected (pm-reviewer planning
finding — an internal inconsistency in the earlier draft): §3.7 says
the restart happens "once §3.1–3.3 land" — that's Epic C **and** Epic
A, not Epic A alone; the earlier draft sequenced M32 in Tranche 1,
ahead of Epic C, contradicting §3.7's own stated gate. Combined with the
Epic D finding above, M32 does not run until Epic A + Epic C + Epic D
are all complete (Epic E strongly recommended, not required).*

| ID | Milestone | Exit criteria |
|---|---|---|
| M31 | Pre-register benchmark, horizon, process metrics, kill criteria | Committed file, dated, before restart. |
| M32 | Reset paper account; begin clean record | Fresh account, zero positions, `bot-state` archived not deleted. **Gated on Epic A + Epic C + Epic D complete** (Epic E recommended), not on Epic A alone. |
| M33 | Process-conformance dashboard | Every metric on the dashboard hand-verified at least once against the underlying journal/`state.json`/fills data **for one full quarter** — "visible on the site" means "rendered *and* checked to match the source data," not merely that the page loads. This is a hard calendar floor of one fiscal quarter after M32, independent of engineering effort. |

### Tranche 4 — Qualitative layer (optional)

Unchanged in content and internal sequencing from the earlier draft.
Epic F and Epic G are independent of Tranches 1–3's modules and don't
gate the live-trading criterion below (only Tranches 1–3 do) — they can
be developed in parallel with Tranche 3's mandatory quarter-long wait
rather than strictly after it.

**Epic F — Filing analysis agent.**

| ID | Milestone | Exit criteria |
|---|---|---|
| M44 | EDGAR retrieval + Item 1/1A/3/7 section parser | Boundaries verified on 20 filings across sectors. |
| M45 | Extraction with quote verification + golden set in CI | Precision and recall measured against ~30 labeled filings, **≥90% precision / ≥80% recall on Tier-1 disqualifiers** per §3.5's stated bar; every field traceable to a verbatim span; the agent influences no order until this gate is green. |
| M46 | Tier-1/Tier-2 rule layer (routine-auto-halve vs. structural-threat-holds-for-human-decision split, per §3.5) + moat-mechanism classification (display-only) | Regression fixtures reproduce the HRMY veto, GNTX's two routine Tier-2 flags auto-halving, and GNTX's structural-threat flag correctly holding at last weight and raising the named human-decision alert. |
| M47 | Wire into the screen as veto-and-flag; display before it trades | Findings visible on the site for one full cycle before influencing orders. |

**Epic G — Material-event monitoring, model-assisted half (§3.8 Tier 2).**

| ID | Milestone | Exit criteria |
|---|---|---|
| M48 | Retarget `news_update.py` to per-holding event extraction against the closed taxonomy | Taxonomy is an enumeration in code; every emitted record carries an event type, a date, and a working source URL; records failing any of the three are discarded, not downgraded. |
| M49 | Backtest the taxonomy against known events | The Feb 2026 HRMY ruling, Takeda's competing orexin agonist, and the camera-monitor regulation changes are each surfaced from contemporaneous sources. |

*If Tranche 4 never ships*, Epic E still closes the
accounting-irregularity and auditor-change blind spots entirely, and the
deterministic moat and trend work in Tranche 1's Epic D still corrects
the HRMY-over-GNTX ranking. That is most of the downside protection at a
small fraction of the cost, which is why the split falls where it does.

### Live trading gate

Live remains disabled — `MUNGER_LIVE_TRADING_ENABLED` unset — until
Tranches 1 through 3 are complete (Epic A, Epic C, Epic D, and Epic B's
restart+validation — Epic E strongly recommended before M32 as well),
the pre-registered process metrics are green for two consecutive
quarters after M33's own quarter-long verification closes, and the live
P&L IAM path is verified working end to end. Tranche 4 (Epic F, Epic G)
is not required for this gate. The current arrangement, where live
trading was reachable on credentials alone while its own observability
pipeline returned 403 every run, is the specific failure this gate
exists to prevent.

### Revised time estimate

The earlier draft (§4.3) called the whole plan "roughly six months of
part-time work," asserted rather than derived. Reasoning instead from
this project's own demonstrated pace (pm-reviewer planning finding):
`TASKS.md` shows M20 through M23 — in aggregate, comparable in scope to
the newly-split M26 (5 pieces) plus M29 (3 pieces) — shipped within two
active working days (2026-08-10, 2026-08-12), including design, code,
tests, and review gates. Coding throughput is not the constraint on this
project; two things are.

First, session cadence: the real gap between M23 (2026-08-12) and M24
(2026-09-02) was 21 calendar days with no commits — this project runs in
bursts, not continuously, consistent with genuine part-time solo work.
Scaling the ~32 required milestones in Tranches 1–3 (up from 26 in the
unsplit draft) against a few-sessions-per-week part-time cadence, with
Epic A and Epic D run in parallel, puts the active-engineering portion
of Tranches 1–3 at roughly **6–10 weeks of elapsed part-time work**.

Second, and larger: several exit criteria have a calendar floor that no
amount of engineering speed shortens. M36's shadow-mode cycle needs a
full parallel-run cycle before switchover (days, at daily-screen
cadence). And M33 — which gates the live-trading criterion — requires a
full fiscal quarter of hand-verified dashboard data *after* the restart
(M32) before it can close. That quarter starts only once Epic A, Epic C,
and Epic D are all done, per the corrected gate above.

Putting the two together: **roughly 2–2.5 months of elapsed part-time
work to reach the restart (M32)**, followed by a **structural ~3-month
floor for M33** that runs regardless of further engineering effort
(Tranche 4 can be built during this window without affecting the
critical path). Total elapsed time to the live-trading gate being
satisfiable: **roughly 5–6 months** — similar in magnitude to the
original six-month guess, but for a different and more actionable
reason: not because the work is large, but because a mandatory
quarter-long observation window is on the critical path and was implicit
rather than accounted for in the original estimate.

## 6. What v2 Still Does Not Solve

The strategy remains unvalidated. Everything above improves the
*fidelity* with which the system executes an idea; none of it provides
evidence the idea works. That evidence takes years and may never arrive
— value approaches underperform for long stretches, and a sample of one
portfolio run by one person is weak evidence either way.

The filing agent reads what companies disclose about themselves and is
only as current as the last annual report. §3.8 closes most of that gap
— 8-K polling catches disclosed material events deterministically, and
Tier-2 monitoring targets the third-party events that a company never
files. What remains is a narrower and more honest limitation: the event
layer is alert-only by design, so its value depends entirely on a human
reading the alert and acting. A system that surfaces the right warning
to someone who ignores it has not actually seen anything.

And the deepest limitation is narrower than v1 claimed, but real. Two of
the three moat layers in §3.6 do automate: persistence is measurable
from a decade of filings, and the claimed mechanism is extractable from
Item 1. What does not automate is durability against a specific,
identified future threat — GNTX is the standing example, where twenty
years of high returns and 79% global share coexist with regulators in
four major markets now permitting cameras to replace mirrors entirely.
Every backward-looking series says wide moat. None of them price a
regime change.

So the honest statement is this: the system measures whether a moat
*has existed* and enforces discipline about price and quality. It cannot
judge whether a moat *will survive* a threat that has no precedent in
the data. That judgment is yours, it is the one Munger spent his career
on, and the site should say so plainly rather than letting a persistence
score read as a verdict on the future.
