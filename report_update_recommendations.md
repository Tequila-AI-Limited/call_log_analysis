# Weekly Call Report - UX and Stakeholder Improvement Recommendations

## Purpose

The current weekly call report is functional and data-rich. It already gives stakeholders access to call volumes, retail/trade splits, abandonment metrics, out-of-hours analysis, charts, raw call samples, abandoned-call records, and CSV downloads.

The next improvement opportunity is not just adding more data. The bigger opportunity is making the report easier to scan, easier to act on, and easier to trust.

This document outlines practical UX-led improvements that would make the report more useful for senior stakeholders, operational managers, and customer-facing teams.

## Current Strengths

- The report is already automated from source data to email delivery.
- It includes the core weekly KPIs stakeholders care about: total calls, retail calls, trade calls, abandoned calls, and abandonment rates.
- It separates retail and trade call performance, which is important for operational decision-making.
- It includes out-of-hours analysis, which can support staffing and service-hours decisions.
- It includes abandoned trade customer detail, which is useful for follow-up.
- It provides downloadable CSVs for deeper analysis.
- The database persistence is designed to avoid duplicate records when the same report is rerun.

## Main UX Problem

The report currently behaves more like a data output than a decision tool.

Stakeholders can find the numbers, but they have to work to understand:

- What changed this week?
- Is the change good or bad?
- What needs attention?
- Which customers or operational areas need follow-up?
- How confident should they be in the figures?

The report should answer those questions in the first screen.

## Recommended Direction

Move the report toward an executive operating summary:

1. Start with the most important weekly changes.
2. Highlight exceptions and risks.
3. Show why the changes happened.
4. Provide follow-up lists for operational action.
5. Keep raw data available, but move it below the decision layer.

## Priority 1 - Add a Clear "What Changed?" Summary

### Recommendation

Add a top-level weekly movement panel comparing This Week against Last Week.

Example:

```text
This Week vs Last Week

Total calls:        2,410  (+67 / +2.9%)
Retail calls:       1,745  (+42 / +2.5%)
Trade calls:          382  (-11 / -2.8%)
Abandoned calls:      286  (+12 / +4.3%)
Abandonment rate:   11.9%  (+0.4 pp)
```

Use simple direction indicators:

- Green for improvement.
- Red for deterioration.
- Neutral grey for small/no change.

### Why It Matters

Stakeholders should not have to compare two cards manually. The report should tell them immediately whether the week improved, worsened, or stayed stable.

### Effort

Medium.

The underlying metrics already exist. The work is mainly adding delta calculations and template display logic.

## Priority 2 - Put Executive Summary Before Metric Cards

### Recommendation

Move the executive summary above the KPI card grid and rewrite it as short decision-focused bullets.

Suggested format:

```text
Executive Summary

- Call volume increased by 2.9% week on week.
- Retail abandonment rose to 14.6%, driven mainly by Monday and Tuesday peaks.
- Trade abandonment remained low, but 8 named trade customers abandoned calls and may need follow-up.
- Out-of-hours demand remains material, with most calls occurring after closing.
```

### Why It Matters

Most senior readers scan top to bottom. The first section should explain the report, not just present totals.

### Effort

Low to medium.

The existing narrative function can be reworked without changing the data pipeline.

## Priority 3 - Add "Needs Attention" Flags

### Recommendation

Add an exceptions panel near the top of the report.

Example:

```text
Needs Attention

High retail abandonment: 14.6%
After-hours demand: 184 calls
Repeat abandoned trade customers: 5
Peak waiting-time day: Monday
```

Flags should be based on simple thresholds agreed with stakeholders.

Possible thresholds:

- Retail abandonment rate above target.
- Trade abandonment rate above target.
- Out-of-hours calls above a weekly threshold.
- Any named trade customer abandoned more than once.
- Average wait time above target.

### Why It Matters

This turns the report from a passive dashboard into an operational triage tool.

### Effort

Medium.

Requires agreed thresholds and template changes.

## Priority 4 - Separate Executive View from Detail View

### Recommendation

Restructure the report into clear sections:

1. Executive Summary
2. Weekly KPI Movement
3. Operational Exceptions
4. Charts and Trend Analysis
5. Customer Follow-Up Lists
6. Raw Data and Downloads

The raw data tables should remain available, but they should not compete with the executive summary.

### Why It Matters

Different stakeholders use the report differently. Senior stakeholders need the answer quickly. Operations teams need the details. The report should support both without forcing everyone through the same dense layout.

### Effort

Low.

Mostly template reordering and headings.

## Priority 5 - Improve KPI Card Design

### Recommendation

Reduce repeated card grids and make each card answer one complete question.

Instead of separate cards for "This Week Trade" and "Last Week Trade", use comparison cards:

```text
Trade Calls
382
-11 vs last week
```

```text
Retail Abandonment
14.6%
+0.8 pp vs last week
```

