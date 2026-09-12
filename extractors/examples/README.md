# Verified extraction examples

One JSON per drawing whose extraction a human has checked against the sheet.
Retrieved by `extractors/rag_examples.py` and injected as few-shot context into
the pedestal prompt for sheets from the same family (dHash distance, or a shared
drawing-number prefix such as `MD-522-8110-EG-…`).

Written by:

* `pages/0_Extract.py` → "Save this extraction as a verified example"
* `run_pipeline.py --save-example`

**Only save an extraction you have actually checked.** These files are treated as
ground truth by every later run; a wrong one teaches the model to repeat the
mistake. Handoff §10 target: 10+ verified drawings.

Schema:

```json
{
  "drawing_no": "MD-522-8110-EG-CV-LAD-0107",
  "revision": "C01",
  "image_hash": "<16 hex chars, dHash of the sheet>",
  "verified_extraction": {
    "pedestals": [{"tag": "P1", "length_mm": 600, "width_mm": 500, "quantity": 2}]
  },
  "verified_by": "Johnson Andrew",
  "verified_at": "2026-09-07",
  "source_pdf": "MD-522-8110-EG-CV-LAD-0107_C01.pdf",
  "notes": ""
}
```
