"""
Prompts for Haryana RERA construction certificate extraction.

Four prompts — two variants, both with cacheable system prompts:

  2-call variant (text PDFs — markdown already on disk):
    STRUCTURE_SYSTEM  — system prompt (schema + rules)
    STRUCTURE_USER    — user message template; fill {markdown_content}

  Single-call variant (scanned PDFs — send raw PDF bytes):
    SINGLE_CALL_SYSTEM — system prompt (schema + rules, OCR-aware framing)
    SINGLE_CALL_USER   — user message sent alongside the PDF part

Usage — 2-call variant:
    response = client.models.generate_content(
        model=model,
        contents=[STRUCTURE_USER.format(markdown_content=md)],
        config=types.GenerateContentConfig(
            system_instruction=STRUCTURE_SYSTEM,
            temperature=0,
            response_mime_type="application/json",
        ),
    )

Usage — single-call variant:
    pdf_part = types.Part.from_bytes(pdf_bytes, "application/pdf")
    response = client.models.generate_content(
        model=model,
        contents=[pdf_part, SINGLE_CALL_USER],
        config=types.GenerateContentConfig(
            system_instruction=SINGLE_CALL_SYSTEM,
            temperature=0,
            response_mime_type="application/json",
        ),
    )
"""

# ── Shared schema description ─────────────────────────────────────────────────

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
  "extraction_notes": ["string"],
  "skip": true | false,
  "skip_reason": "string | null"
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
- skip: true when the document is too incomplete to yield reliable extraction (see SKIP
  DECISION below). false otherwise. When skip is true, still populate whatever fields
  are readable; set confidence to "low" and fill skip_reason.
- skip_reason: one or two sentences explaining why the document was skipped. null when
  skip is false.
