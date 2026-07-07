#!/usr/bin/env python3
"""
pbi_usage_analyzer.py
=====================
Cross-references a Power BI semantic model (model.bim / TMSL JSON) with one or
more PBIR-format reports (definition/pages/... folder structure) to document
which tables are used, how, and by which reports.

Usage:
    python pbi_usage_analyzer.py --model path/to/model.bim --reports path/to/root_folder -o output_dir

    --model    Path to the semantic model JSON (model.bim, TMSL format).
    --reports  Root folder to scan recursively for reports. Every folder that
               contains a 'definition.pbir' file is treated as one report.
               Can be passed multiple times.
    -o         Output directory for CSVs (default: ./pbi_docs)

Outputs (CSV):
    table_usage_summary.csv, field_usage_detail.csv, measures.csv,
    calculated_columns.csv, relationships.csv, report_inventory.csv
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Semantic model parsing (TMSL / model.bim)
# ---------------------------------------------------------------------------

def _expr_to_str(expr):
    """TMSL expressions can be a string or a list of lines."""
    if expr is None:
        return ""
    if isinstance(expr, list):
        return "\n".join(str(x) for x in expr)
    return str(expr)


def load_model(model_path: Path) -> dict:
    with open(model_path, encoding="utf-8-sig") as f:
        raw = json.load(f)

    # model.bim wraps everything under "model"; a bare TMSL model object also works
    model = raw.get("model", raw)

    tables = {}
    for t in model.get("tables", []):
        name = t.get("name")
        if not name:
            continue
        measures = []
        for m in t.get("measures", []):
            measures.append({
                "name": m.get("name", ""),
                "expression": _expr_to_str(m.get("expression")),
                "isHidden": bool(m.get("isHidden", False)),
                "displayFolder": m.get("displayFolder", ""),
            })
        calc_columns = []
        for c in t.get("columns", []):
            if c.get("type") == "calculated":
                calc_columns.append({
                    "name": c.get("name", ""),
                    "expression": _expr_to_str(c.get("expression")),
                })
        # partitions (the actual data sources) + calculated-table detection
        calc_table_expr = ""
        partitions = []
        for p in t.get("partitions", []):
            src = p.get("source", {})
            ptype = src.get("type", "m" if src.get("expression") else "")
            expr = _expr_to_str(src.get("expression")) or _expr_to_str(src.get("query"))
            if ptype == "calculated" and not calc_table_expr:
                calc_table_expr = expr
            partitions.append({
                "name": p.get("name", ""),
                "mode": p.get("mode", "import"),
                "type": ptype,
                "expression": expr,
                "dataSource": src.get("dataSource", ""),
                "entityName": src.get("entityName", ""),
                "schemaName": src.get("schemaName", ""),
            })
        tables[name] = {
            "isHidden": bool(t.get("isHidden", False)),
            "measures": measures,
            "calc_columns": calc_columns,
            "calc_table_expression": calc_table_expr,
            "partitions": partitions,
            "n_columns": len(t.get("columns", [])),
        }

    # legacy provider datasources (used by partition source type "query")
    datasources = {}
    for ds in model.get("dataSources", []):
        cs = ds.get("connectionString", "")
        server = re.search(r"(?:data source|server)\s*=\s*([^;]+)", cs, re.I)
        db = re.search(r"(?:initial catalog|database)\s*=\s*([^;]+)", cs, re.I)
        datasources[ds.get("name", "")] = {
            "server": server.group(1).strip() if server else "",
            "database": db.group(1).strip() if db else "",
        }

    relationships = []
    for r in model.get("relationships", []):
        relationships.append({
            "name": r.get("name", ""),
            "fromTable": r.get("fromTable", ""),
            "fromColumn": r.get("fromColumn", ""),
            "toTable": r.get("toTable", ""),
            "toColumn": r.get("toColumn", ""),
            "isActive": bool(r.get("isActive", True)),
            "crossFilteringBehavior": r.get("crossFilteringBehavior", "oneDirection"),
        })

    return {"tables": tables, "relationships": relationships,
            "datasources": datasources}


# ---------------------------------------------------------------------------
# DAX reference extraction (static, regex-based; see research notes for limits)
# ---------------------------------------------------------------------------

def _strip_dax_noise(dax: str) -> str:
    """Remove string literals and comments so we don't match tables inside them."""
    dax = re.sub(r'"(?:[^"]|"")*"', '""', dax)          # string literals
    dax = re.sub(r"//[^\n]*", "", dax)                   # line comments
    dax = re.sub(r"--[^\n]*", "", dax)
    dax = re.sub(r"/\*.*?\*/", "", dax, flags=re.S)      # block comments
    return dax


