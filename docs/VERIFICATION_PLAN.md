# Verification plan — checking the extracted BOQ against the drawing

For the session with the civil team. Budget **45–60 minutes** for one A0 sheet.

The purpose is not to decide whether the tool is clever. It is to answer one
question per quantity: **would you sign this number?**

---

## 1. What to have in front of you

| Artefact | Where it comes from | Why |
|---|---|---|
| The drawing, A1 or A0 print | your usual print | the reference |
| `*_check.png` — check print | written beside the workbook | the drawing with every extracted value boxed, numbered and colour-coded |
| The workbook, **Verification** sheet | 11th sheet | one row per extracted value, with sign-off columns |
| The workbook, **Derivation** sheet | present if quantities were derived | the arithmetic behind everything not read off the drawing |

Generate all of it with:

```bash
venv/bin/python run_pipeline.py --derive all --audit-sheet --prepared-by "<your name>"
```

Colours on the check print: **green** high confidence, **orange** medium,
**red** low or conflicting. Start with red and orange.

---

## 2. How to check — in priority order

Work down the Verification sheet. Every row carries the **verbatim callout** the
model read and a **grid reference** (e.g. `D-7`) locating it on the sheet border,
so nobody has to hunt.

### Tier A — must be right (10 min)

1. **Title block** — drawing no, revision, date, project. Wrong here means the
   whole workbook is mislabelled.
2. **Grade slab extent** — `24430 × 8600 × 300`. **Check this hardest.** It is
   the only major quantity read off the *plan view* rather than a callout, and
   every derived quantity (excavation, blinding, liner, coating, curb, joints)
   is computed from it. A 5% error here moves the whole estimate by 5%.
3. **Pedestal callouts** — tag, both dimensions, quantity, for each of P1…P7.
   Compare against the callout text quoted in the row.

### Tier B — the known gap (10 min)

4. **Pedestal heights.** These are **not printed on the callouts** and are
   **not extracted**. The workbook uses a flagged `0.800 m` placeholder and says
   so in red on the Summary sheet. Take the real heights off the sections and
   write them in the `Correct value` column. Until this is done, pedestal
   concrete, formwork and rebar are wrong.

### Tier C — assumptions, not readings (15 min)

5. **Derived quantities** — read the Derivation sheet and challenge each
   assumption against the project spec / method statement:

   | Rule | Default assumed | Check against |
   |---|---|---|
   | Excavation | 300 mm working space each side | spec / shoring method |
   | Excavation depth | slab + blinding + fill | section detail |
   | Blinding | 100 mm offset, 75 mm thick | typical section |
   | Fill / sub-base | 150 mm | geotech recommendation |
   | Epoxy | slab top area × 2 coats | the "4MM THK ACID RESISTANT" note |
   | Curb wall | run = slab perimeter | plan — is it continuous? |
   | Contraction joints | 5 m panel grid | ACI 330R / the joint layout drawing |

   Change the numbers on the **Input** page and regenerate; the formulas update.

### Tier D — what is missing (10 min)

6. Walk the drawing for anything the BOQ does not contain. Expect to find:
   sump geometry, embedment schedule, joint layout as actually drawn, rebar
   bar-by-bar, and anything on companion sheets. The tool reports insert-plate
   types, epoxy specs, levels and rebar callouts under *"Also read — review
   only"*, but does **not** put them in the BOQ, because the drawing does not
   dimension them fully.

---

## 3. What to record

Fill the shaded columns of the Verification sheet:

* **Correct value (if wrong)** — write what it should be, not just a cross.
* **Verified by**, **Date** — per row, so a partial check is still useful.
* Sign the block at the bottom.

Then measure the result:

```
Tier A accuracy = correct Tier A rows / total Tier A rows
```

Track the **count of wrong values**, not just the percentage — one wrong
quantity on a large item matters more than three wrong small ones.

---

## 4. Acceptance criteria

Suggested thresholds to agree with the team *before* checking, so the result is
not argued after the fact:

| Level | Criterion | If not met |
|---|---|---|
| **Usable as a draft takeoff** | Tier A ≥ 90% correct, no wrong value on the slab | Fix and re-run; do not price from it |
| **Usable as an issued BOQ** | Tier A 100%, Tier B entered by an engineer, Tier C assumptions signed off | — |
| **Trustworthy for the next drawing** | Two consecutive sheets meet the above | Keep every extraction under review |

A wrong value is not a failure of the exercise — it is the exercise working.
What matters is that no wrong value reaches a priced BOQ unnoticed, which is
what the UNVERIFIED DRAFT banner and this sheet exist to prevent.

---

## 4b. Working a whole set

For several drawings at once, tick them on **Drawing → BOQ** (or run
`--batch Drawings`). You get `output/SET_BOQ.xlsx`:

* **Summary** — Sl. #, Description, UoM, then one quantity column per drawing,
  a Total, and an Assumptions column. Grouped by category, full drawing numbers
  printed under the short tab codes.
* **Detail sheets** — one per drawing per activity (`0107_Concrete`,
  `0107_Rebar`), so a checker can work one sheet at a time.
* **Shaded cells** — every quantity that is an assumption rather than a
  reading: derived from slab geometry, or resting on a placeholder pedestal
  height. Start the review there.

Each drawing also keeps its own workbook, with its own Verification sheet, for
checking against that one sheet.

**Expect some sheets to carry no quantities.** In this set, 0101 is a
sections/details sheet and 0102–0105 are foundation plans that mark pedestal
positions but reference their sizes to another drawing. The tool reports the
position-mark counts it can see (`P1 x19, P2 x14`) for review, but does not
turn them into quantities — an element drawn in both plan and section is
labelled twice. Confirming those counts is one of the more valuable things the
review can produce.

## 5. Feeding corrections back

0. **Mark up the Verification sheet.** Write the right number in *Correct
   value*, put your name in *Verified by* and the date. Leave *Correct value*
   blank on rows that are right — a blank with your name means "checked, agreed".
   Then hand the files back:

   ```bash
   venv/bin/python bin/ingest_corrections.py output/reviewed/*.xlsx
   ```

   That prints accuracy per item type and per drawing, banks fully-confirmed
   drawings as examples, and — most usefully — lists any callout the parser
   could not read. Those are permanent, cheap fixes.

1. Correct the numbers on **Drawing → BOQ** (the grid is editable) or on
   **Input**, then regenerate the workbook.
2. If the extraction was right, save it as a verified example:
   **Drawing → BOQ → Save as verified example**. Examples are injected as
   few-shot context for sheets in the same family, so later drawings in the
   `MD-522-8110-EG-…` series read better. Target is 10 drawings.
3. If it was wrong, note *how* — a misread digit, a missed callout, or a callout
   in a format the grammar does not know. The third kind is a one-line fix in
   `extractors/callout_grammar.py`.

**Do not save an unverified extraction as an example.** It becomes ground truth
for every later run.

---

## 6. Known limitations — check these hardest

* Pedestal **heights** are never extracted (§ Tier B).
* Grade slab is read from a **plan dimension**, not a callout — lower confidence
  than the pedestal rows.
* The extractor reads **one sheet at a time**. Concrete grades, rebar details and
  general specifications usually live on companion drawings.
* Anything **handwritten or red-lined** is ignored by design.
* A callout in an unusual format is **skipped rather than guessed** — so a
  missing item is more likely than a wrong one. Check for absences, not just
  errors.