"""

# ── Shared extraction rules ──────────────────────────────────────────────────

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
  - Serial-number column (numeric or alphanumeric like "B-1") merged into the activity column.
  - Two data columns merged into one cell.
  - A row spanning all columns is a section header, not a work item.
  - Category rows (is_category: true) may appear in Table B as well.

TOWER LABEL VALIDATION
The "Building / Tower no." cell in Haryana certificates is often misused. Apply these rules
before setting tower_labels and tower_type.

ALWAYS VALID — never reject these as project names, regardless of context:
  Any value of the form <type-word> <letter-or-number>, where type-word is one of:
  Tower, Wing, Block, Building, Bldg, Phase, Plot, Unit, T, W, B, or a bare letter/number.
  Examples that are ALWAYS valid tower labels:
    "Tower A", "Tower-A", "Tower 1", "T-1", "T1"
    "Wing B", "Wing-B", "W2"
    "Block C", "Block-C", "B3"
    "Building 1", "Bldg-2"
    "Phase II", "Phase 1"
    "A", "B", "C", "1", "2"
    "L1", "L2", "L3-L5"

Reject as a tower label — set tower_labels: null, tower_type: null — ONLY when the
value is clearly NOT a tower identifier:
  - Pure project/brand name with no tower code: "M2K Olive Greens", "Vienna Greens",
    "Whiteland Blissville" (a proper noun with no alphanumeric tower suffix)
  - Project name combined with location: "M2K Olive Greens, Sector-104, Gurugram"
  - Location or administrative text only: "Sector-76, RC 79 of 2022, 55 Plots"
  - Generic placeholders: "NA", "N/A", "All", "unknown", "Not Applicable", "Project"

Strip project name — keep only the identifier tokens — when the cell mixes a real
tower code with a project name or location suffix:
  - "L1, L2, L3-L5 Samsara Villa" → tower_labels: ["L1", "L2", "L3-L5"]
  - "Tower A, B – Whiteland Blissville" → tower_labels: ["Tower A", "Tower B"]
  Rule: discard tokens that match the known project name or read as a location.
  If no valid identifier tokens remain after stripping, set tower_labels: null.

Never output "NA", "All", "unknown", or any placeholder as a tower label.
SCOPE: These rules apply ONLY to the tower/building/wing column. They never apply to
activity strings — an activity string that begins with "B-1" or "A-2" is subject to
SERIAL NUMBER STRIPPING below, not to tower label logic.

SERIAL NUMBER STRIPPING (mandatory, all tables)
When the S.No column has merged into the activity text, ALWAYS strip the leading serial
token before outputting the activity name. This applies to Table A, A2, B, and any other
table. Serial token patterns to strip:
  - Plain number:            "1 Excavation"    → "Excavation"
  - Number with dot:         "1. Excavation"   → "Excavation"
  - Letter + number:         "A1 Plumbing"     → "Plumbing"
  - Letter + hyphen + number:"B-1 Services"    → "Services"
                             "C-2 Landscaping" → "Landscaping"
The stripped token must appear at the START of the activity string. Do not strip a
number that is integral to the activity name ("0 number of podiums" stays unchanged).
IMPORTANT: Tower label validation does NOT apply to activity strings. "B-1 Services"
in an activity column → strip "B-1", output "Services". "B-1" is NOT a tower label here.

EXTRACTION ARTEFACTS (all tables)
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
After reading all pages, check for two distinct problems:

PROBLEM A — JUMBLED PAGES (pages present but out of order):
  Symptom: S.No sequence has a gap at one page boundary that is exactly filled by another
           page in the document (e.g. page 2 ends at S.No 12, page 3 starts at S.No 20,
           but page 5 covers S.No 13–19).
  Action:  Re-order those pages logically and extract from the corrected sequence.
           If fully resolved → confidence: "high".
           If partially resolved (some pages still misplaced) → confidence: "low".

PROBLEM B — MISSING PAGES (pages absent from the scan):
  Symptom: S.No sequence has a gap that NO other page in the document fills
           (e.g. page 2 ends at S.No 12, page 3 starts at S.No 20, and no page
           covers S.No 13–19). This is confirmed missing pages, not jumbled pages.
  Action:  Set skip: true immediately (see SKIP DECISION). Do not attempt to
           "resolve" the gap by choosing a fragment, inferring values, or treating
           the remaining data as complete. Choosing one fragment over another is
           NOT a resolution — it means data was lost.

Additional continuity signals (treat as Problem B if unresolvable by reordering):
  - Table column-header continuity: a sudden change in column structure at a page
    boundary that no other page explains.
  - Paragraph flow: a sentence cut mid-way with no resumption on any available page.
  - Fragmented or repeated table content where no arrangement of the available pages
    produces a coherent, gap-free S.No sequence.

STRICT S.NO RULE
After attempting all possible page reorderings, if any gap remains in the S.No
sequence, treat it as Problem B. There is no partial credit — an unresolvable
S.No gap means pages are missing from the scan, full stop.

SKIP DECISION
Set skip: true when the document is too incomplete to yield meaningful extraction.
Trigger skip: true if EITHER of these conditions holds:

  1. Problem B (missing pages) was detected — any S.No gap that cannot be closed
     by reordering the available pages, or any structural table break with no
     available page to fill it.

  2. Both Table A1 and Table A2 are absent or completely unreadable due to missing
     pages.

Do NOT set skip: true solely because many rows have pct: null — early-stage
projects may genuinely have zero or unreported progress on most activities.

When skip: true:
  - Set confidence: "low".
  - Set skip_reason: a concise explanation naming which S.No ranges are missing and
    what data is lost as a result (e.g. "S.No 9–14 absent from scan; Super-Structure
    slab and floor-count rows have no data as a result. Remaining data cannot be
    considered complete.").
  - Still populate project, certified_towers, and any tables that ARE readable.

When skip: false:
  - Set skip_reason: null.
  - Set confidence: "high" if extraction is fully reliable; "low" with a
    confidence_note if some data is uncertain but the document is still usable.

CERTIFIED_TOWERS
Extract from the opening section where the architect names the specific
towers/wings/blocks/plots they are certifying in this report.
Apply the same validation as TOWER LABEL VALIDATION: each entry must be a real
tower/wing/block/plot identifier, not a project name, location, or placeholder.
If what is listed is a project name or address rather than a tower identifier,
output certified_towers: []. Never include project names, "All", "NA", or
location strings as entries. If no valid tower identifiers can be extracted,
output certified_towers: [].

MISSING_TOWERS
After extracting all tables_a entries, compare tower_labels against certified_towers.
List any certified tower with no matching tables_a entry.
If certified_towers is empty, set missing_towers to [].

EXTRACTION_NOTES
Record: A1/A2 identification method, guessed tower labels, table type inferred from content,
page reordering applied (jumbled-page recovery), missing-page gaps detected, corrected merged
columns, page-break losses, landscape-table handling, category rows identified,
proposed-column overrides, or any other assumption made.
"""

# ── Prompt 1: System prompt for structure-extraction call ────────────────────

STRUCTURE_SYSTEM = f"""\
You extract structured data from Indian RERA (Real Estate Regulatory Authority)
construction progress certificates for Haryana state.

These certificates are called "Architect Certificates". They may be text-based PDFs
(given as extracted markdown) or scanned PDFs (given as raw pages). Always scan
both paragraphs and tables to locate all required fields.

{_SCHEMA}

{_RULES}
""".strip()

# ── Prompt 2: User message for structure-extraction call ─────────────────────

STRUCTURE_USER = """\
Extract structured data from the RERA construction certificate below.
Output only raw JSON — no markdown fencing, no explanation.

---
{markdown_content}
---
"""

# ── Prompts 3 & 4: Single-call variant for scanned PDFs ──────────────────────

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