def extract_dax_refs(dax: str, table_names, measure_index):
    """
    Return (tables_referenced, measures_referenced) from a DAX expression.
    - 'Quoted Table' refs and Table[Column] refs are reliable.
    - Bare table tokens (e.g. COUNTROWS(Sales)) matched as whole words.
    - [Measure] refs resolved against the global measure index.
    """
    clean = _strip_dax_noise(dax)
    found_tables = set()

    for m in re.finditer(r"'([^']+)'", clean):           # 'Table Name'
        if m.group(1) in table_names:
            found_tables.add(m.group(1))

    for name in table_names:
        if re.search(r"(?<!')\b" + re.escape(name) + r"\s*\[", clean):  # Table[Col]
            found_tables.add(name)
        elif re.search(r"(?<![\w'])" + re.escape(name) + r"(?![\w\[])", clean):
            found_tables.add(name)                        # bare token (best effort)

    found_measures = set()
    for m in re.finditer(r"\[([^\[\]]+)\]", clean):
        if m.group(1) in measure_index:
            found_measures.add(m.group(1))

    return found_tables, found_measures


def resolve_measure_tables(measure_name, measure_index, table_names, cache, stack=None):
    """Transitively resolve every table a measure depends on through DAX."""
    if measure_name in cache:
        return cache[measure_name]
    stack = stack or set()
    if measure_name in stack:
        return set()
    stack.add(measure_name)

    info = measure_index[measure_name]
    tables, measures = extract_dax_refs(info["expression"], table_names, measure_index)
    for child in measures:
        if child != measure_name:
            tables |= resolve_measure_tables(child, measure_index, table_names, cache, stack)
    stack.discard(measure_name)
    cache[measure_name] = tables
    return tables


# ---------------------------------------------------------------------------
# Partition source analysis
# ---------------------------------------------------------------------------

def _sql_objects(sql: str):
    """Extract table/view names from FROM and JOIN clauses of a SQL string."""
    objs = re.findall(r"(?:FROM|JOIN)\s+([\w\[\]\.\"]+)", sql, re.I)
    return sorted({o.strip('"') for o in objs if o.upper() != "SELECT"})


