# Power BI Table Usage Analyzer

A Python tool that cross-references a Power BI semantic model with its PBIR-format
reports to answer one question: **which tables are actually used by reports, and
which aren't?**

It statically parses the PBIP project files on disk — no connection to the Power BI
Service, no XMLA endpoint, no credentials required — and documents tables,
relationships, calculations, and per-report field usage as CSV files.

---

## How it works

A PBIP project has two layers that don't know about each other:

| Layer | Folder | What it contains |
|---|---|---|
| Semantic model | `*.SemanticModel` | tables, columns, measures, relationships (in `model.bim`) |
| Report (PBIR) | `*.Report` | pages, visuals, filters, bookmarks (in `definition/`) |

Reports reference model objects by table name (`Entity`) and field name (`Property`)
inside their JSON files. The script harvests every such reference from every report,
joins them against the model, and classifies each table. See
`PBIR_research_notes.md` for the full breakdown of where references live in the
PBIR structure.

## Requirements

- Python 3.8+ (standard library only — no packages to install)
- Semantic model exported as TMSL JSON (`model.bim`)
- Reports saved in **PBIR format** (the folder-based format with
  `definition/pages/...`). PBIR-Legacy reports (single `report.json`) are detected
  and skipped with a warning — re-save them in Power BI Desktop to convert.

## Usage

```bash
python pbi_usage_analyzer.py --model path/to/model.bim --reports path/to/reports_root -o pbi_docs
```

| Argument | Required | Description |
|---|---|---|
| `--model` | yes | Path to the semantic model JSON (`model.bim`, TMSL format) |
| `--reports` | yes | Root folder scanned **recursively** — every folder containing a `definition.pbir` is treated as one report. Repeat the flag to scan multiple roots. |
| `-o`, `--out` | no | Output directory for CSVs (default: `./pbi_docs`) |

### Examples

```bash
# All reports live under one workspace folder
python pbi_usage_analyzer.py --model ".\Sales.SemanticModel\model.bim" --reports ".\workspace" -o docs

# Reports spread across multiple folders
python pbi_usage_analyzer.py --model model.bim --reports ".\Finance" --reports ".\Ops" -o docs
```

## Output files

All CSVs are written UTF-8 with BOM, so they open cleanly in Excel.

| File | One row per | Purpose |
|---|---|---|
| `table_usage_summary.csv` | model table | **The main answer.** Classification, report count, list of reports using it |
| `field_usage_detail.csv` | field reference | Full lineage: report → page → visual → table.field, with kind and source file |
| `measures.csv` | measure | Home table, DAX, tables its DAX depends on (transitive), reports using it |
| `calculated_columns.csv` | calc column / calc table | Expression and tables referenced |
| `relationships.csv` | relationship | Endpoints, active flag, cross-filter direction, whether either side is used |
| `report_inventory.csv` | report | Page count, visual count, distinct tables touched |

### Table classification

Each table in `table_usage_summary.csv` gets one of four labels, in order of
precedence:

1. **Used - direct** — the table's name appears in at least one report file: a
   visual field binding, a report/page/visual-level filter, a sort, conditional
   formatting, or a bookmark.
2. **Used - via measure DAX** — a measure that *is* used in a report references
   this table in its DAX. Measure-to-measure references are resolved transitively
   (e.g. report uses `[Sales YoY]` → which calls `[Total Sales]` → which sums
   `Sales[Amount]`, so `Sales` is marked used).
3. **Possibly used - related to used table** — not referenced anywhere, but sits on
   an active relationship with a used table, so it may participate in filter
   propagation. Treat as "review before deleting", not proof of use.
4. **UNUSED** — no detected usage. Candidates for removal.

The console also prints the unused list and warns about entities referenced in
reports that **don't exist in the model** — usually a sign a report points at a
different or renamed semantic model.

## Limitations

Static analysis can't see everything. Before deleting an "UNUSED" table, be aware
the script does **not** check:

- **RLS role filters** (`model.roles[].tablePermissions` DAX)
- **Calculation groups** and **field parameters**
- Consumers outside these reports: other reports on the same published model,
  composite models, Analyze in Excel, paginated reports, DAX queries
- DAX table detection is regex-based. Quoted `'Table'` and `Table[Column]` patterns
  are reliable; bare table tokens (e.g. `COUNTROWS(Sales)`) are best-effort and can
  over-match if a table name is a common word.

## Files in this package

- `pbi_usage_analyzer.py` — the analyzer script
- `PBIR_research_notes.md` — research on the PBIR file structure that the parser
  is built on (folder layout, where field references live, schema patterns)
- `README.md` — this file
