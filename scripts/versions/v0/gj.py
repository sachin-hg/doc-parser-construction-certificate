"""Gujarat RERA extraction prompts — version v0 (initial, pre-2026-08-19)."""

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
      "aggregated": true | false,
      "work_items": [
        {
          "activity": "string",
          "values": [
            {
              "tower": "string",
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
"""

_RULES = """
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

If "Table A" / "Table B" title is absent, identify by content using the activity
lists above. Do not confuse Table B activities with Table A entries.

A table that only contains start/end dates with no percentage column is a
schedule table — output it in schedule_tables, not in tables_a.

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
- activity: strip leading serial numbers ("2 Excavation" → "Excavation"). Do not
  strip numbers that are part of the activity name ("0 number of podiums" stays).
- When a serial-number column has merged into the activity column
  ("| 2 excavation |" instead of "| 2 | excavation |"), extract just the activity.
- When two data columns have merged into one cell ("excavation 100"), split them.
- Rows where a tower/block name repeats across all columns are header rows —
  extract as the tower label for that sub-table, not as a work item.

CERTIFIED_TOWERS
Extract from the opening section where the architect names the specific
towers/wings/blocks/plots they are certifying in this report.

MISSING_TOWERS
After extracting all tables_a entries, compare their tower_labels against
certified_towers. List any tower from certified_towers that has no matching
tables_a entry. If certified_towers is empty, set missing_towers to [].

EXTRACTION_NOTES
Record: guessed tower labels, table type inferred from content, corrected merged
columns, page-break losses, landscape-table handling, or any other assumption made.
"""

STRUCTURE_SYSTEM = f"""\
You extract structured data from Indian RERA (Real Estate Regulatory Authority)
construction progress certificates for Gujarat state.

These quarterly certificates are submitted by architects confirming the percentage
completion of construction work. Two main formats exist:
  Form 1 — narrative paragraphs with embedded tables.
  Form 8 — fully tabular format.

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
You extract structured data from scanned Indian RERA (Real Estate Regulatory
Authority) construction progress certificates for Gujarat state.

The input is a scanned PDF. Read all pages carefully — including table contents,
headers, stamps, and any handwritten annotations.

Two main formats exist:
  Form 1 — narrative paragraphs with embedded tables.
  Form 8 — fully tabular format.

{_SCHEMA}

{_RULES}
""".strip()

SINGLE_CALL_USER = (
    "Extract structured data from this certificate. Output only raw JSON."
)