def analyze_partition(part: dict, datasources: dict) -> dict:
    """
    Classify a partition's source and pull out server / database / source
    objects. Binary (enter-data) payloads are flagged but not decoded.
    """
    out = {"source_kind": "", "server": "", "database": "",
           "source_objects": "", "note": ""}
    expr = part["expression"]
    ptype = part["type"]

    if ptype == "calculated":
        out["source_kind"] = "Calculated table (DAX)"
        out["note"] = expr[:200]
        return out

    if ptype == "entity":
        out["source_kind"] = "Dataflow / entity"
        obj = part["entityName"]
        if part["schemaName"]:
            obj = f"{part['schemaName']}.{obj}"
        out["source_objects"] = obj
        return out

    if ptype == "query":  # legacy provider partition
        ds = datasources.get(part["dataSource"], {})
        out["source_kind"] = "Legacy SQL query"
        out["server"], out["database"] = ds.get("server", ""), ds.get("database", "")
        out["source_objects"] = "; ".join(_sql_objects(expr))
        return out

    # --- M expression partitions -------------------------------------------
    if "Binary.Decompress" in expr or "Binary.FromText" in expr:
        out["source_kind"] = "Embedded binary (enter-data)"
        out["note"] = "Opaque binary payload stored in the model - details omitted"
        return out

    m = re.search(r'Sql\.Databases?\(\s*"([^"]+)"(?:\s*,\s*"([^"]+)")?', expr)
    if m:
        out["source_kind"] = "SQL Server (M)"
        out["server"], out["database"] = m.group(1), m.group(2) or ""
    else:
        m = re.search(r'AnalysisServices\.Databases?\(\s*"([^"]+)"(?:\s*,\s*"([^"]+)")?', expr)
        if m:
            out["source_kind"] = "Analysis Services (M)"
            out["server"], out["database"] = m.group(1), m.group(2) or ""
        else:
            m = re.search(r'(\w[\w.]*)\.(?:Database|Databases|Contents|Catalogs|Workspaces)\(\s*"([^"]*)"', expr)
            if m:
                out["source_kind"] = f"{m.group(1)} (M)"
                out["server"] = m.group(2)
            elif expr:
                out["source_kind"] = "M query (unrecognized connector)"

    # database picked via navigation: [Name="DW"] on a Databases() list
    if out["server"] and not out["database"]:
        nav = re.search(r'\[\s*Name\s*=\s*"([^"]+)"\s*\]', expr)
        if nav:
            out["database"] = nav.group(1)

    objs = set()
    for sm in re.finditer(r'(?:Schema\s*=\s*"([^"]+)"\s*,\s*)?Item\s*=\s*"([^"]+)"', expr):
        objs.add(f"{sm.group(1)}.{sm.group(2)}" if sm.group(1) else sm.group(2))
    # native SQL embedded in the M ([Query="..."] or Value.NativeQuery)
    for qm in re.finditer(r'"(\s*(?:SELECT|WITH)\b[^"]+)"', expr, re.I):
        objs.update(_sql_objects(qm.group(1)))
        if "native SQL" not in out["note"]:
            out["note"] = (out["note"] + " contains native SQL").strip()
    out["source_objects"] = "; ".join(sorted(objs))
    return out




FIELD_KINDS = ("Column", "Measure", "Hierarchy", "HierarchyLevel",
               "Aggregation", "NativeVisualCalculation")


def find_reports(roots):
    reports = []
    for root in roots:
        root = Path(root)
        for pbir in sorted(root.rglob("definition.pbir")):
            report_dir = pbir.parent
            definition = report_dir / "definition"
            if definition.is_dir():
                reports.append(report_dir)
            else:
                print(f"  [skip] {report_dir.name}: PBIR-Legacy format "
                      f"(no definition/ folder). Re-save as PBIR in Desktop.",
                      file=sys.stderr)
    return reports


