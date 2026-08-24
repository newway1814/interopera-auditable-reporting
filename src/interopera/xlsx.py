from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from interopera.computation import Figure


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS = {"x": MAIN_NS}
ET.register_namespace("", MAIN_NS)


def _set_inline_string(cell: ET.Element, value: str) -> None:
    cell.set("t", "inlineStr")
    for child in list(cell):
        cell.remove(child)
    inline = ET.SubElement(cell, f"{{{MAIN_NS}}}is")
    text = ET.SubElement(inline, f"{{{MAIN_NS}}}t")
    if value.startswith(" ") or value.endswith(" "):
        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    text.text = value


def _source_cell(figure: Figure) -> str:
    governed = next((path for path in figure.graph_paths if len(path) > 1 and path[1].get("relation") == "GOVERNED_BY"), None)
    computed = next(
        (
            path for path in figure.graph_paths
            if len(path) > 1
            and path[1].get("relation") == "COMPUTED_FROM"
            and path[-1].get("id", "").startswith("chunk:holding:")
        ),
        None,
    )
    guideline_chunk = governed[-1]["id"] if governed else ""
    holding_chunk = computed[-1]["id"] if computed else ""
    guideline = next((item for item in figure.citations if item["chunk_id"] == guideline_chunk), None)
    holding = next((item for item in figure.citations if item["chunk_id"] == holding_chunk), None)
    branches: list[str] = []
    if governed and guideline:
        limit_id = governed[2]["id"]
        branches.append(
            f"Figure -[:GOVERNED_BY]-> {limit_id} -[:DERIVED_FROM]-> "
            f"{guideline['source_doc']} p.{guideline['page']}/{guideline['chunk_id']}"
        )
    if computed and holding:
        branches.append(
            f"Figure -[:COMPUTED_FROM]-> {len(figure.input_node_ids)} Position(s) -[:DERIVED_FROM]-> "
            f"{holding['source_doc']} p.{holding['page']}/{holding['chunk_id']}"
        )
    return "; ".join(branches)


def _append_font(fonts: ET.Element, *, bold: bool = False, color: str = "FF000000", size: str = "11") -> None:
    font = ET.SubElement(fonts, f"{{{MAIN_NS}}}font")
    ET.SubElement(font, f"{{{MAIN_NS}}}name", {"val": "Calibri"})
    ET.SubElement(font, f"{{{MAIN_NS}}}family", {"val": "2"})
    if bold:
        ET.SubElement(font, f"{{{MAIN_NS}}}b")
    ET.SubElement(font, f"{{{MAIN_NS}}}color", {"rgb": color})
    ET.SubElement(font, f"{{{MAIN_NS}}}sz", {"val": size})


def _append_fill(fills: ET.Element, color: str) -> None:
    fill = ET.SubElement(fills, f"{{{MAIN_NS}}}fill")
    pattern = ET.SubElement(fill, f"{{{MAIN_NS}}}patternFill", {"patternType": "solid"})
    ET.SubElement(pattern, f"{{{MAIN_NS}}}fgColor", {"rgb": color})
    ET.SubElement(pattern, f"{{{MAIN_NS}}}bgColor", {"indexed": "64"})


def _styled_styles_xml(styles_bytes: bytes) -> bytes:
    root = ET.fromstring(styles_bytes)
    fonts = root.find("x:fonts", NS)
    fills = root.find("x:fills", NS)
    borders = root.find("x:borders", NS)
    cell_xfs = root.find("x:cellXfs", NS)
    if fonts is None or fills is None or borders is None or cell_xfs is None:
        raise ValueError("Template styles.xml is missing required collections")

    _append_font(fonts, bold=True, color="FFFFFFFF", size="11")
    _append_font(fonts, color="FF4B5563", size="9")
    fonts.set("count", str(len(fonts)))
    for color in ("FF17365D", "FFE2F0D9", "FFFCE8E6", "FFFFF2CC"):
        _append_fill(fills, color)
    fills.set("count", str(len(fills)))

    border = ET.SubElement(borders, f"{{{MAIN_NS}}}border")
    for side in ("left", "right", "top", "bottom"):
        element = ET.SubElement(border, f"{{{MAIN_NS}}}{side}", {"style": "thin"})
        ET.SubElement(element, f"{{{MAIN_NS}}}color", {"rgb": "FFD9E2F0"})
    ET.SubElement(border, f"{{{MAIN_NS}}}diagonal")
    borders.set("count", str(len(borders)))

    def add_xf(font_id: int, fill_id: int, alignment: dict[str, str]) -> None:
        xf = ET.SubElement(
            cell_xfs,
            f"{{{MAIN_NS}}}xf",
            {
                "numFmtId": "0", "fontId": str(font_id), "fillId": str(fill_id),
                "borderId": "1", "xfId": "0", "applyAlignment": "1",
            },
        )
        ET.SubElement(xf, f"{{{MAIN_NS}}}alignment", alignment)

    add_xf(1, 2, {"horizontal": "center", "vertical": "center", "wrapText": "1"})  # 1 header
    add_xf(0, 0, {"vertical": "top"})  # 2 body text
    add_xf(0, 0, {"horizontal": "center", "vertical": "center"})  # 3 values
    add_xf(2, 0, {"vertical": "top", "wrapText": "1"})  # 4 provenance
    add_xf(0, 3, {"horizontal": "center", "vertical": "center"})  # 5 OK
    add_xf(0, 4, {"horizontal": "center", "vertical": "center"})  # 6 BREACH
    add_xf(0, 5, {"horizontal": "center", "vertical": "center"})  # 7 AT LIMIT
    cell_xfs.set("count", str(len(cell_xfs)))
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


