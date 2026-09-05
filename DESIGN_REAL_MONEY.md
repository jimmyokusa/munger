# Design: Real-money trading page (M20)

**Status: M20a approved to build (2026-08-10). M20b (donations) and the
subsequent AdSense/affiliate/"$100k funding pipeline" reframing are both
DROPPED — see §0.1.** No code exists yet. Both engineering review gates
(pm-reviewer + staff-engineer-reviewer) are complete for M20a; findings
folded in below. **staff-engineer-reviewer found the state-isolation
mechanism in the first draft did not actually hold** (§3.1 rewritten,
plus a new §3.4/§3.5) — flagged here because it was the single most
severe finding and changes §3's concrete mechanism, not just its wording.

**Scope, after §0.1: M20a only — a live account + real-money page (§3,
§4), funded entirely by the user's own capital, no public funding
mechanism of any kind.** §5 (donations) is retained below for the
historical record (it was fully designed and passed both engineering
review gates before being dropped for legal reasons, not technical ones)
but is not being built.
- **M20b — donations (§5).** Additionally gated on the user's legal
  review completing. Ships independently, after M20a, whenever that
  clears — not bundled into one atomic build.

_Origin: user request, 2026-08-10, following the M19 intraday P&L work. "I
want to work on a design for real money trading, it will be a separate
page, with separate graphs similar to what we do for paper trading, I want
to have a donate option on the website where donations get added to the
real money trading on Alpaca." Clarified in conversation: donations are
**pure gifts, no strings attached** — no promised return, no profit share,
no equity claim, no redemption right, no per-donor tracking of "their"
performance. The user already has a live (non-paper) Alpaca account, KYC'd
and funded. Payment collection: Stripe, with **manual** (not automated)
transfer of accumulated donations into the Alpaca account._

---

## 0. Decisions evaluated and dropped

- **A webhook receiver for Stripe payment events — dropped.** The obvious
  way to record donations in real time is a live endpoint Stripe POSTs to
  on each successful payment. This app has no such endpoint anywhere —
  `report.py` is a batch job producing static HTML; `report-web` is a dumb
  nginx service with no application code (§1). Standing up a webhook
  receiver would mean a genuinely new always-on stateful service, signature
  verification, and a new attack surface, for a feature (a running
  donation total) that has no need to be second-by-second fresh. Instead:
  **§5 uses a scheduled poller** (`donations.py`, mirroring `pnl.py`'s and
  `prices.py`'s existing shape) that reads Stripe's own API for completed
  payments on a cadence and writes a small JSON file through the same
  GCS-bridge pattern already built for M16/M17. Zero new live components.
- **Dynamic per-request account switching in one process — dropped.**
  `ExecutionModule`/`pnl.py`/`config.py` currently read
  `config.ALPACA_API_KEY`/`ALPACA_SECRET_KEY`/`PAPER_TRADING` as
  process-global constants, set once at import time from the environment.
  Refactoring the trading engine to accept an explicit "which account"
  argument threaded through every module would be a real, invasive change
  to code that has been live and stable since M8/M10. Instead: **§3 runs
  the exact same `bot.py`/`pnl.py` binaries a second time**, unmodified
  except for one new env-driven `config.PAPER_TRADING` (currently a
  hardcoded `True`), against a second set of credentials and a second,
  isolated state/output path — the same "one binary, environment decides
  behavior" idiom this codebase already uses everywhere (`MUNGER_DATA_DIR`,
  `MUNGER_GRAFANA_URL`, etc.), not a new pattern.
- **Automated Stripe→bank→Alpaca funds transfer — dropped, per user
  decision.** Technically possible (Stripe payouts to a linked bank
  account, then an ACH transfer instruction to Alpaca), but the user chose
  the manual model specifically: fewer moving parts, no code that ever
  initiates a real funds movement (this assistant's own operating rules
  also prohibit executing financial transfers on the user's behalf — see
  the note in §3), and it keeps the "pure gift, you decide what to do with
  it" framing honest rather than implying a formal pooled-fund mechanism.
- **Stripe Checkout Sessions (server-created) — dropped in favor of Stripe
  Payment Links.** A Checkout Session is normally created server-side with
  a secret key per transaction; this app has no request-serving backend to
  create one from (same constraint as the webhook decision above). A
  **Payment Link** is a hosted checkout URL created once, manually, in the
  Stripe Dashboard (or via a one-off API call, not a running service) —
  the site just links/buttons to it. Zero new backend code for the
  collection side; only the *reporting* side (§5) needs a scheduled job.

### 0.1 Public funding (donations, then a broader ads/affiliate/"$100k
target" pipeline) — dropped, per legal advice (2026-08-10)

Recorded the way §0 records the smaller in-doc decisions above, and the
way this project records other deliberate no's (M1's declined FMP API,
M17's dropped Prometheus) — a considered decision, not a silent removal.