def _load_json(path: Path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [warn] could not parse {path}: {e}", file=sys.stderr)
        return None


def _collect_alias_map(node, alias_map):
    """Harvest From:[{Name, Entity}] alias declarations anywhere in the file."""
    if isinstance(node, dict):
        frm = node.get("From")
        if isinstance(frm, list):
            for item in frm:
                if isinstance(item, dict) and "Entity" in item:
                    if item.get("Name"):
                        alias_map[item["Name"]] = item["Entity"]
        for v in node.values():
            _collect_alias_map(v, alias_map)
    elif isinstance(node, list):
        for v in node:
            _collect_alias_map(v, alias_map)


def _walk_refs(node, alias_map, out, parent_key=None):
    """
    Harvest every (entity, property, kind) reference in a PBIR JSON tree.
    Handles direct SourceRef.Entity and alias SourceRef.Source forms.
    """
    if isinstance(node, dict):
        # pattern: {"Expression": {"SourceRef": {...}}, "Property": "..."}
        expr = node.get("Expression")
        if isinstance(expr, dict) and "SourceRef" in expr:
            sref = expr["SourceRef"]
            entity = None
            if isinstance(sref, dict):
                entity = sref.get("Entity") or alias_map.get(sref.get("Source"))
            if entity:
                prop = node.get("Property") or node.get("Hierarchy") or ""
                kind = parent_key if parent_key in FIELD_KINDS else "Other"
                out.append((entity, str(prop), kind))
        else:
            # bare SourceRef without Property (e.g. table-level filter/scope)
            sref = node.get("SourceRef")
            if isinstance(sref, dict):
                entity = sref.get("Entity") or alias_map.get(sref.get("Source"))
                if entity:
                    out.append((entity, "", "TableRef"))
        # From-array entities (filter query form)
        frm = node.get("From")
        if isinstance(frm, list):
            for item in frm:
                if isinstance(item, dict) and item.get("Entity"):
                    out.append((item["Entity"], "", "TableRef"))
        for k, v in node.items():
            _walk_refs(v, alias_map, out, parent_key=k)
    elif isinstance(node, list):
        for v in node:
            _walk_refs(v, alias_map, out, parent_key=parent_key)


def extract_refs_from_file(path: Path):
    data = _load_json(path)
    if data is None:
        return []
    alias_map = {}
    _collect_alias_map(data, alias_map)
    out = []
    _walk_refs(data, alias_map, out)
    # de-dupe within file, keep richest kind info
    return sorted(set(out))


def scan_report(report_dir: Path):
    """Return (rows, inventory) for one report."""
    definition = report_dir / "definition"
    report_name = report_dir.name.replace(".Report", "")
    rows = []

    def add(refs, page_name, visual_id, visual_type, context, src_file):
        for entity, prop, kind in refs:
            rows.append({
                "report": report_name, "page": page_name, "visual_id": visual_id,
                "visual_type": visual_type, "context": context,
                "table": entity, "field": prop, "field_kind": kind,
                "source_file": str(src_file.relative_to(report_dir)),
            })

    # report-level (report.json, reportExtensions.json, bookmarks)
    for fname, ctx in [("report.json", "report-level"),
                       ("reportExtensions.json", "report-extension")]:
        p = definition / fname
        if p.exists():
            add(extract_refs_from_file(p), "", "", "", ctx, p)

    for p in sorted((definition / "bookmarks").glob("*.json")) if (definition / "bookmarks").is_dir() else []:
        add(extract_refs_from_file(p), "", "", "", "bookmark", p)

    n_pages = n_visuals = 0
    pages_dir = definition / "pages"
    if pages_dir.is_dir():
        for page_dir in sorted(d for d in pages_dir.iterdir() if d.is_dir()):
            page_json = page_dir / "page.json"
            page_data = _load_json(page_json) if page_json.exists() else None
            page_name = (page_data or {}).get("displayName", page_dir.name)
            n_pages += 1
            if page_json.exists():
                add(extract_refs_from_file(page_json), page_name, "", "", "page-filter", page_json)
            visuals_dir = page_dir / "visuals"
            if visuals_dir.is_dir():
                for vis_dir in sorted(d for d in visuals_dir.iterdir() if d.is_dir()):
                    vjson = vis_dir / "visual.json"
                    if not vjson.exists():
                        continue
                    n_visuals += 1
                    vdata = _load_json(vjson) or {}
                    vtype = (vdata.get("visual") or {}).get("visualType", "")
                    add(extract_refs_from_file(vjson), page_name, vis_dir.name,
                        vtype, "visual", vjson)

    inventory = {"report": report_name, "path": str(report_dir),
                 "pages": n_pages, "visuals": n_visuals,
                 "tables_referenced": len({r["table"] for r in rows})}
    return rows, inventory


# ---------------------------------------------------------------------------
# Cross-referencing & CSV output
# ---------------------------------------------------------------------------

def write_csv(path: Path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote {path} ({len(rows)} rows)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[2])
    ap.add_argument("--model", required=True, help="Path to model.bim / semantic model JSON")
    ap.add_argument("--reports", required=True, action="append",
                    help="Root folder(s) to scan for PBIR reports")
    ap.add_argument("-o", "--out", default="pbi_docs", help="Output directory")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Loading semantic model...")
    model = load_model(Path(args.model))
    table_names = set(model["tables"])
    print(f"  {len(table_names)} tables, {len(model['relationships'])} relationships")

    # global measure index (measure names are unique model-wide)
    measure_index = {}
    for tname, t in model["tables"].items():
        for m in t["measures"]:
            measure_index[m["name"]] = {"table": tname, **m}

    print("Scanning reports...")
    report_dirs = find_reports(args.reports)
    if not report_dirs:
        sys.exit("No PBIR reports found (no definition.pbir with a definition/ folder).")
    print(f"  found {len(report_dirs)} PBIR report(s)")

    all_rows, inventories = [], []
    for rd in report_dirs:
        rows, inv = scan_report(rd)
        all_rows.extend(rows)
        inventories.append(inv)
        print(f"  {inv['report']}: {inv['pages']} pages, {inv['visuals']} visuals, "
              f"{inv['tables_referenced']} tables referenced")

    # ---- usage rollups -----------------------------------------------------
    multi_report = len({r["report"] for r in all_rows}) > 1

    def page_label(r):
        """Attribution unit = the page. Report-scoped refs (report-level
        filters, bookmarks, extensions) get a pseudo-page label."""
        page = r["page"] or f"({r['context']})"
        return f"{r['report']} / {page}" if multi_report else page

    direct_use = defaultdict(set)            # table -> {pages}
    measure_use = defaultdict(set)           # measure -> {pages}
    unknown_entities = defaultdict(set)      # entity not in model -> {pages}
    for r in all_rows:
        lbl = page_label(r)
        if r["table"] in table_names:
            direct_use[r["table"]].add(lbl)
        else:
            unknown_entities[r["table"]].add(lbl)
        if r["field_kind"] == "Measure" and r["field"] in measure_index:
            measure_use[r["field"]].add(lbl)

    # indirect via measures used in reports
    dax_cache = {}
    via_measure = defaultdict(set)           # table -> {reports}
    for mname, reps in measure_use.items():
        for tbl in resolve_measure_tables(mname, measure_index, table_names, dax_cache):
            via_measure[tbl] |= reps

    # indirect via active relationships touching a used table
    used_somehow = set(direct_use) | set(via_measure)
    via_rel = defaultdict(set)
    for rel in model["relationships"]:
        if not rel["isActive"]:
            continue
        a, b = rel["fromTable"], rel["toTable"]
        if a in used_somehow and b not in used_somehow:
            via_rel[b].add(a)
        if b in used_somehow and a not in used_somehow:
            via_rel[a].add(b)

    # ---- CSV 1: table summary ----------------------------------------------
    summary = []
    for tname in sorted(table_names):
        t = model["tables"][tname]
        if tname in direct_use:
            cls = "Used - direct"
        elif tname in via_measure:
            cls = "Used - via measure DAX"
        elif tname in via_rel:
            cls = "Possibly used - related to used table"
        else:
            cls = "UNUSED"
        pages_d = sorted(direct_use.get(tname, set()))
        pages_m = sorted(via_measure.get(tname, set()))
        summary.append({
            "table": tname, "classification": cls, "is_hidden": t["isHidden"],
            "n_columns": t["n_columns"], "n_measures": len(t["measures"]),
            "is_calculated_table": bool(t["calc_table_expression"]),
            "pages_direct_count": len(pages_d), "pages_direct": "; ".join(pages_d),
            "pages_via_measure_count": len(pages_m),
            "pages_via_measure": "; ".join(pages_m),
            "related_used_tables": "; ".join(sorted(via_rel.get(tname, set()))),
        })
    write_csv(out_dir / "table_usage_summary.csv", summary, list(summary[0].keys()))

    # ---- CSV 2: field usage detail ------------------------------------------
    detail_fields = ["report", "page", "visual_id", "visual_type", "context",
                     "table", "field", "field_kind", "source_file"]
    write_csv(out_dir / "field_usage_detail.csv", all_rows, detail_fields)

    # ---- CSV 3: measures -----------------------------------------------------
    mrows = []
    for mname in sorted(measure_index):
        info = measure_index[mname]
        deps = sorted(resolve_measure_tables(mname, measure_index, table_names, dax_cache))
        pgs = sorted(measure_use.get(mname, set()))
        mrows.append({"measure": mname, "home_table": info["table"],
                      "used_on_page_count": len(pgs), "pages": "; ".join(pgs),
                      "tables_referenced_in_dax": "; ".join(deps),
                      "expression": info["expression"]})
    if mrows:
        write_csv(out_dir / "measures.csv", mrows, list(mrows[0].keys()))

    # ---- CSV 4: calculated columns & tables ----------------------------------
    crows = []
    for tname in sorted(table_names):
        t = model["tables"][tname]
        for c in t["calc_columns"]:
            refs, _ = extract_dax_refs(c["expression"], table_names, measure_index)
            crows.append({"object_type": "calculated column", "table": tname,
                          "name": c["name"], "tables_referenced": "; ".join(sorted(refs)),
                          "expression": c["expression"]})
        if t["calc_table_expression"]:
            refs, _ = extract_dax_refs(t["calc_table_expression"], table_names, measure_index)
            crows.append({"object_type": "calculated table", "table": tname,
                          "name": tname, "tables_referenced": "; ".join(sorted(refs)),
                          "expression": t["calc_table_expression"]})
    if crows:
        write_csv(out_dir / "calculated_columns.csv", crows, list(crows[0].keys()))

    # ---- CSV 5: partitions (source-level lineage) -------------------------------
    # one row per partition per page, so the file is filter/pivot friendly
    prows = []
    for tname in sorted(table_names):
        pages_d = direct_use.get(tname, set())
        pages_m = via_measure.get(tname, set())
        all_pages = sorted(pages_d | pages_m)
        for part in model["tables"][tname]["partitions"]:
            info = analyze_partition(part, model["datasources"])
            base = {
                "table": tname, "partition": part["name"], "mode": part["mode"],
                "source_kind": info["source_kind"], "server": info["server"],
                "database": info["database"],
                "source_objects": info["source_objects"],
                "note": info["note"],
            }
            if not all_pages:
                prows.append({**base, "page": "", "usage_type": "not used on any page"})
                continue
            for page in all_pages:
                if page in pages_d and page in pages_m:
                    usage = "direct + via measure"
                elif page in pages_d:
                    usage = "direct"
                else:
                    usage = "via measure"
                prows.append({**base, "page": page, "usage_type": usage})
    if prows:
        fields = ["table", "partition", "page", "usage_type", "mode", "source_kind",
                  "server", "database", "source_objects", "note"]
        write_csv(out_dir / "partitions.csv", prows, fields)

    # ---- CSV 6: relationships -------------------------------------------------
    rrows = []
    for rel in model["relationships"]:
        rrows.append({
            "from_table": rel["fromTable"], "from_column": rel["fromColumn"],
            "to_table": rel["toTable"], "to_column": rel["toColumn"],
            "is_active": rel["isActive"],
            "cross_filtering": rel["crossFilteringBehavior"],
            "from_table_used": rel["fromTable"] in used_somehow,
            "to_table_used": rel["toTable"] in used_somehow,
        })
    if rrows:
        write_csv(out_dir / "relationships.csv", rrows, list(rrows[0].keys()))

    # ---- CSV 6: report inventory -----------------------------------------------
    write_csv(out_dir / "report_inventory.csv", inventories,
              ["report", "path", "pages", "visuals", "tables_referenced"])

    # ---- console summary --------------------------------------------------------
    unused = [s["table"] for s in summary if s["classification"] == "UNUSED"]
    print(f"\n=== {len(unused)} table(s) with no detected usage ===")
    for u in unused:
        print(f"  - {u}")
    if unknown_entities:
        print("\n[warn] Entities referenced in reports but NOT found in the model "
              "(different model, renamed table, or report-scope table?):")
        for e, pgs in sorted(unknown_entities.items()):
            print(f"  - {e}  (on: {', '.join(sorted(pgs))})")


if __name__ == "__main__":
    main()
