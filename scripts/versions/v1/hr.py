"""Haryana RERA extraction prompts — version v1 (2026-08-19)."""

_SCHEMA = """
OUTPUT SCHEMA (raw JSON, no markdown fencing, no explanation):
{
  "doc_type": "architect_certificate",
  "certificate_date": "string | null",
  "confidence": "high" | "low",
  "confidence_note": "string | null",
  "project": {
    "name": "string | null",
    "rera_id": "string | null",
    "builder": "string | null",
    "location": "string | null"
  },
  "certifying_architect": "string | null",
  "certified_towers": ["string"],
  "tables_a": [
    {
      "tower_labels": ["string"] | null,
      "tower_type": "building" | "wing" | "tower" | "block" | "phase" | "plot" | "other" | null,
      "aggregated": true | false,
      "work_items": [
        {
          "activity": "string",
          "is_category": true | false,
          "parent_category": "string | null",
          "values": [
            {
              "tower": "string",
              "proposed": true | false | null,
              "pct": number | null,
              "status": null | "na" | "not_started",
              "remarks": "string | null",
              "planned_start": "string | null",
              "planned_end": "string | null",
              "actual_start": "string | null",
              "actual_end": "string | null"
            }
          ]
        }
      ]
    }
  ],
  "table_a2": [
    {
      "tower_labels": ["string"] | null,
      "tower_type": "building" | "wing" | "tower" | "block" | "phase" | "plot" | "other" | null,
      "aggregated": true | false,
      "work_items": [
        {
          "activity": "string",
          "is_category": true | false,
          "parent_category": "string | null",
          "values": [
            {
              "tower": "string",
              "proposed": true | false | null,
              "pct": number | null,
              "status": null | "na" | "not_started",
              "remarks": "string | null",
              "planned_start": "string | null",
              "planned_end": "string | null",
              "actual_start": "string | null",
              "actual_end": "string | null"
            }
          ]
        }
      ]
    }
  ] | null,
  "schedule_tables": [
    {
      "tower_labels": ["string"] | null,
      "work_items": [
        {
          "activity": "string",
          "planned_start": "string | null",
          "planned_end": "string | null",
          "actual_start": "string | null",
          "actual_end": "string | null"
        }
      ]
    }
  ],
  "table_b": {
    "work_items": [
      {
        "activity": "string",
        "is_category": true | false,
        "parent_category": "string | null",
        "proposed": true | false | null,
        "pct": number | null,
        "status": null | "na" | "not_started",
        "remarks": "string | null",
        "planned_start": "string | null",
        "planned_end": "string | null",
        "actual_start": "string | null",
        "actual_end": "string | null"
      }
    ]
  } | null,
  "missing_towers": ["string"],
  "extraction_notes": ["string"]
}

FIELD RULES:
- pct: numeric only (70, not "70%"). null when status is non-null.
- status: "na" = Not Applicable, "not_started" = not yet started, null = normal percentage.
- Dates: preserve as written in the document ("Jan 2024", "01/2024", etc.).
- missing_towers: empty list [] if certified_towers could not be extracted.
- is_category: true for rows that act as grouping headers (e.g. "MEP", "Finishing");
  false for leaf work items. A category row may or may not have its own pct.
- parent_category: the grouping row's activity name this item falls under; null if top-level.
- tower_type: infer from context — "tower", "wing", "block", "building", "phase", "plot",
  "other", or null if cannot determine. Set per tables_a / table_a2 entry.
- proposed: true = planned/applicable; false = explicitly not planned (no conflicting pct
  evidence); null = no proposed column present or cannot be determined.
- confidence: "high" if extraction is reliable; "low" if page ordering is suspect or data is
  discontinuous and could not be fully resolved. Set confidence_note to explain.
- table_a2: null if no A2 section is present in the document.
- Note: Table C does NOT exist in Haryana — omit it entirely.
"""

