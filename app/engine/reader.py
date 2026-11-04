"""Reads .xls / .xlsx / .xlsb from bytes. The uploaded file is never modified."""
import io

from openpyxl.utils import column_index_from_string
from python_calamine import CalamineWorkbook

from .normalize import is_blank


class FileStructureError(Exception):
    pass


def read_sheet(data: bytes, sheet: str) -> list[list]:
    try:
        wb = CalamineWorkbook.from_filelike(io.BytesIO(data))
    except Exception as e:
        raise FileStructureError(f"Could not open the file as Excel ({e}).") from e
    if sheet not in wb.sheet_names:
        raise FileStructureError(
            f'Sheet "{sheet}" not found (file has: {", ".join(wb.sheet_names)}). '
            "Is this the right export / software?")
    return wb.get_sheet_by_name(sheet).to_python()


def col(letter: str) -> int:
    return column_index_from_string(letter) - 1


def cell(row: list, letter: str):
    i = col(letter)
    return row[i] if i < len(row) else None


def check_headers(rows: list[list], header_row: int, expected: dict[str, str], what: str):
    if len(rows) < header_row:
        raise FileStructureError(f"{what}: file is empty.")
    hdr = rows[header_row - 1]
    bad = [f'{c}="{e}"' for c, e in expected.items()
           if str(cell(hdr, c) or "").strip().lower() != e.lower()]
    if bad:
        raise FileStructureError(
            f"{what}: row {header_row} does not have the expected headers ({', '.join(bad)}). "
            "The file does not look like this software's export.")


def data_rows(rows: list[list], start: int, stop_blank: int):
    """Yields (excel_row_no, row) until `stop_blank` consecutive blank rows."""
    blanks = 0
    for i in range(start - 1, len(rows)):
        r = rows[i]
        if all(is_blank(v) for v in r):
            blanks += 1
            if blanks >= stop_blank:
                return
            continue
        blanks = 0
        yield i + 1, r
