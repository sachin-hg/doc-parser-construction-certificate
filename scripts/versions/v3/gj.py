"""Gujarat RERA extraction prompts — version v3 (2026-08-19).

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

# ── Shared schema description (embedded in both prompts) ─────────────────────

_SCHEMA = """
OUTPUT SCHEMA (raw JSON, no markdown fencing, no explanation):
{
  "doc_type": "form_1" | "form_8" | "other",
  "certificate_date": "string | null",
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
  "table_c": "(same structure as table_b)" | null,
  "missing_towers": ["string"],
  "extraction_notes": ["string"]
}

FIELD RULES:
- pct: numeric only (70, not "70%"). null when status is non-null.
- status: "na" = Not Applicable, "not_started" = not yet started, null = normal percentage.
- Dates: preserve as written in the document ("Jan 2024", "01/2024", etc.).
- table_c: null if absent or if no percentage column is present.
- missing_towers: empty list [] if certified_towers could not be extracted.
- is_category: true for rows that act as grouping headers (e.g. "MEP", "Finishing");
  false for leaf work items. A category row may or may not have its own pct.
- parent_category: the grouping row's activity name this item falls under; null if top-level.
- tower_type: infer from context — "tower", "wing", "block", "building", "phase", "plot",
  "other", or null if cannot determine. Set per tables_a entry.
- proposed: true = work is planned/applicable; false = explicitly not planned (no conflicting
  pct evidence); null = no proposed column present or cannot be determined.
"""

# ── Shared extraction rules ──────────────────────────────────────────────────

_RULES = """
DOCUMENT STRUCTURE
Two main form types exist. Scan both paragraphs AND tables regardless of form:
  Form 1 — project details, architect details, and certified tower/block names appear
            in narrative paragraphs (not just tables). Tables hold Table A/B/C data.
  Form 8 — project details and architect details are in structured tables.
            Table A/B/C data is also in tables.

TABLE IDENTIFICATION
Table A = construction activities for individual towers / wings / blocks / plots.
  Typical activities: excavation, basement, plinth, podium, RCC frame/slab/columns,
  brickwork/masonry, plaster, flooring, doors/windows, sanitary fittings, plumbing,
  electricals, staircase, lift/elevator, painting, finishing, waterproofing.

Table B = common / external development works.
  Typical activities: internal road, water supply, sewage/drains, landscaping,
  street lighting, waste disposal, rainwater harvesting, energy management,
  fire protection, electrical meter room, CCTV, letterbox, compound wall.

Table C = parking / garage works. Only include if a percentage column is present.
  If a Table C exists but has no pct column, skip it entirely.

If "Table A" / "Table B" / "Table C" title is absent, identify by content using
the activity lists above. Do not confuse Table B activities with Table A entries.

A table that only contains start/end dates with no percentage column is a
schedule table — output it in schedule_tables, not in tables_a.

IGNORE: tables or paragraphs about documents to be uploaded, cost/funding,
inventory, payment schedules, or other non-construction-progress content.

PRIORITY: activity name and pct are primary. Dates, remarks, and work counts
(e.g. "12 out of 20 complete") are secondary — capture them when present but
do not fail or skip a row if they are absent.

TABLES_A RULES
- One tables_a entry per physical table in the document.
- tower_labels: the tower/wing/block/plot identifiers this table covers.
  * A row that spans all columns with the same label (e.g. "| A | A | A | A |")
    is a tower header row — extract the label once, do not treat it as a work item.
  * When aggregated=true, there is one pct column representing all listed towers
    combined. Set values to a single entry; set tower to the joined label ("A+B+C").
  * When aggregated=false, there is one pct column per tower. Each tower gets its
    own entry in values.
  * If the tower label was at the bottom of the previous page and is now missing,
    infer from the sequence of other tables (e.g. if surrounding tables cover
    [A,B,C] and [E,F], the missing table likely covers [D]). Add a note in
    extraction_notes. If the label cannot be determined, set tower_labels to null.
- activity: strip leading serial tokens from the activity text. Serial tokens include
  plain numbers ("2"), numbers with dots ("2."), and alphanumeric codes where a
  letter prefix is followed by a number ("B-1", "A2", "C-3").
  Examples: "2 Excavation" → "Excavation", "B-1 Services" → "Services".
  Do not strip tokens that are part of the activity name itself
  ("0 number of podiums" stays; the leading "0" is data, not a serial).
- When a serial-number column has merged into the activity column
  ("| B-1 Services |" instead of "| B-1 | Services |"), extract just the activity.
- When two data columns have merged into one cell ("excavation 100"), split them.
- Rows where a tower/block name repeats across all columns are header rows —
  extract as the tower label for that sub-table, not as a work item.
- remarks: capture count-based information verbatim (e.g. "12 out of 20 complete").
- tower_labels: use null (not an empty list) when the label cannot be determined.

TOWER LABEL VALIDATION
The cell that names the tower/wing/block is sometimes misused by architects. Apply these
rules before setting tower_labels and tower_type.

A valid tower label is a short, explicit identifier: a letter, number, code, or short
alphanumeric name (e.g. "A", "B", "T-1", "Tower 1", "Wing B", "Block C", "Phase II").

Reject a value as a tower label — set tower_labels: null, tower_type: null — when:
  - It is a project name, address, location, or any combination thereof
    (e.g. "M2K Olive Greens, Sector-104, Gurugram" → null)
  - It contains locality/city/sector information, plot counts, floor counts, or legal
    references (e.g. "Sector-76 … RC 79 of 2022, 55 Plots, 220 Floors" → null)
  - It is a generic placeholder: "NA", "N/A", "All", "unknown", "Not Applicable",
    "Project", or any similar non-identifier → null