_RULES = """
DOCUMENT TYPE
Haryana RERA certificates are called "Architect Certificate" — not Form 1 or Form 8.
Always set doc_type to "architect_certificate".
Scan both paragraphs and tables to locate all required fields.

TABLE IDENTIFICATION
If "Table A" / "Table B" labels are absent, identify by content:

  Table A — building/tower construction activities:
    Sub-structure: excavation, foundation, basement, waterproofing.
    Super-structure: RCC frame, slabs (floor-wise), walls, staircase, lift lobbies.
    MEP: Mechanical, Electrical, Plumbing.
    Finishing — External: external plaster, paint, facade, cladding, terrace.
    Finishing — Internal: internal plaster, tiling, flooring, painting, false ceiling.

  Table B — common areas and development works:
    Internal roads, pavement, parking, water supply, sewage/drains, storm water drains,
    landscaping, garden, playground, benches, shopping area, street lights,
    waste management, energy/solar management, fire protection, sub-station, meter room,
    community centre, schools, dispensary, club, etc.

Table C does NOT exist in Haryana. If content labelled "Table C" appears, ignore it.

A table that only contains start/end dates with no percentage column is a schedule table
— output it in schedule_tables, not in tables_a or table_a2.

IGNORE: tables or paragraphs about documents to be uploaded, cost/funding, inventory,
payment schedules, or other non-construction-progress content.

PRIORITY: activity name and pct are primary. Dates, remarks, and work counts are
secondary — capture them when present but do not fail or skip a row if they are absent.

TABLE A STRUCTURE: A1 (CUMULATIVE) AND A2 (DETAILED BREAKDOWN)
Table A in Haryana almost always has two parts. They may or may not be labelled.

A1 — Cumulative progress table. Identifies as:
  - Labelled "A1", "Cumulative Progress", "Overall Progress", or similar.
  - Contains exactly the 4 main categories (or a subset if phases not yet started):
      Sub Structure, Super Structure,
      MEP  (with 3 sub-items: Mechanical, Electrical, Plumbing),
      Finishing  (with 2 sub-items: External Finishing, Internal Finishing).
  - Usually a short table — roughly 6–8 rows.
  - Does NOT contain fine-grained rows like "Excavation" or "Laying of Foundation".
  → Output in tables_a.

A2 — Detailed breakdown. Identifies as:
  - Labelled "A2", "Tasks/Activity", "Detailed Progress", or signalled by a re-appearing
    bold column header (e.g. "S.No | Task/Activity | % complete") in the middle of a table.
  - Contains granular activities: excavation, laying of foundation, total floors in building,
    slab floor-wise, walls, staircase, lift lobbies, plumbing fixtures, electrical wiring, etc.
  - Usually a longer table — many rows within each category.
  → Output in table_a2.

Decision when labels are absent:
  - Content is 4-category structure with sub-items → A1 → tables_a.
  - Content has granular activity rows (e.g. "Excavation") → A2 → table_a2.
  - If both appear in the document but are unlabelled, use a re-appearing column header
    row as the boundary between A1 and A2.
  - Note the identification method in extraction_notes.

Presence variants:
  - Both A1 and A2 present → populate tables_a and table_a2.
  - Only A1 → tables_a, table_a2: null.
  - Only A2 → tables_a: [], table_a2: [one entry].
  - Neither → tables_a: [], table_a2: null.

AGGREGATION IN HARYANA
Table A (both A1 and A2) is almost always a single aggregated table for the whole project
— not one table per tower. Set aggregated: true; set tower_labels to the project name or
block group if stated, or null if not given. Individual per-tower tables are rare in Haryana;
if present, model them as separate entries as you would for any other state.

CATEGORY ROWS
Tables include grouping rows (often bold or with no pct) that head a set of sub-items.
  Examples: "MEP" groups Mechanical, Electrical, Plumbing.
            "Finishing" groups External Finishing, Internal Finishing.
  Rule: include the grouping row as a work_item with is_category: true, parent_category: null.
  Set each sub-item's parent_category to the grouping row's activity name, is_category: false.
  Include the grouping row AND each sub-item in work_items in document order.
  If the grouping row carries its own pct, capture it; if it is just a header, set pct: null.

TOWER TYPE
Infer tower_type from the tower label and surrounding text. Set per tables_a / table_a2 entry.
  "Tower A", "T-1" → "tower"
  "Wing 1", "Wing A" → "wing"
  "Block A", "Block 1" → "block"
  "Building 1", "Bldg 2" → "building"
  "Phase 1", "Phase II" → "phase"
  "Plot 12" → "plot"
  Cannot determine → null.

PROPOSED COLUMN
If a "Proposed" / "Applicable" / "Planned" column is present, interpret it per row:
  - Proposed = No / NA / Not Applicable → proposed: false.
    Exception: if pct > 0 in the same row, override to proposed: true.
  - Proposed = Yes / Applicable / ✓ → proposed: true.
  - Proposed = Yes with pct column blank / "--" / null → proposed: true, pct: null,
    status: "not_started".
  - No Proposed column present → proposed: null for all rows.

TABLE B ARTEFACTS
The same extraction artefacts that affect Table A also apply to Table B:
  - Serial-number column merged into the activity column.
  - Two data columns merged into one cell.
  - A row spanning all columns is a section header, not a work item.
  - Category rows (is_category: true) may appear in Table B as well.

EXTRACTION ARTEFACTS (all tables)
- Serial-number column merged into activity: "| 2 Excavation |" → strip serial, keep "Excavation".
  Do not strip numbers that are part of the activity name ("0 number of podiums" stays).
- Two data columns merged into one cell ("Excavation 100") → split them.
- A row repeating the same label in all columns (e.g. "| A | A | A | A |") is a tower header
  — extract the label once, do not treat it as a work item.
- tower_labels: use null (not an empty list) when the label cannot be determined.
- remarks: capture count-based information verbatim (e.g. "12 out of 20 complete").

LANDSCAPE / WIDE TABLES
Tables too wide for portrait may be printed landscape or — in scanned PDFs — rotated 90°/−90°.
In markdown extractions, wide tables may appear broken. Reconstruct from column headers and
activity content. Note any reconstruction in extraction_notes.

PAGE CONTINUITY CHECK (SCANNED PDFS)
Poorly scanned Haryana documents may have pages out of order. After reading all pages:

  1. Check serial number (S.No) continuity at every page boundary.
     Discontinuity example: page 1 ends at S.No 5, page 2 starts at S.No 13.
     Recovery: check whether another page starts at S.No 6 and ends at S.No 12 — if so,
     those pages were mis-ordered. Re-order logically and extract.

  2. Check table/column header continuity: each page of a multi-page table should
     continue the same columns.

  3. Check paragraph flow: a sentence cut mid-way on one page should resume naturally
     on the following page.

Set confidence after checking:
  "high" — no discontinuity detected, or detected discontinuity was resolved by re-ordering.
  "low"  — discontinuity detected and could NOT be resolved. Set confidence_note explaining
           which pages and serial-number ranges are inconsistent.
           Still extract whatever is reliably recoverable.

CERTIFIED_TOWERS
Extract from the opening section where the architect names the specific
towers/wings/blocks/plots they are certifying in this report. If the certificate
covers the whole project collectively, use ["All"] or the project name as the single entry.

MISSING_TOWERS
After extracting all tables_a entries, compare tower_labels against certified_towers.
List any certified tower with no matching tables_a entry.
If certified_towers is empty, set missing_towers to [].

EXTRACTION_NOTES
Record: A1/A2 identification method, guessed tower labels, table type inferred from content,
page reordering applied, corrected merged columns, page-break losses, landscape-table
handling, category rows identified, proposed-column overrides, or any other assumption made.
"""

STRUCTURE_SYSTEM = f"""\
You extract structured data from Indian RERA (Real Estate Regulatory Authority)
construction progress certificates for Haryana state.

These certificates are called "Architect Certificates". They may be text-based PDFs
(given as extracted markdown) or scanned PDFs (given as raw pages). Always scan
both paragraphs and tables to locate all required fields.

{_SCHEMA}

{_RULES}
""".strip()

STRUCTURE_USER = """\
Extract structured data from the RERA construction certificate below.
Output only raw JSON — no markdown fencing, no explanation.

---
{markdown_content}
---
"""

SINGLE_CALL_SYSTEM = f"""\
You extract structured data from Indian RERA (Real Estate Regulatory Authority)
construction progress certificates for Haryana state.

The input is a scanned PDF. Read all pages carefully — including table contents,
headers, stamps, and any handwritten annotations. Watch for pages that are
rotated 90°/−90° (landscape tables printed sideways). Also watch for pages
that may be out of order — check serial number and table continuity across pages.

{_SCHEMA}

{_RULES}
""".strip()

SINGLE_CALL_USER = (
    "Extract structured data from this certificate. Output only raw JSON."
)
