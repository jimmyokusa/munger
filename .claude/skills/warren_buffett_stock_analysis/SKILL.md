---
name: warren_buffett_stock_analysis
description: >-
  Performs a rigorous, Warren Buffett-style fundamental analysis on a public
  company: business understandability, economic moat, management quality &
  CEO track record, financial health, and intrinsic value with a margin of
  safety. Use when asked to evaluate a specific ticker as a potential
  investment, not for reviewing munger's own screening code or thesis (that's
  the warren-buffett agent).
---

# Warren Buffett stock analysis

Evaluate a single public company the way Warren Buffett would: as a business
to be owned for decades, not a ticker to be traded. Work through the five
steps below in order and produce the markdown report at the end.

## Required inputs

Before starting, confirm you have (or can gather, e.g. via WebFetch/WebSearch
if the caller didn't supply them):

- **ticker** — stock ticker symbol (e.g. "AAPL", "KO")
- **financial_data** — 10-year historical financial statements (income
  statement, balance sheet, cash flow statement)
- **management_data** — executive bios, insider ownership, capital allocation
  history, CEO reputation (shareholder letters, transcripts, Glassdoor/industry
  standing)
- **current_price** — current market stock price

If any of these are missing or too thin to support a real judgment, say so
explicitly in the report rather than guessing.

## Step 1: Circle of Competence & Business Simplicity

- Determine whether the business model is straightforward and predictable.
- Try to explain the core revenue engine in two simple sentences. If you
  can't, that's itself a finding.
- Penalty flag: highly speculative tech, frequent business-model pivots, or
  opaque financial products.

## Step 2: Economic Moat Assessment (Durable Competitive Advantage)

Identify whether the company has at least one durable moat mechanism:

1. **Brand power** — pricing power without losing volume.
2. **High switching costs** — painful or expensive for customers to leave.
3. **Network effects** — value grows with each new user.
4. **Cost advantage** — scale or geography peers can't match.

Check gross margin stability over the last 10 years as supporting evidence:
fluctuation under ~5 points suggests real pricing power / a strong moat.

## Step 3: Management & CEO Evaluation

Analyze leadership on three pillars:

**A. Integrity & candor**
- Read shareholder letters and earnings-call transcripts for a pattern:
  does the CEO openly own mistakes, or reflexively blame macro factors?
- Are GAAP earnings routinely dressed up with non-standard / adjusted
  metrics?

**B. Rational capital allocation**
- How is excess cash actually deployed? Rank in order of preference:
  (1) high-ROIC internal reinvestment, (2) value-accretive acquisitions,
  (3) share buybacks executed *below* intrinsic value, (4) dividends.
- Red flag: buybacks executed while the stock trades at all-time-high
  valuation multiples — that's capital destruction, not return.

**C. Alignment ("skin in the game")**
- Does the CEO/executive team hold substantial equity (rule of thumb:
  >1–2% of shares outstanding, or a multi-year-salary equivalent)?
- Is compensation tied to ROIC/ROE, or to revenue growth and stock-price
  momentum (the wrong incentive)?
- Note CEO reputation signals (employee satisfaction, Glassdoor, industry
  standing) as a secondary integrity check.

## Step 4: Financial Strength & Quality Test

Verify these thresholds over a 10-year period:

| Metric | Buffett target |
|---|---|
| Return on Equity (ROE) | Consistently > 15%, without excessive leverage |
| Return on Invested Capital (ROIC) | Consistently > 12–15%, and must exceed WACC |
| Debt-to-Equity | < 0.50 (or the conservative equivalent for the industry) |
| Interest coverage | > 5x |
| Free Cash Flow | Positive in at least 8 of the last 10 years |
| Retained earnings efficiency | $1.00 retained has created ≥ $1.00 of market value over 10 years |

## Step 5: Intrinsic Value & Margin of Safety

- Estimate **Owner Earnings**:
  `Net Income + Depreciation/Amortization − Maintenance CapEx ± Working Capital Changes`
- Project a conservative 10-year FCF growth rate — cap it at roughly
  GDP growth + inflation for a narrow moat, and at most 8–10% even for a
  wide moat.
- Discount at the 10-Year Treasury yield plus an equity risk premium
  (typically a 9–10% total discount rate).
- Calculate Intrinsic Value per share from the resulting DCF.
- Require a margin of safety: **Target Entry Price = Intrinsic Value × 0.70**
  (a minimum 30% discount to intrinsic value).

## Output

Produce a markdown report in this shape:

```markdown
# Warren Buffett Stock Analysis: {ticker}
**Current Price:** ${current_price} | **Target Entry Price (30% MoS):** ${target_price}
**Recommendation:** [STRONG BUY / WATCHLIST / PASS]

## 1. Business & Moat Analysis
- **Business Model Simplicity:** [High / Medium / Low]
- **Moat Type:** [Brand / Switching Costs / Network Effects / Cost / None]
- **Moat Durability Score:** [1-10]

## 2. CEO & Management Quality
- **Capital Allocation Grade:** [A / B / C / D / F]
- **Skin in the Game / Insider Ownership:** [Details]
- **Candor & Executive Reputation:** [Details]

## 3. Financial Health (10-Yr Track Record)
| Metric | Company Avg | Buffett Target | Pass/Fail |
|---|---|---|---|
| ROE | {roe}% | > 15% | {roe_pass} |
| ROIC | {roic}% | > 12% | {roic_pass} |
| Debt/Equity | {de_ratio} | < 0.50 | {de_pass} |
| FCF Yield | {fcf_yield}% | > 5% | {fcf_pass} |

## 4. Valuation & Margin of Safety
- **Intrinsic Value per Share:** ${intrinsic_value}
- **Margin of Safety:** {margin_of_safety}%
- **Final Assessment:** [Summary narrative]
```

Recommendation logic: **STRONG BUY** only if the moat is real (Step 2),
management passes on all three pillars (Step 3), the financial thresholds
in Step 4 are met, and current price is at or below the target entry price.
**WATCHLIST** if the business and management pass but price hasn't reached
the margin-of-safety threshold. **PASS** if any of the moat, management, or
financial-quality checks fail — a discount cannot fix a bad business.
