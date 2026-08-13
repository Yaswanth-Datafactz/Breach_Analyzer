# Breach Analytics at Scale — Problem Statement

**DataFactZ AI Engineering Internship — Use Case 3 (Capstone)**
**Author:** Yaswanth Thottempudi | **Date:** August 14, 2026

## Business framing

When a client suffers a data breach, the first question from their legal team is never "how did
it happen." It is: **who is affected, and what of theirs was exposed.** That answer has to hold
up to a regulator, and it has to arrive inside a notification deadline measured in days, not
months.

In practice, the breached data arrives as an unsorted dump — scanned PDFs, digital PDFs, Word
documents, spreadsheet exports, email threads with attachments, screenshots — sometimes 500
files, sometimes 500,000. Buried inside are the personal records of anywhere from a few hundred
to a few hundred thousand individuals, referenced inconsistently: full names, nicknames,
maiden names, partial identifiers split across documents, the same SSN typed three different
ways. A breach-response team doing this by hand trades accuracy for speed or speed for accuracy.
Neither is acceptable when the output has to survive legal and regulatory scrutiny.

Breach Analytics at Scale automates this triage: ingest the raw document dump, extract personal
data elements, resolve every mention to one unique individual, and produce a single exposure
table that legal can hand to a regulator without apology — every flag traceable to the exact
passage that justifies it.

## Who this is for

**Primary user:** the breach-response consultant or in-house privacy counsel who has to produce
the affected-individuals list and defend it under questioning. They are not a data scientist —
they need a table they can filter, search, and drill into, not a model output they have to
interpret.

**Secondary user:** the reviewer who spot-checks the system's judgment calls — an uncertain
entity match, a low-confidence extraction, an exception the pipeline could not resolve on its
own — and either confirms or corrects them before the table goes final.

## What success looks like

A run is successful when:

1. **Every document reaches a terminal state.** Processed to completion, or quarantined with a
   named reason. Nothing is silently dropped — this is checked by a zero-row reconciliation
   query, not assumed.
2. **Every exposure flag has evidence.** No flag is ever asserted without at least one passage
   reference with character-offset anchoring back to a real source document. A flag with no
   evidence is worthless in front of a regulator, and the system enforces this in code, not by
   convention.
3. **Entity resolution never merges two different people.** Shared names, common surnames, and
   near-identical identifiers are the standard trap in this domain; the system is measured
   explicitly on whether it avoids it (the "wrongly-merged" headline metric), not just on raw
   recall.
4. **Accuracy and cost are both measured, not asserted.** Person-level precision/recall and
   per-category flag accuracy against a ground-truth manifest; real, logged cost per document,
   extrapolated honestly to 100K and 1M documents; at least two tiered-routing configurations
   compared on a cost/accuracy curve, with a recommendation.
5. **Judgment calls are visible and bounded.** Where the system cannot enumerate the right
   answer in advance — an unreadable file, an ambiguous entity match, a bulk-impact merge — an
   agent investigates within a hard budget, and every step of its reasoning is logged and
   reviewable. Nothing an agent decides is invisible or unlimited in cost.

## Out of scope (for this capstone, recorded honestly rather than silently dropped)

- **Incremental reprocessing** of a corpus after new documents arrive without a full rerun.
- **Active learning** from reviewer corrections back into extraction or matching thresholds.
- **Per-jurisdiction notification-letter generation** and multilingual document support.

These are named explicitly as future work in the design document rather than omitted without
comment — a defensible scope boundary is itself part of the deliverable.