Strip — extract only the identifiers — when the cell mixes real tower codes with a
project name or location suffix:
  - "L1, L2, L3-L5 Samsara Villa" → tower_labels: ["L1", "L2", "L3-L5"]
  - "Tower A, B – Whiteland Blissville" → tower_labels: ["Tower A", "Tower B"]
  Rule: split on common delimiters (comma, dash, slash, space). Discard any token
  that matches the known project name or reads as a location (contains "Sector",
  "Road", city names, etc.). If no valid identifier tokens remain after stripping,
  set tower_labels: null.

Never output "NA", "All", "unknown", or any placeholder as a tower label. Tower
labels are either known explicitly or null — there is no middle ground.

CATEGORY ROWS
Tables may include bold or otherwise-marked grouping rows that head a set of sub-items.
  Examples: "MEP" groups Mechanical, Electrical, Plumbing.
            "Finishing" groups External Finishing, Internal Finishing.
  Rule: include the grouping row as a work_item with is_category: true, parent_category: null.
  Set each sub-item's parent_category to the grouping row's activity name, is_category: false.
  Include the grouping row AND each sub-item in work_items in document order.
  If the grouping row carries its own pct, capture it; if it is just a header, set pct: null.

TOWER TYPE
Infer tower_type from the tower label and surrounding text. Set per tables_a entry.
  "Tower A", "T-1" → "tower"
  "Wing 1", "Wing A" → "wing"
  "Block A", "Block 1" → "block"
  "Building 1", "Bldg 2" → "building"
  "Phase 1", "Phase II" → "phase"
  "Plot 12" → "plot"
  Multiple types in one table (e.g. "Tower A, B") → use the predominant type.
  Cannot determine → null.

PROPOSED COLUMN
If a "Proposed" / "Applicable" / "Planned" column is present, interpret it per row:
  - Proposed = No / NA / Not Applicable → proposed: false.
    Exception: if pct > 0 in the same row, override to proposed: true.
  - Proposed = Yes / Applicable / ✓ → proposed: true.
  - Proposed = Yes with pct column blank / "--" / null → proposed: true, pct: null,
    status: "not_started".
  - No Proposed column present → proposed: null for all rows.

WIDE MULTI-TOWER TABLES (one physical table, many tower columns)
Some tables list all towers as separate columns under a single spanning header, e.g.:

  | Activity   | % complete                              |
  |            |  A   |  B   |  C,D  |  E,F  |  G      |
  | Excavation | 100% | 70%  |  60%  |  90%  |  10%    |

Model this as ONE tables_a entry with aggregated=false:
  - tower_labels: all distinct column headers listed (["A","B","C,D","E,F","G"])
  - For each work_item, produce one value entry per column in left-to-right order.
  - Columns that already combine towers ("C,D", "E,F") → set that value entry's
    tower to "C+D" or "E+F" respectively; note in extraction_notes that this
    column is aggregated for those towers.
These wide tables may appear as very wide markdown tables in text-PDF extractions.

INTERNAL / EXTERNAL WORKS SUB-TABLES WITHIN TABLE A
In some documents, Table A data for one tower is split across multiple physical tables:
  1. A schedule table (start/end dates only, no pct) → schedule_tables.
  2. An internal works table (pct for activities like plumbing, electricals, plaster).
  3. An external works table (pct for activities like external cladding, terrace).
Tables 2 and 3 are both tables_a entries sharing the same tower_labels.
Use the activity names to distinguish internal from external when the title is absent.

TABLE B / TABLE C ARTEFACTS
The same extraction artefacts that affect Table A also apply to Table B and C:
  - Serial-number column merged into the activity column.
  - Two data columns merged into one cell.
  - A row spanning all columns is a section header, not a work item.
  - Percentage column absent → that table is schedule-only; put in schedule_tables.

LANDSCAPE / WIDE TABLES
Tables too wide for portrait may be printed on landscape pages in the original,
or — in scanned PDFs — rotated 90°/−90°. In markdown extractions of text PDFs,
very wide tables may appear broken or have many columns. Reconstruct the intended
table structure from column headers and activity content. Note any reconstruction
in extraction_notes.

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
After extracting all tables_a entries, compare their tower_labels against
certified_towers. List any tower from certified_towers that has no matching
tables_a entry. If certified_towers is empty, set missing_towers to [].

EXTRACTION_NOTES
Record: guessed tower labels, table type inferred from content, corrected merged
columns, page-break losses, landscape-table handling, wide-table column groupings,
internal/external sub-table splits, or any other assumption made.
"""

# ── Prompt 1: System prompt for structure-extraction call ────────────────────

STRUCTURE_SYSTEM = f"""\
You extract structured data from Indian RERA (Real Estate Regulatory Authority)
construction progress certificates for Gujarat state.

These quarterly certificates are submitted by architects confirming the percentage
completion of construction work. Documents are mostly text-based PDFs; a few are
scanned. Two main formats exist:
  Form 1 — project/architect details in narrative paragraphs; Table A/B/C in tables.
  Form 8 — project/architect details and Table A/B/C all in structured tables.
Always scan both paragraphs and tables to locate all required fields.

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
construction progress certificates for Gujarat state.

The input is a scanned PDF. Read all pages carefully — including table contents,
headers, stamps, and any handwritten annotations. Watch for pages that are
rotated 90°/−90° (landscape tables printed sideways).

Two main formats exist:
  Form 1 — project/architect details in narrative paragraphs; Table A/B/C in tables.
  Form 8 — project/architect details and Table A/B/C all in structured tables.
Always scan both paragraphs and tables to locate all required fields.

{_SCHEMA}

{_RULES}
""".strip()

SINGLE_CALL_USER = (
    "Extract structured data from this certificate. Output only raw JSON."
)