Include:

- Current value.
- Change versus last week.
- Direction of change.
- Optional target indicator.

### Why It Matters

The current cards show values, but they do not explain movement. Comparison cards reduce cognitive load.

### Effort

Medium.

Requires metric deltas and template updates.

## Priority 6 - Add Targets or Benchmarks

### Recommendation

Agree target thresholds for the metrics that matter most.

Suggested initial targets:

```text
Retail abandonment rate target: < 10%
Trade abandonment rate target: < 5%
Average wait time target: < agreed threshold
Out-of-hours calls target: monitor only initially
```

Then show report status as:

```text
On target
Watch
Off target
```

### Why It Matters

A number by itself is hard to interpret. A number against a target is actionable.

### Effort

Medium.

Technically simple, but requires stakeholder agreement.

## Priority 7 - Make Trade Customer Follow-Up More Actionable

### Recommendation

Enhance the abandoned trade customer section into a follow-up list.

Add columns such as:

- Customer name.
- Phone number.
- Number of abandoned calls this week.
- Most recent abandoned call time.
- Previous week abandoned calls.
- Suggested action: "Call back", "Monitor", or "Recurring issue".

### Why It Matters

Trade customers are high-value. A report that identifies who needs follow-up is more useful than a report that only counts abandonment.

### Effort

Medium to high.

Some data exists already. More value would come from grouping repeat abandoned calls by customer/number and comparing across weeks.

## Priority 8 - Add a Data Confidence Section

### Recommendation

Add a small footer or final section showing:

```text
Data Quality

Source emails processed: 4
Attachments downloaded: 4
Unique call IDs processed: 2,410
Duplicate call IDs removed: 0
Report generated: 2026-06-02 22:43
Database save status: successful
```

### Why It Matters

Automated reports need trust. A short data-confidence section helps stakeholders know whether the report ran cleanly.

### Effort

Medium.

Some values are already logged; the report would need to receive them from the pipeline.

## Priority 9 - Improve Chart Storytelling

### Recommendation

Keep the interactive Plotly chart, but add short plain-English chart annotations above or beside it.

Example:

```text
Chart Takeaways

- Waiting time peaked on Monday.
- Talking time remained stable across the week.
- Abandoned calls were concentrated on Tuesday and Friday.
```

Also consider splitting the current combined chart into smaller focused charts:

- Waiting time by day.
- Talking time by day.
- Abandoned calls by day.

### Why It Matters

Interactive charts are useful, but stakeholders should not have to inspect every hover tooltip to understand the message.

### Effort

Medium.

The chart data already exists; the main work is summarising the key trends.

## Priority 10 - Make the Email Body More Useful

### Recommendation

The report email should include a short summary, not just an attachment.

Example:

```text
Hi all,

This week's call report is attached.

Headline:
- Total calls: 2,410 (+2.9% vs last week)
- Abandoned calls: 286 (+4.3%)
- Retail abandonment: 14.6%
- Trade abandoned customers needing follow-up: 8

Regards
```

### Why It Matters

Many stakeholders will read the email preview before opening the report. The email should carry the headline.

### Effort

Low to medium.

The email function already supports a summary parameter, but the pipeline does not currently populate it.

## Suggested Implementation Roadmap

### Phase 1 - Quick UX Wins

Estimated effort: 1 day.

- Move executive summary to the top.
- Rewrite summary into concise bullets.
- Add This Week vs Last Week deltas.
- Add a simple "Needs Attention" section.
- Improve report email body with headline metrics.

### Phase 2 - Operational Actionability

Estimated effort: 1-2 days.

- Add threshold-based flags.
- Improve abandoned trade customer follow-up list.
- Add repeat abandoned customer grouping.
- Add chart takeaway bullets.
- Add data confidence footer.

### Phase 3 - Stakeholder Dashboard Quality

Estimated effort: 2-4 days.

- Redesign KPI card layout.
- Add targets/benchmark status.
- Split executive and operational detail sections.
- Add trend history from the database.
- Add a cleaner mobile/email-friendly layout.

## Stakeholder Decisions Needed

Before implementing the full version, stakeholders should agree:

- Which KPIs are most important?
- What are acceptable abandonment-rate targets for retail and trade?
- Should trade customers be treated with separate priority?
- Who owns follow-up on abandoned trade customer calls?
- Should out-of-hours demand be treated as a staffing/service-hours issue?
- Should the report include a RAG status: red, amber, green?
- Should the report email include headline figures in the body?

## Recommended Next Step

Start with Phase 1.

It would make the report noticeably easier to read without changing the core analytics logic. The report would still use the same metrics and database pipeline, but the stakeholder experience would improve immediately.

The most valuable first change is:

```text
Executive Summary + Weekly Movement + Needs Attention
```

That gives stakeholders the answer first, then lets them drill into the charts and raw details if needed.
