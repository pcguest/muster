"""Regenerate the binary XLSX fixture.

The CSV fixtures are committed as text; this script exists so the XLSX one
can be rebuilt deterministically. Run from anywhere:

    python tests/fixtures/build_fixtures.py
"""

from pathlib import Path

import xlsxwriter

ROWS = [
    ("CUSTOMER_NUMBER", "FullName", "Email Address", "Signup Date", "LTV", "Active"),
    ("C-020", "Freya Holt", "freya.holt@example.org", "05 Mar 2024", 640.10, "Y"),
    ("C-021", "George Abara", "george.abara@example.org", "17 Sep 2022", 1875.00, "N"),
    ("C-022", "Hana Suzuki", "hana.suzuki@example.org", "sometime in June", 52.35, "Y"),
    ("C-023", "Ivan Petrov", "ivan.petrov@example.org", "22 Dec 2023", 990.00, "N"),
]


def main() -> None:
    target = Path(__file__).parent / "customers_west.xlsx"
    workbook = xlsxwriter.Workbook(target)
    sheet = workbook.add_worksheet("customers")
    for row_index, row in enumerate(ROWS):
        for column_index, value in enumerate(row):
            sheet.write(row_index, column_index, value)
    workbook.close()


if __name__ == "__main__":
    main()
