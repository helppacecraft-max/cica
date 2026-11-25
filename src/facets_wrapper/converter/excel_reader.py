"""Read the mapping workbook into raw sections/rows.

Layout the reader recognises (as in the source sheet):

    row n-2   <banner>            e.g. "Claim Level Data"            (merged / single cell)
    row n-1   <banner>            e.g. "Web Services" / "REST Services"
    row n     header row: ... | Facets Table | Facets Column | Field Name | API Field name | URL | Comments
    row n+1.. data rows

Columns are located from the header labels ("Facets Table", "Facets Column", "URL"), not from
fixed letters, and a sheet may contain any number of such blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
SERVICE_WORDS = re.compile(r"^\s*(web|rest)\s+services?\s*$", re.I)


class ConversionError(Exception):
    pass


@dataclass
class RawRow:
    row: int
    web_field: str
    data_type: str
    requirement: str
    facets_table: str
    facets_column: str
    rest_field: str
    api_field: str
    url_text: str
    urls: list[str]
    comment: str


@dataclass
class RawSection:
    title: str
    banner_row: int
    header_row: int
    rows: list[RawRow] = field(default_factory=list)


def _txt(v) -> str:
    return "" if v is None else re.sub(r"\s+", " ", str(v)).strip()


def _clean_url(u: str) -> str:
    return u.rstrip(").,;:")


def _pick_sheet(wb, wanted: str | None):
    if wanted:
        if wanted not in wb.sheetnames:
            raise ConversionError(f"sheet {wanted!r} not found; available: {wb.sheetnames}")
        return wb[wanted]
    return wb["Mapping Sheet"] if "Mapping Sheet" in wb.sheetnames else wb.active


def read_sections(path: str | Path, sheet: str | None = None) -> tuple[str, list[RawSection]]:
    p = Path(path)
    if not p.is_file():
        raise ConversionError(f"workbook not found: {p}")
    try:
        wb = openpyxl.load_workbook(p, data_only=True)  # data_only: cached values, not formulas
    except Exception as e:
        raise ConversionError(f"cannot open workbook {p.name}: {type(e).__name__}") from e
    ws = _pick_sheet(wb, sheet)
    max_row, max_col = ws.max_row, ws.max_column

    def cell(r: int, c: int):
        return ws.cell(row=r, column=c)

    def nonempty(r: int) -> list[int]:
        return [c for c in range(1, max_col + 1) if _txt(cell(r, c).value)]

    # 1. locate header rows -------------------------------------------------------------
    headers: list[tuple[int, dict[str, int]]] = []
    for r in range(1, max_row + 1):
        labels = {c: _txt(cell(r, c).value).lower() for c in range(1, max_col + 1)}
        ft = next((c for c, l in labels.items() if l == "facets table"), None)
        url = next((c for c, l in labels.items() if l == "url" and ft and c > ft), None)
        if ft and url:
            fc = next((c for c, l in labels.items() if l == "facets column" and c > ft), ft + 1)
            cols = {
                "facets_table": ft,
                "facets_column": fc,
                "rest_field": fc + 1,
                "api_field": fc + 2,
                "url": url,
                "comment": next((c for c, l in labels.items() if l.startswith("comment") and c > url), url + 1),
            }
            left = {c: l for c, l in labels.items() if c < ft}
            cols["web_field"] = next((c for c, l in left.items() if l.startswith("field name")), ft - 3)
            cols["data_type"] = next((c for c, l in left.items() if l.startswith("data typ")), ft - 2)
            cols["requirement"] = next((c for c, l in left.items() if l.startswith("default")), ft - 1)
            headers.append((r, cols))
    if not headers:
        raise ConversionError(
            f"no header row found on sheet {ws.title!r}: expected a row containing the labels "
            "'Facets Table', 'Facets Column' and 'URL'"
        )

    # 2. section banners above each header ------------------------------------------------
    sections: list[RawSection] = []
    prev_header = 0
    for r, cols in headers:
        banners: list[tuple[int, str]] = []
        rr = r - 1
        while rr > prev_header and len(banners) < 3:
            ne = nonempty(rr)
            if not ne:
                if banners:
                    break
            elif len(ne) <= 2:
                banners.append((rr, _txt(cell(rr, ne[0]).value)))
            else:
                break
            rr -= 1
        title = next((t for _, t in banners if not SERVICE_WORDS.match(t)), "")
        banner_row = min((b for b, _ in banners), default=r)
        sections.append(RawSection(title or f"section_row_{r}", banner_row, r))
        prev_header = r

    # 3. data rows -------------------------------------------------------------------------
    for i, sec in enumerate(sections):
        cols = headers[i][1]
        end = (sections[i + 1].banner_row - 1) if i + 1 < len(sections) else max_row
        for r in range(sec.header_row + 1, end + 1):
            def t(name: str) -> str:
                return _txt(cell(r, cols[name]).value)

            url_cell = cell(r, cols["url"])
            url_text = _txt(url_cell.value)
            urls = [_clean_url(u) for u in URL_RE.findall(str(url_cell.value or ""))]
            target = getattr(url_cell.hyperlink, "target", None) if url_cell.hyperlink else None
            if target:
                tu = _clean_url(target)
                if tu not in urls:
                    urls.append(tu)
            row = RawRow(
                row=r,
                web_field=t("web_field"),
                data_type=t("data_type"),
                requirement=t("requirement"),
                facets_table=t("facets_table"),
                facets_column=t("facets_column"),
                rest_field=t("rest_field"),
                api_field=t("api_field"),
                url_text=url_text,
                urls=list(dict.fromkeys(urls)),
                comment=t("comment") if cols["comment"] <= max_col else "",
            )
            if any([row.rest_field, row.api_field, row.url_text, row.web_field]):
                sec.rows.append(row)
    return ws.title, sections