def _style_sheet(root: ET.Element) -> None:
    sheet_view = root.find(".//x:sheetView", NS)
    if sheet_view is not None:
        sheet_view.set("showGridLines", "0")
        for child in list(sheet_view):
            sheet_view.remove(child)
        ET.SubElement(sheet_view, f"{{{MAIN_NS}}}pane", {"ySplit": "1", "topLeftCell": "A2", "activePane": "bottomLeft", "state": "frozen"})
        ET.SubElement(sheet_view, f"{{{MAIN_NS}}}selection", {"pane": "bottomLeft", "activeCell": "A2", "sqref": "A2"})

    sheet_data = root.find("x:sheetData", NS)
    if sheet_data is None:
        raise ValueError("Template has no sheetData")
    cols = ET.Element(f"{{{MAIN_NS}}}cols")
    widths = [(1, "16"), (2, "38"), (3, "18"), (4, "16"), (5, "17"), (6, "14"), (7, "78")]
    for index, width in widths:
        ET.SubElement(cols, f"{{{MAIN_NS}}}col", {"min": str(index), "max": str(index), "width": width, "customWidth": "1"})
    root.insert(list(root).index(sheet_data), cols)

    rows = sheet_data.findall("x:row", NS)
    for row_index, row in enumerate(rows):
        row.set("ht", "30" if row_index == 0 else "45")
        row.set("customHeight", "1")
        for cell in row.findall("x:c", NS):
            reference = cell.attrib["r"]
            column = reference[0]
            if row_index == 0:
                cell.set("s", "1")
            elif column in "AB":
                cell.set("s", "2")
            elif column == "G":
                cell.set("s", "4")
            elif column == "F":
                status = "".join(cell.itertext())
                cell.set("s", {"OK": "5", "BREACH": "6", "AT LIMIT": "7"}.get(status, "3"))
            else:
                cell.set("s", "3")


def export_report(template_path: Path, output_path: Path, figures: list[Figure]) -> None:
    by_metric = {figure.metric: figure for figure in figures}
    with zipfile.ZipFile(template_path, "r") as source:
        sheet_bytes = source.read("xl/worksheets/sheet1.xml")
        root = ET.fromstring(sheet_bytes)
        rows = root.findall(".//x:sheetData/x:row", NS)
        for row in rows[1:]:
            cells = {cell.attrib["r"]: cell for cell in row.findall("x:c", NS)}
            row_number = row.attrib["r"]
            metric_cell = cells[f"B{row_number}"]
            metric = "".join(metric_cell.itertext())
            if metric not in by_metric:
                raise ValueError(f"Template metric has no computed figure: {metric}")
            figure = by_metric[metric]
            values = {
                f"C{row_number}": figure.value,
                f"D{row_number}": figure.limit,
                f"E{row_number}": figure.utilization,
                f"F{row_number}": figure.status,
                f"G{row_number}": _source_cell(figure),
            }
            for reference, value in values.items():
                _set_inline_string(cells[reference], value)
        _style_sheet(root)
        updated_sheet = ET.tostring(root, encoding="utf-8", xml_declaration=False)
        updated_styles = _styled_styles_xml(source.read("xl/styles.xml"))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w") as destination:
            for info in source.infolist():
                if info.filename == "xl/worksheets/sheet1.xml":
                    payload = updated_sheet
                elif info.filename == "xl/styles.xml":
                    payload = updated_styles
                else:
                    payload = source.read(info.filename)
                destination.writestr(info, payload)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(item.itertext()) for item in root.findall("x:si", NS)]


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(cell.itertext())
    value = cell.find("x:v", NS)
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        return shared[int(value.text)]
    return value.text


def read_first_sheet(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path, "r") as archive:
        shared = _shared_strings(archive)
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        table: list[list[str]] = []
        for row in root.findall(".//x:sheetData/x:row", NS):
            values = {cell.attrib["r"]: _cell_value(cell, shared) for cell in row.findall("x:c", NS)}
            row_number = row.attrib["r"]
            table.append([values.get(f"{column}{row_number}", "") for column in "ABCDEFG"])
        return table


def answer_key_from_xlsx(path: Path) -> dict[str, dict[str, str]]:
    rows = read_first_sheet(path)
    return {
        row[1]: {"value": row[2], "limit": row[3], "utilization": row[4], "status": row[5]}
        for row in rows[1:]
    }