**Sequence:** §5 (donations, "pure gift, no strings") was fully designed
and passed both engineering review gates. The user's legal review of that
specific model passed. Separately, in the same session, the user then
described a broader reframing: donations *plus* Google AdSense ad revenue
*plus* affiliate-link revenue, all accumulating toward one publicly-stated
**$100,000 target**, with live trading contingent on reaching it. That
reframing was **not** covered by the review that had already passed — a
stated collective target that multiple revenue streams count toward is a
different legal fact pattern (closer to the "common enterprise" element
regulators look for) than individual undirected gifts with no stated
goal. A background memo was drafted (informational only, not legal
advice) for the user to bring back to their attorney specifically on that
delta, before any of the ads/affiliate/target-progress code was built.

**Outcome, after that follow-up consultation: the user's attorney advised
against crowdfunding this account in any of these forms.** The user
decided to fund the live account entirely with their own capital instead.
**This drops all of §5 (donations) along with the entire AdSense/
affiliate/$100k-target reframing** — not deferred, not "revisit later,"
dropped on legal advice. §5 is kept below only as the historical design
record (it was sound engineering, killed for a legal reason unrelated to
its architecture) — nothing in it should be built. `config.PAPER_TRADING`
being env-driven, and everything in §3/§4, are **unaffected**: the live
account still exists, still trades the identical strategy, still gets a
public page — it's simply funded the ordinary way, by its owner, with no
donation button, no ads, no affiliate links, and no stated funding
target anywhere on the site.

---

## 1. Inherited constraints (non-negotiable)

1. **Static-site + generated-artifact model** (established M13/M17/M18,
   `DESIGN_WEB_ANALYTICS_SEO.md` §1.1/1.2). `report.py` emits static HTML;
   nginx (`report-web`) only serves bytes, on both Cloud Run (GCS FUSE
   mount) and k3s (PVC `report` subPath). No page here can depend on a
   live backend to render.
2. **Alpaca credentials never reach the public deployment**
   (`DESIGN.md` §3.5/§3.8.1, M14's screen-only boundary). This is the
   single most load-bearing property in the whole codebase and this
   milestone does not relax it: the live-trading credentials (§3) run
   exactly where the existing paper ones do (GitHub Actions), never on
   Cloud Run/k3s.
3. **Config-gated feature pattern** (`GRAFANA_BASE_URL`,
   `SITE_BASE_URL`/`ANALYTICS_URL` templates). New optional output is
   gated on an env-derived constant that defaults to empty/off — a
   deployment that hasn't set the live-account or Stripe env vars gets
   exactly today's behavior, nothing new baked in.
4. **Position sizing is already equity-relative, not dollar-hardcoded**
   (`portfolio.py`, `config.GLOBAL_NOTIONAL_BUDGET_PCT`/
   `TARGET_POSITION_COUNT`/`MAX_SINGLE_POSITION_WEIGHT`). The trading
   engine already scales its own risk to whatever equity actually exists
   in the account it's pointed at — a real, existing safety property this
   milestone inherits for free, not something new it has to build. It
   does **not** by itself bound worst-case dollar loss from a bug (see
   §3.3).
5. **The raised-stakes precondition already named for this exact
   moment.** `DESIGN.md` §3.8 (M16) flagged: the public report "must stay
   screen-only, paper-account data" and must be revisited "before any
   paper→live go/no-go... since the same report would then show real
   position sizes." This milestone **is** that go/no-go. §6 resolves it
   explicitly rather than letting it slide by implication.
6. **This assistant does not execute financial transfers or trades.**
   Nothing in this design has Claude (in any session, then or now) move
   money or place an order — the live trading engine is `bot.py` running
   unattended in CI exactly as it already does for paper, and any
   donation→brokerage transfer is a manual human action outside the
   codebase, by design (§0).

---

## 2. Legal/product framing

Donations are **pure gifts**: a donor sends money with no promise of any
return, no profit share, no equity or ownership stake in the account, no
redemption or withdrawal right, and no individual tracking of "their"
contribution's performance. The site does not offer, and must not be
worded to imply, an investment relationship.

This is a product/legal decision, not an engineering one, and it is **not
finalized**. The user is getting independent legal review before any of
§5 goes live (a separate research briefing — informational only, not legal
advice — was compiled in the same session as this doc). Two things this
review will directly determine, which this design treats as open
placeholders rather than assumptions:

- **Exact donation-page copy.** "Pure gift" needs to actually read that
  way to a visitor, not just be true in a disclaimer's fine print — page
  wording is drafted here as a placeholder (§5.3) pending real review, the
  same way `_disclaimer_banner()`'s "not investment advice" text exists
  today but this milestone's copy is materially higher-stakes (real money
  changing hands, not just a screening opinion).
- **Whether any entity structure is needed** (e.g., does this stay a sole
  proprietor personal account, or does the user's attorney recommend an
  LLC or similar wrapper). Out of scope for this doc either way — noted
  because it could affect which bank account/Alpaca account entity name
  is used, which is a user-side administrative step, not code.

