# Research Notes: Analyzing Table Usage Across Power BI PBIR Reports

## Goal
Determine which semantic model tables are used by reports (and how), and which are
unused, by statically parsing PBIP artifacts with Python. Output: CSV documentation of
tables, relationships, calculations (measures / calculated columns / calculated tables),
and per-report field usage.

## 1. The two artifact layers

A PBIP project has two independent layers that must be joined:

| Layer | Folder | Key files | Contains |
|---|---|---|---|
| Semantic model | `*.SemanticModel` | `model.bim` (TMSL JSON) or `definition/` (TMDL) | tables, columns, measures, relationships, partitions |
| Report (PBIR) | `*.Report` | `definition.pbir`, `definition/**` | pages, visuals, filters, bookmarks |

The report layer never contains DAX definitions — only *references* to model objects
by table name ("Entity") and field name ("Property"). The model layer never knows
which reports consume it. Cross-referencing the two answers the usage question.

## 2. PBIR report folder structure (enhanced format)

```
MyReport.Report/
├── definition.pbir                  # points to the semantic model (byPath or byConnection)
└── definition/
    ├── report.json                  # report-level config + report-level filters (filterConfig)
    ├── version.json
    ├── reportExtensions.json        # OPTIONAL: report-level measures defined in a thin report
    ├── bookmarks/
    │   ├── bookmarks.json
    │   └── *.bookmark.json          # captured filter/field state (may reference entities)
    └── pages/
        ├── pages.json               # page order + active page
        └── {pageId}/
            ├── page.json            # displayName, ordinal, PAGE-LEVEL filters (filterConfig)
            └── visuals/
                └── {visualId}/
                    ├── visual.json  # visualType, query bindings, VISUAL-LEVEL filters
                    └── mobile.json  # optional mobile layout
```

Every file has a public JSON schema
(`https://developer.microsoft.com/json-schemas/fabric/item/report/definition/...`).

## 3. Where field references live

### 3.1 visual.json → query bindings (primary source)
Path: `visual.query.queryState.{Role}.projections[]` where `{Role}` is the field-well
name and varies by visual type: `Category`, `Values`, `Y`, `Y2`, `Series`, `Rows`,
`Columns`, `Data`, `Details`, `Tooltips`, etc. Each projection:

```json
{
  "field": {
    "Column":  { "Expression": { "SourceRef": { "Entity": "Product" } }, "Property": "Brand" }
  },
  "queryRef": "Product.Brand",
  "nativeQueryRef": "Brand",
  "active": true
}
```

Field wrapper kinds observed: `Column`, `Measure`, `Hierarchy` / `HierarchyLevel`,
and `Aggregation` (which nests a `Column` inside `Expression`). In every case the
table name is the string at `... "SourceRef": { "Entity": "<TableName>" }`.

`sortDefinition.sort[].field` uses the same structure and can reference a field not
present in projections.

### 3.2 filterConfig (report.json, page.json, visual.json)
Filters exist at all three levels under `filterConfig.filters[]`. Each filter has a
`field` in the same Entity/Property shape. Persisted filter *values* may instead use a
query-style form with alias indirection:

```json
"From": [ { "Name": "p", "Entity": "Product", "Type": 0 } ],
"Where": [ { "Condition": { ... "SourceRef": { "Source": "p" } ... } } ]
```

So a parser must handle both `SourceRef.Entity` (direct) and `SourceRef.Source`
(alias resolved via a `From` array in the same subtree/file).

### 3.3 Other reference locations (covered by recursive scan)
- Conditional formatting / dynamic titles: `visual.objects.*[].properties.*.expr`
- Bookmarks: captured filter state in `*.bookmark.json`
- `reportExtensions.json`: report-scope measures — has `entities[].name` (host table)
  and DAX `expression` strings
- Drillthrough / tooltip page bindings in page.json

**Design decision:** rather than enumerating every schema path (which shifts across
schema versions — seven version bumps in the March 2026 wave alone), the script
recursively walks *all* JSON in `definition/` and harvests every
`SourceRef.Entity`, `From[].Entity`, and Entity+Property pair. This is robust to
schema evolution and unknown visual types (including custom visuals, which use the
same queryState structure).

## 4. Semantic model side (model.bim, TMSL JSON)

Relevant paths:
- `model.tables[].name`, `.isHidden`
- `model.tables[].columns[]` — calculated columns have `"type": "calculated"` and an `expression`
- `model.tables[].measures[]` — `name`, `expression` (string or array of lines)
- `model.tables[].partitions[].source.type == "calculated"` → calculated table (DAX in `source.expression`)
- `model.relationships[]` — `fromTable`, `fromColumn`, `toTable`, `toColumn`,
  `isActive` (default true), `crossFilteringBehavior` (default oneDirection)

## 5. Usage classification logic

A table can be "used" three ways, in decreasing directness:

1. **Direct** — its Entity name appears in any report file (visual binding, filter, sort,
   conditional formatting, bookmark).
2. **Indirect via measure** — a measure that IS used in a report references the table in
   its DAX (e.g. measure lives in a "Measures" table but computes over `FactSales`).
   Requires parsing DAX for `'Table'[Col]`, `Table[Col]`, and bare table tokens, and
   resolving measure→measure references transitively.
3. **Indirect via relationship** — the table sits on an active relationship path to a
   used table (filter propagation / RELATED). Reported as informational, since a
   relationship alone doesn't prove the table is needed.

Anything matching none of these = candidate for removal (**Unused**).

### Known limitations of static analysis
- DAX table-reference extraction is regex-based; bare (unquoted, un-bracketed) table
  tokens can over-match if a table name is also common English used in a string literal.
  Quoted `'Table'` and `Table[` patterns are reliable.
- Doesn't see: RLS role filter DAX (add if needed — `model.roles[].tablePermissions`),
  calculation groups, field parameters' generated tables, usage by *other* models
  (composite models), Excel/Analyze-in-Excel consumers, or paginated reports.
- Reports must be in PBIR (folder) format; PBIR-Legacy `report.json`-only reports are
  detected and reported as skipped.

## 6. Output CSVs produced by the script

| File | Grain | Purpose |
|---|---|---|
| `table_usage_summary.csv` | 1 row per model table | classification (Direct / ViaMeasure / ViaRelationship / Unused), report count, report list |
| `field_usage_detail.csv` | 1 row per field reference per visual/filter | full lineage: report → page → visual → table.field, kind, context |
| `measures.csv` | 1 row per measure | home table, DAX, tables referenced by DAX, # reports using it |
| `calculated_columns.csv` | 1 row per calc column/table | expression + tables referenced |
| `relationships.csv` | 1 row per relationship | endpoints, active, cross-filter, whether either side is used |
| `report_inventory.csv` | 1 row per report | pages, visuals, distinct tables touched |
