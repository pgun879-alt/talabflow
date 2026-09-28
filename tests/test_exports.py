"""Tests for spreadsheet export, including the formula-injection guard."""

from __future__ import annotations

import csv
import io

import pytest
from openpyxl import load_workbook
from sqlalchemy.orm import Session

from talabflow import repository
from talabflow.exports import COLUMNS, orders_to_csv, orders_to_xlsx
from talabflow.models import Customer, Order, OrderStatus


@pytest.fixture
def customer(session: Session) -> Customer:
    return repository.get_or_create_customer(
        session, channel="scripted", channel_user_id="1001", chat_id="1001", display_name="Amina B"
    )


def _order(session: Session, customer: Customer, **overrides: str) -> Order:
    fields = {
        "service_type": "Repair",
        "details": "The washing machine will not drain",
        "contact_phone": "0555123456",
        "address": "12 Rue Didouche Mourad, Algiers",
    }
    fields.update(overrides)
    return repository.create_order(session, customer=customer, **fields)  # type: ignore[arg-type]


def _csv_rows(payload: bytes) -> list[list[str]]:
    text = payload.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def test_csv_has_a_header_and_one_row_per_order(session: Session, customer: Customer) -> None:
    _order(session, customer)
    _order(session, customer, details="The fridge is making a loud noise")
    rows = _csv_rows(orders_to_csv(repository.list_orders(session).items))
    assert rows[0] == [header for header, _ in COLUMNS]
    assert len(rows) == 3


def test_csv_is_written_with_a_bom_so_excel_reads_arabic(
    session: Session, customer: Customer
) -> None:
    """Without the BOM, Excel on Windows shows Arabic as mojibake -- mangling half the data."""
    _order(session, customer, details="الغسالة لا تصرف الماء", address="حي النصر، الجزائر")
    payload = orders_to_csv(repository.list_orders(session).items)
    assert payload.startswith(b"\xef\xbb\xbf")
    assert "الغسالة لا تصرف الماء" in payload.decode("utf-8-sig")


def test_csv_contains_the_expected_values(session: Session, customer: Customer) -> None:
    order = _order(session, customer)
    rows = _csv_rows(orders_to_csv([order]))
    row = dict(zip(rows[0], rows[1], strict=True))
    assert row["Reference"] == order.reference
    assert row["Status"] == "new"
    assert row["Service"] == "Repair"
    assert row["Phone"] == "0555123456"
    assert row["Customer"] == "Amina B"
    assert row["Channel"] == "scripted"
    assert row["Created (UTC)"]


def test_empty_export_still_produces_a_header(session: Session) -> None:
    rows = _csv_rows(orders_to_csv([]))
    assert len(rows) == 1
    assert rows[0] == [header for header, _ in COLUMNS]


@pytest.mark.parametrize(
    "payload",
    [
        '=HYPERLINK("http://evil.example","Click me")',
        "+1+1",
        "-1+1",
        "@SUM(A1:A9)",
        "=cmd|'/c calc'!A1",
    ],
)
def test_formula_injection_is_neutralised_in_csv(
    session: Session, customer: Customer, payload: str
) -> None:
    """A customer types a formula into a chat field; staff open the export in Excel.

    Without escaping, the spreadsheet evaluates it -- turning a free-text field into code
    execution on the buyer's machine. Prefixing an apostrophe forces the cell to stay text.
    """
    order = _order(session, customer, details=payload)
    rows = _csv_rows(orders_to_csv([order]))
    cell = dict(zip(rows[0], rows[1], strict=True))["Details"]
    assert cell.startswith("'"), cell
    assert not cell.startswith(("=", "+", "-", "@"))


def test_formula_injection_is_neutralised_in_xlsx(session: Session, customer: Customer) -> None:
    order = _order(session, customer, details='=HYPERLINK("http://evil.example","x")')
    workbook = load_workbook(io.BytesIO(orders_to_xlsx([order])))
    sheet = workbook.active
    headers = [cell.value for cell in sheet[1]]
    value = sheet.cell(row=2, column=headers.index("Details") + 1).value
    assert str(value).startswith("'")


def test_ordinary_text_is_not_mangled(session: Session, customer: Customer) -> None:
    """The guard must not corrupt normal data -- a leading hyphen is the only risky case."""
    order = _order(session, customer, details="Washing machine, model X-200, will not drain")
    rows = _csv_rows(orders_to_csv([order]))
    assert dict(zip(rows[0], rows[1], strict=True))["Details"] == (
        "Washing machine, model X-200, will not drain"
    )


def test_xlsx_is_a_valid_workbook_with_a_frozen_header(
    session: Session, customer: Customer
) -> None:
    _order(session, customer)
    workbook = load_workbook(io.BytesIO(orders_to_xlsx(repository.list_orders(session).items)))
    sheet = workbook.active
    assert sheet.title == "Orders"
    assert [cell.value for cell in sheet[1]] == [header for header, _ in COLUMNS]
    assert sheet.freeze_panes == "A2"
    assert sheet[1][0].font.bold
    assert sheet.max_row == 2


def test_xlsx_column_widths_are_bounded(session: Session, customer: Customer) -> None:
    """A 2,000-character details field must not produce an unusable column."""
    _order(session, customer, details="x" * 2000)
    workbook = load_workbook(io.BytesIO(orders_to_xlsx(repository.list_orders(session).items)))
    sheet = workbook.active
    for dimension in sheet.column_dimensions.values():
        assert dimension.width <= 50


def test_xlsx_of_an_empty_list_has_no_autofilter(session: Session) -> None:
    workbook = load_workbook(io.BytesIO(orders_to_xlsx([])))
    sheet = workbook.active
    assert sheet.max_row == 1
    assert sheet.auto_filter.ref is None


def test_both_formats_share_one_column_definition(session: Session, customer: Customer) -> None:
    """A single COLUMNS table is what stops CSV and XLSX drifting apart."""
    _order(session, customer)
    orders = repository.list_orders(session).items
    csv_header = _csv_rows(orders_to_csv(orders))[0]
    sheet = load_workbook(io.BytesIO(orders_to_xlsx(orders))).active
    assert csv_header == [cell.value for cell in sheet[1]]


def test_status_is_exported_as_its_value_not_the_enum_repr(
    session: Session, customer: Customer
) -> None:
    order = _order(session, customer)
    repository.change_order_status(
        session, order=order, to_status=OrderStatus.CONFIRMED, actor="amina"
    )
    rows = _csv_rows(orders_to_csv([order]))
    assert dict(zip(rows[0], rows[1], strict=True))["Status"] == "confirmed"