---

## 3. Live-account trading architecture

### 3.1 Running paper and live in parallel

**Revised after staff-engineer-reviewer finding — the first draft's
isolation claim did not actually hold.** That draft said "a second
job/step" without committing to which, and specified only the git branch
and GCS *output* prefix as different between the two. Verified against
the real code: `config.STATE_FILE_PATH`/`JOURNAL_DB_PATH`/
`KILL_SWITCH_FLAG_FILE_PATH`/`SCREEN_RESULTS_CSV_PATH`/`PNL_DATA_PATH`/
`PNL_HISTORY_PATH` are all `DATA_DIR`-relative, and `DATA_DIR` defaults to
the checkout root unless `MUNGER_DATA_DIR` is set — which the existing
`daily-trade.yml` never does. If live were added as extra **steps** in
that same job (the cheaper, more natural-looking implementation, since it
avoids a second checkout), both accounts would read/write the literal
same local `state.json`/`journal.db`/`KILL_SWITCH` file on the runner's
disk regardless of which GCS prefix or git branch each leg *intends* to
persist to.

**Decided: live runs as a genuinely separate GitHub Actions *workflow
file*** (e.g. `daily-trade-live.yml`), not a second job or step bolted
onto `daily-trade.yml` — the same shape M19 already established for
`pnl-snapshot.yml` rather than folding into `daily-trade.yml` ("a display-
refresh cadence is not a reason to run the trading logic more often," the
identical reasoning applies here: a second account is not a reason to
complicate the first account's own workflow). A separate workflow file
gets a fresh runner and a fresh checkout automatically — no shared local
disk between the two accounts is possible, without needing `MUNGER_DATA_DIR`
gymnastics to simulate isolation inside one job. `MUNGER_DATA_DIR` is
still set (to a distinct value, e.g. unset/default for paper is fine
as-is; live's workflow doesn't need it either, precisely because it's a
different runner) — noted only to be explicit that this is "different CI
run" isolation, not "different directory, same run" isolation.

| | Paper (existing, `daily-trade.yml`) | Live (new, `daily-trade-live.yml`) |
|---|---|---|
| `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` | existing `secrets.ALPACA_API_KEY` | new `secrets.ALPACA_LIVE_API_KEY`/`ALPACA_LIVE_SECRET_KEY` |
| `MUNGER_PAPER_TRADING` | unset (defaults `true`) | `false` |
| State (`state.json`/`journal.db`) | `bot-state` branch (existing) | new `bot-state-live` branch, restored/persisted by live's own copy of `daily-trade.yml`'s existing "Prepare/Persist bot-state branch" steps |
| Durable P&L history (`pnl_history.jsonl`) | `gs://munger-503515-data/pnl_history.jsonl` (existing) | `gs://munger-503515-data/live/pnl_history.jsonl` — **live's workflow needs its own copy of `daily-trade.yml`'s "Download prior P&L history from GCS" step**, scoped to this path, not `MUNGER_PNL_SNAPSHOT_ONLY=1` (that M19 flag is for the *intraday supplementary* job only — the once-daily live run is the canonical run for its own account, same as paper's, and must maintain its own durable history the same careful way) |
| Current-state P&L (`pnl.json`) | `gs://munger-503515-data/pnl.json` (existing) | `gs://munger-503515-data/live/pnl.json` |

The one actual code change to `bot.py`/`pnl.py`/`execution.py` themselves:
`config.PAPER_TRADING` is currently a hardcoded `True` (`config.py:142`),
not read from the environment like `ALPACA_API_KEY` is. Needs to become
`os.environ.get("MUNGER_PAPER_TRADING", "true").lower() == "true"` — a
one-line change, defaulting to today's exact behavior when unset, so no
existing deployment changes behavior by accident. Confirmed safe to do
this way: every read site in the codebase does `import config; ...
config.PAPER_TRADING`, never `from config import PAPER_TRADING`, so there
is no import-time literal-bool binding anywhere that an env-string parse
would break (staff-engineer-reviewer verified this against the actual
source, not assumed).

Screening stays shared: `screener.py`'s picks are the same regardless of
which account executes them (the whole point being "the identical
strategy, one demonstration account, one real account" — divergent picks
between the two would undermine the comparison the page is meant to make).
Only the portfolio-construction/execution/journal layer runs twice, once
per account, both reading the same day's `screen_results.csv` (which
`daily-trade-live.yml` reads from the paper run's own checkout/output —
exact mechanism TBD at implementation time, e.g. `daily-trade-live.yml`
triggered after `daily-trade.yml` completes, or both reading a
`screen_results.csv` already bridged to GCS the way `pnl.json` is).

### 3.2 Kill switch scope

**Escalated from "runbook-only" to a code-level fix, per staff-engineer-
reviewer finding.** The first draft treated the two-kill-switch gotcha
(separate `DATA_DIR`s per §3.1 mean separate `KILL_SWITCH` files, so
stopping one account doesn't stop the other) as a documentation-only
concern. Given the stakes — and this project's own precedent of treating
a similar "matters once, catastrophically" case (the paper/live API-key
mismatch check, `DESIGN.md` §4) as a mandatory code-level abort, not a
runbook note — that bar is too low here. **Decided:** a single,
account-independent master kill switch (e.g.
`config.GLOBAL_KILL_SWITCH_FLAG_FILE_PATH`, anchored outside either
account's own `DATA_DIR` — a fixed path in the repo checkout itself, or a
GitHub Actions repo variable checked before either workflow's trading
step runs) that **both** `daily-trade.yml` and `daily-trade-live.yml`
check first, unconditionally, before their own per-account
`KILL_SWITCH`. One flag stops both accounts; each account's existing
per-account flag remains for stopping just one without touching the
other. Exact placement/mechanism is an implementation detail for the
build, not decided further here.

### 3.3 Raised-stakes safety

§1.4 already gives the engine equity-relative sizing, which bounds
*proportional* risk but not worst-case dollar loss from a data bug (e.g. a
corrupted equity read feeding a wildly wrong percentage).

- An absolute per-order and/or per-account dollar ceiling, independent of
  the existing percentage logic, purely as a blast-radius bound — still
  not decided (exact number is a build-time/user decision), but see the
  interim default below for the posture until it is.
- **Decided (staff-engineer-reviewer finding): reconciliation-mismatch
  handling aborts, not warns-and-continues, for the live account
  specifically.** The first draft left this open; the reviewer's own
  assessment was direct enough to resolve it here rather than defer
  again — "given how thin the current defense-in-depth is elsewhere in
  this design" (see §3.4/§3.5 below), a mismatch on the live account
  should stop the run rather than proceed on a best-effort basis, even
  though paper's existing behavior (warn-and-continue, M10) stays
  unchanged for paper.

**Interim default until the dollar ceiling is set (pm-reviewer finding):**
the combination of "not decided" here plus §7's other open items would
otherwise leave the live account's actual maximum dollar exposure fully
unbounded at doc-review-complete time. Starts on the existing
equity-relative sizing only (§1.4), **no absolute ceiling, and the
account funded conservatively/small to start** (exact starting size is
the user's call, §7) — until an absolute ceiling is explicitly agreed
at build time, the small starting balance itself is the blast-radius
bound.

### 3.4 Journal schema: defense-in-depth against imperfect isolation (staff-engineer-reviewer finding)

§3.1's workflow-level separation (separate CI runs, separate git
branches, separate GCS paths) is the primary isolation mechanism, but
`journal.py`'s `journal` table has **no account/mode column at all** —
`client_order_id` is a plain `TEXT` field, not unique-constrained, and
`get_expected_holdings()`/`check_reconciliation()` group by `MAX(id)` per
symbol with no account filter. If workflow-level isolation is ever
imperfect for even one run (a failed restore step, a misconfigured
secret, a future edit that accidentally merges the two workflows), rows
from both accounts could merge into one journal **silently, with no way
to detect or unwind it after the fact** — the one mechanism
(`check_reconciliation`) this system relies on to catch exactly this
class of problem would itself be reading corrupted, cross-account data
without knowing it.

**Decided:** add an explicit `account` (or `mode`) column to `journal.py`'s
schema — `"paper"`/`"live"`, written on every insert, read by
`check_reconciliation()`'s queries as an explicit filter, not an implicit
assumption. Cheap, and closes this gap independently of whether §3.1's
workflow-level separation holds perfectly, which is the point of
defense-in-depth: it shouldn't have to.

### 3.5 `client_order_id`: not a broker-level collision risk, but worth tagging anyway

Verified: Alpaca paper and live are separate endpoints reached with
separate credentials (`execution.py` constructs a fresh `TradingClient`
per `ExecutionModule` instance, keyed to whichever account's
`api_key`/`paper` flag it was built with), so `has_already_submitted`'s
`get_order_by_client_id` lookup is inherently account-scoped at the
broker even though `_client_order_id` itself
(`f"{run_date}-{symbol}-{side}"`) carries no account tag today — no
cross-account idempotency risk at Alpaca's end.

The absence of an account tag only becomes a problem in combination with
§3.4's gap: two journal rows with the *identical* `client_order_id`
string and no schema-level way to tell which account a given row belongs
to. Decided, as a second, independent piece of defense-in-depth: prefix
`client_order_id` with the account too (e.g.
`f"{mode}-{run_date}-{symbol}-{side}"`), so even a raw read of
`journal.db` without the new §3.4 column is self-describing.

---

## 4. Real-money P&L page

Mirrors `pnl.html` closely, reusing the M19 machinery — **but not by
naive unmodified reuse**, per a concrete bug the staff-engineer-reviewer
pass found: `_render_pnl`'s `<h1>Paper trading P&amp;L</h1>` heading, its
"paper money only, not investment advice" body text, and `_seo_head`'s
`"pnl.html"` title/description/canonical-slug arguments are all literal
strings today, not parameterized on mode — calling `_render_pnl`
unmodified against a live snapshot would render a page whose own heading
and disclaimer say "paper" while showing real dollar figures. Worse,
`_pnl_polling_script`'s client-side poller does `fetch('pnl.json', ...)`
as a **hardcoded literal**, unlike the two genuinely templated tokens
(`__PNL_STALENESS_MAX_HOURS__`, `__PNL_POLL_INTERVAL_MS__`) that same
function already uses — reusing it unmodified on `real-money.html` would
have the live page silently re-fetch and display the **paper** account's
numbers every 30 minutes under a "LIVE" badge, with no error, since the
fetch would succeed.

- New page, e.g. `real-money.html`, generated by a **parameterized**
  `_render_pnl(snapshot, mode_label=..., snapshot_url=...)` — heading,
  disclaimer copy, `_seo_head` slug, and (new) the polling script's fetch
  target all become arguments, not literals, with paper's existing call
  site passing today's exact values so `pnl.html`'s output is unchanged
  bit-for-bit. The fetch-target token joins the other two as a third
  `_PNL_POLLING_SCRIPT_TEMPLATE` placeholder (e.g.
  `__PNL_SNAPSHOT_URL__`), not a fourth ad hoc mechanism.
- Loads a **second** snapshot file (`real_money.json`, parallel to
  `pnl.json`, same shape — `pnl.py`'s snapshot format is already
  account-agnostic, labeling `"mode": "paper"` or `"live"` from
  `config.PAPER_TRADING`, M16's "extensible to real money" design intent
  finally exercised).
- Same intraday-refresh treatment as M19 if wanted (30-min cadence,
  client-side poll) — reuses that machinery by pointing a second
  `pnl-snapshot.yml`-style job at the live credentials and a
  `real_money.json` GCS path, rather than building refresh infrastructure
  twice. **Also needs its own Cloud Run nginx exact-match location block**
  (staff-engineer-reviewer finding — the first draft's §4 mentioned the
  `gcs_bridge.py` k3s copy but not this): M19's `location = /pnl.json`
  alias exists specifically because `DATA_DIR` root isn't otherwise
  served; without an equivalent `location = /real_money.json` block, the
  client-side poll 404s on prod even once pointed at the right file.
- `gcs_bridge.py` gets a third file to pull for k3s parity, same pattern
  as `pnl.json`/`prices.json` today.
- **GCS IAM scoping needs its own iteration, not an assumed extension**
  (staff-engineer-reviewer finding). `DESIGN.md` §3.8 documents that
  `munger-pnl-writer`'s IAM condition is scoped to the literal object
  names `pnl.json`/`pnl_history.jsonl`/`prices.json` — it took three
  attempts to get that scoping right (create-vs-overwrite needing
  `delete`, plus bucket-level `list`). The new `live/pnl.json`,
  `live/pnl_history.jsonl`, and (§5) `donations.json` object names are
  **not** covered by that existing condition. Named explicitly here so
  whoever implements this expects an IAM iteration rather than assuming
  "same bridge pattern" implies "already permissioned" — matching this
  project's own documented history on exactly this point (M16/M17, four
  iterations total).
- Nav: a new link from the existing pages to `real-money.html`, clearly
  labeled (e.g. a distinct badge/color from the `PAPER` mode badge already
  rendered on `pnl.html`, so a visitor never confuses the two accounts).
- **Config-gated, per pm-reviewer finding** — matching §1.3's own stated
  pattern (`GRAFANA_BASE_URL`/`SITE_BASE_URL`), which this section
  described but didn't actually apply to itself. The page/nav-link only
  generate when `config.LIVE_TRADING_ENABLED` is set. **Corrected at
  implementation time from this section's own original wording:** not
  "derived from whether the live credentials are actually configured" —
  report.py's deployment must never hold *or even reference the name of*
  an Alpaca credential (§1.2/M14), so `LIVE_TRADING_ENABLED` is a plain
  manually-set env flag (`MUNGER_LIVE_TRADING_ENABLED=1` on the
  daily-screen Job), identical in shape to `GRAFANA_BASE_URL`, not a read
  of `ALPACA_LIVE_API_KEY`. A test (`test_live_trading_enabled_never_
  reads_alpaca_live_credentials`) locks this boundary in — otherwise a
  deployment without the live account provisioned yet would render a dead
  link or an empty page, the same failure mode §1.3 already exists to
  prevent for Grafana/SEO.

### 4.1 Verification bar before "live" (pm-reviewer finding)

Mirrors the standard this project has applied to every prior data-facing
milestone (M16 row 551: paper P&L checked against Alpaca's own dashboard
to within intraday drift; M17 row 599 and M18's analytics row: explicit
user visual confirmation before "done"). Not new process, just restating
the existing bar for this milestone specifically, since the doc had
omitted it:

1. The live-account snapshot's equity/cash/positions checked against
   Alpaca's own live-account dashboard at the same moment, same tolerance
   M16 row 551 used (ordinary intraday drift, not exact-to-the-cent).
2. The user visually confirms `real-money.html` renders correctly against
   the real live account before it's considered live, not just deployed.

---

## 5. Donations (M20b) — DROPPED, historical record only (§0.1)

**Not being built.** Kept below unmodified as the design record of a
fully-reviewed piece of work that was killed for legal reasons, not
technical ones — see §0.1 for why. Nothing in this section should be
implemented.

### 5.0 Technical backstop on the legal gate (pm-reviewer finding)

§0/§2 gate this section on the user's legal review, but as originally
written that gate was enforced by human discipline alone — the Payment
Link is created outside the codebase (§5.1), so nothing here would
actually stop the donate section from shipping early, and this project
has direct precedent for a review gate being skipped under real-world
pressure (`TASKS.md` M18: shipped to prod before its review gates ran,
logged retroactively as a process gap). Fix: a new
`config.DONATIONS_LEGAL_REVIEW_COMPLETE` constant, defaulting `False`
(off), gating whether `report.py` renders the donate section/link at all —
the same config-gated-off-by-default pattern §1.3 already uses everywhere
else. Someone could still create a Payment Link early, but the site
itself won't advertise or link to it until this flag is deliberately
flipped, which is a real code review + deploy, not a silent slip.

### 5.1 Collection: Stripe Payment Link

Created once, manually, in the Stripe Dashboard (or a one-off API call) —
not generated by this codebase. `real-money.html` links/buttons to it
(gated per §5.0). Stripe hosts the actual checkout; no card data, no PCI
scope, and no new backend code touches this app at all.

### 5.2 Reporting: `donations.py` (new module, mirrors `pnl.py`/`prices.py`)

Runs on a schedule in GitHub Actions (does **not** need Alpaca
credentials — only a Stripe **restricted, read-only** API key, scoped to
reading charges/payment intents, never to creating refunds or payouts).
Sums completed payments since some starting point, writes a small JSON:

```json
{"generated_at": "...", "total_donated_cents": 123456, "count": 42}
```

Uploaded to GCS via the same bridge pattern as `pnl.json`, read by
`report.py` to render stats on `real-money.html` — deliberately kept
**separate from account equity** in the P&L tiles, so a visitor never
mistakes "money donated" for "money made." Two follow-up fixes from
pm-reviewer, both about that same "don't show a number a visitor could
reasonably misread" concern:

- **Refunds/chargebacks (pm-reviewer finding — not addressed in the first
  draft).** "Completed payments" isn't necessarily final — a chargeback
  after the fact would leave the displayed total stale/wrong with no
  reconciliation. `donations.py` sums Stripe's own **net** figure (succeeded
  charges minus refunds/disputes as Stripe's API reports them), not a
  naive one-time "successful payment" tally, and re-derives the whole sum
  fresh each run rather than incrementally accumulating — the same
  "re-derive from source of truth every run" posture `prices.json` already
  uses for exactly this kind of staleness risk.
- **"Total donated" is not the same fact as "in the trading account" (pm-
  reviewer finding).** §0's manual-transfer decision means these two
  numbers can legitimately diverge for a while. Shown as two honestly
  distinct, separately labeled stats — "Total donated" (from `donations.py`)
  and the real-money page's existing "Equity" tile (from the live Alpaca
  snapshot, §4) — rather than one merged figure implying donated money is
  immediately reflected in account equity.

**Verification bar before donations go live (pm-reviewer finding,
mirrors §4.1):** `donations.py`'s running total checked against Stripe's
own dashboard total at the same moment before being trusted publicly —
same "verify against the actual source of truth" bar this project applies
everywhere (M16 row 551's Alpaca-dashboard check is the direct precedent).

**Donor PII scope (staff-engineer-reviewer finding).** A restricted,
read-only Stripe key genuinely can't create refunds/payouts, but reading
charges/PaymentIntents still exposes donor PII (name, email, sometimes
address) at the API level, beyond what the output file needs. `donations.py`
only ever computes and writes the aggregate
(`{generated_at, total_donated_cents, count}`) — it never logs or persists
individual charge/customer objects, and GitHub Actions run logs for this
step must not print per-donor data either (worth an explicit check at
implementation time, not just an intent stated here).

### 5.3 Page copy (placeholder, pending legal review — see §2)

Draft only, not final:

> Donations are gifts, not investments. They carry no promise of any
> return, no share of profits, and no ownership stake in this account. Not
> tax-deductible. [Attorney-reviewed final wording replaces this line.]

### 5.4 If the legal review requires changing the model after donations have already been collected (pm-reviewer finding)

Not resolved here — genuinely the user's call, informed by the actual
attorney conversation, not something an engineering design doc should
pre-decide. Named explicitly so it isn't silently unaddressed: if the
legal review (already flagged as having real open questions in the
separate research briefing, not a formality) concludes the "pure gift"
framing or the page's real-time-performance-next-to-donate-button pattern
needs to change, what happens to money already collected under the old
framing is a decision to make *at that time*, with the attorney, not
something this doc can respond to in advance.

---

## 6. Resolving the M16/DESIGN.md §3.8 precondition

`DESIGN.md` §3.8 required this exact decision to be re-made before any
paper→live flip: does the public site showing real financial data change
anything about its exposure?

**Superseded (2026-08-10, user decision, after the page was already live
and verified working):** the original answer here was "yes, real numbers
go public, deliberately" — reasonable when the funding model was still
public donations (§0.1's now-dropped M20b) and transparency was the
entire point. With M20b dropped and the account self-funded, that
rationale no longer applies, and the user decided `real-money.html`
should be gated behind a login instead. Original reasoning kept below for
the historical record.

**Superseded again (2026-09-04, M44, user decision):** Cloudflare Access
was configured (both Access Applications created, policy scoped to the
user's own email) but had never actually taken effect for ~3 weeks,
blocked on a standing Cloudflare zone-level defect (proxied DNS records
silently not applying on this specific zone, `TASKS.md`'s "Cloudflare
`gramunger.com` zone silently refuses new subdomains" row). Rather than
continue waiting on a platform bug with no owner or ETA, the user asked
for a real login instead. **New mechanism: an oauth2-proxy sidecar +
Google OIDC, gating `real-money.html`/`real_money.json` at the nginx
layer via `auth_request`**, restricted to the same one email Access was
already scoped to.

**Real-world postscript, found live during this milestone's own
deployment (2026-09-04), not assumed:** the standing zone defect this
section describes appears to have resolved itself sometime in the ~3
weeks since it was last confirmed broken — a fresh, unauthenticated
request to both gated paths now correctly redirects to a genuine
`cloudflareaccess.com` login challenge, meaning Access is no longer
inert. This was not expected or relied on when the oauth2-proxy work
below was built, and doesn't replace it — it means the two paths are
currently gated by **both** mechanisms stacked (Access at Cloudflare's
edge, oauth2-proxy behind it at the app layer), both scoped to the same
one email, which is redundant but not conflicting. Left as-is rather than
torn out: removing Access now would depend on trusting a platform bug
that was broken and unowned for weeks to stay fixed, which isn't a
trade worth making for the sake of one fewer layer in front of a live
money account.

This does cost the one thing the Cloudflare Access plan was chosen to
avoid — a real, if small, code/deploy change (§1.1's static-site-only
constraint bends here, deliberately: `report.py`/`report/` itself stays
untouched and still emits no server-side logic; the new logic lives
entirely in nginx config + a second, unmodified upstream container, not
in this project's own Python). In exchange it doesn't depend on any
Cloudflare account state at all, works today, and delegates real
authentication (password, 2FA) to the user's existing Google account
rather than inventing a new credential.

- **Provider: Google OIDC** (`oauth2-proxy --provider=google`),
  restricted to exactly `jimmyokusa@gmail.com` via
  `--authenticated-emails-file` (a single-line allowlist, stored as a GCP
  Secret Manager secret and mounted as a file — not a broader
  `--email-domain`, which would admit any Google account on that domain).
  The Google OAuth Client itself is a manual, one-time user-side step
  (Google Cloud Console, project `munger-503515`, redirect URI
  `https://gramunger.com/oauth2/callback`), left in **Testing** consent-
  screen status with the user's own email as the sole test user — this
  keeps it out of Google's app-verification process entirely, which is
  only required for consumer-facing published apps.
- **Mechanism: nginx `auth_request`**, the pattern oauth2-proxy's own
  docs document for exactly this case (not improvised) — nginx makes a
  subrequest to oauth2-proxy before serving a gated path; an
  unauthenticated request gets redirected through `/oauth2/sign_in` to
  Google and back. See `deploy/cloudrun/report-web/nginx.conf` for the
  `/oauth2/`, `/oauth2/auth`, and `@oauth2_signin` blocks (verified
  locally against a real oauth2-proxy v7.15.4 binary, not just read from
  the docs: both `/real-money.html` and `/real_money.json` correctly
  redirect an unauthenticated request all the way to a genuine Google
  authorization URL with the right client ID and a `state` param
  encoding the original path).
- **Cloud Run: multi-container (sidecar) deployment.** oauth2-proxy
  (upstream `quay.io/oauth2-proxy/oauth2-proxy` image, unmodified, not
  built by this repo) runs alongside the existing nginx container in the
  same `report-web` service, reachable only via `127.0.0.1:4180` from
  nginx — never exposed externally. `gcloud run deploy`'s flags can't
  express a second container, so this target moved to a service spec
  (`deploy/cloudrun/report-web/service.yaml`) applied via
  `gcloud run services replace`; `deploy/cloudrun/deploy.sh` was updated
  to branch on this for the `report-web` target specifically, while still
  building and digest-pinning the nginx image exactly as before.
- **Secrets in GCP Secret Manager**, not plain env vars — the one place
  this departs from the project's usual manual-`--set-env-vars`
  convention, specifically because these secrets gate the live account's
  own page: the Google OAuth Client ID/Secret and the oauth2-proxy cookie
  secret (`openssl rand`-generated, not user-provided). The Cloud Run
  service account was granted `secretAccessor` on each, narrowly, same
  scoping discipline as every other GCS/IAM grant this project has made.
- **Both gated paths, not just the HTML one** — same gotcha the
  Cloudflare Access design already found and is preserved here:
  `real-money.html` (hyphen) and `real_money.json` (underscore) don't
  share a path prefix, so both need their own `auth_request` block or
  the JSON stays readable with the HTML page locked down.
- **Cookie hardening:** `--cookie-secure=true`, `--cookie-httponly=true`
  (default), `--cookie-samesite=lax`, `--cookie-expire=24h`.
- **k3s dev: explicitly out of scope, not silently skipped.** The k3s
  report is served over plain HTTP on a bare NodePort IP, with no stable
  hostname — there's no valid OAuth redirect URI to register there without
  first standing up TLS + a real hostname for it, a bigger, separate
  change. k3s is LAN-only, not internet-exposed, so this is an accepted
  gap for now, not a security hole in the actual public deployment.

**Original reasoning (superseded above, kept for the record):**

- "Yes, real numbers go public, deliberately" — the whole premise of
  this feature is a transparent, publicly-visible real-money track record
  funded by public gifts. This was an informed choice, not an oversight,
  at the time it was made.
- The exposure was informational only, not a control/credential risk —
  the public deployment never holds Alpaca credentials (§1.2) and the
  page is read-only-rendered from a snapshot file; nothing on the site
  ever let a reader act on the account.

---

## 7. Open questions / what's gated on what

- **§5/M20b (donations) and the entire ads/affiliate/$100k-target
  reframing are DROPPED — see §0.1.** No longer gated, no longer open;
  simply not being built, on legal advice. Left here only so a reader of
  this section's history understands why the M20b-shaped questions below
  (originally written when M20b was still active) are now historical.
- **pm-reviewer pass on this doc's scope — complete (2026-08-10).**
  Findings folded in throughout: the M20a/M20b phasing split above, §4.1/
  §5.2's verification-before-live criteria, §5.0's technical gate on the
  donate section, §5.2's refund/chargeback handling and the
  total-donated-vs-account-equity distinction, §3.3's interim blast-radius
  default, and this section's own owner/timing additions below.
- **staff-engineer-reviewer pass on this doc's architecture — complete
  (2026-08-10).** Found the state-isolation gap this section flagged as a
  risk to scrutinize for was, in fact, present: the first draft's
  git-branch/GCS-prefix separation didn't hold without also isolating
  local `DATA_DIR`, and `journal.db` had no account column to fall back on
  if isolation were ever imperfect. Fixed throughout §3: separate GitHub
  Actions *workflow file* (§3.1, genuine runner/checkout isolation, not
  `MUNGER_DATA_DIR` gymnastics inside one job), a code-level global kill
  switch (§3.2, escalated from runbook-only), an `account` column added to
  the journal schema plus an account-tagged `client_order_id` as
  independent defense-in-depth (§3.4/§3.5), and reconciliation-mismatch
  handling decided as abort-not-warn for live (§3.3). Also found and fixed
  a concrete bug in §4's reuse claim (hardcoded "paper" strings and a
  hardcoded poll-fetch target that would have silently displayed the
  wrong account's data), a missing nginx alias, and an unaddressed GCS IAM
  scoping gap. **Both review gates are now complete; this doc is ready to
  build against**, subject to the still-open user-side items below.
- **New secrets provisioning** (`ALPACA_LIVE_API_KEY`/`ALPACA_LIVE_SECRET_KEY`,
  a Stripe restricted read-only key) — a user-side step, not something this
  session can do. **Owner/timing (pm-reviewer finding): resolved by the
  user before M20a's first live order** — same deadline as the starting
  account size below, not an indefinite "eventually."
- **Not decided in this doc, owner and timing named (pm-reviewer
  finding):** starting live-account size, the absolute per-order/
  per-account dollar ceiling (§3.3), and whether the live leg runs on the
  same daily cadence as paper or something more conservative initially —
  all the user's call, **resolved before M20a's first live order** (§3.3's
  interim default applies until then).
