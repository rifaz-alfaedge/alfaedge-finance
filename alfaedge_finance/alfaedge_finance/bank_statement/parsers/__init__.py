"""Bank statement parsers.

Each parser module exposes `detect(rows) -> bool` and `parse(rows) -> ParsedStatement`,
where `rows` is the sheet as a list of tuples of cell values. `parse_statement_file`
reads the file, picks the first parser whose `detect` accepts it, and runs it - so the
bank is identified from the file itself, not chosen by the user.

To support another bank, add a module here and append it to PARSERS.
"""

import io
from dataclasses import dataclass, field
from datetime import date

import frappe
from frappe import _


@dataclass
class StatementLine:
	sno: int
	transaction_date: date
	value_date: date | None
	particulars: str
	withdrawal: float
	deposit: float
	balance: float | None
	cheque_number: str | None = None


@dataclass
class ParsedStatement:
	bank: str
	account_number: str
	from_date: date
	to_date: date
	currency: str | None
	opening_balance: float | None
	closing_balance: float | None
	lines: list[StatementLine] = field(default_factory=list)

	@property
	def total_withdrawal(self):
		return round(sum(line.withdrawal for line in self.lines), 2)

	@property
	def total_deposit(self):
		return round(sum(line.deposit for line in self.lines), 2)


def read_rows(content: bytes, filename: str) -> list[tuple]:
	"""Return the first sheet of an .xlsx/.xls file (or a .csv) as a list of row tuples."""
	name = (filename or "").lower()
	if name.endswith(".csv"):
		import csv

		text = content.decode("utf-8-sig", errors="replace")
		return [tuple(row) for row in csv.reader(io.StringIO(text))]
	if name.endswith(".xls"):
		import xlrd

		book = xlrd.open_workbook(file_contents=content)
		sheet = book.sheet_by_index(0)
		return [tuple(sheet.row_values(i)) for i in range(sheet.nrows)]

	import warnings

	import openpyxl

	with warnings.catch_warnings():
		# Bank exports often lack a default style; openpyxl warns but reads fine.
		warnings.simplefilter("ignore")
		workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
	sheet = workbook.worksheets[0]
	rows = [tuple(row) for row in sheet.iter_rows(values_only=True)]
	workbook.close()
	return rows


def parse_statement_file(content: bytes, filename: str) -> ParsedStatement:
	from alfaedge_finance.alfaedge_finance.bank_statement.parsers import axis

	parsers = [axis]

	try:
		rows = read_rows(content, filename)
	except Exception:
		frappe.throw(_("Could not read {0} as an Excel file (.xlsx or .xls).").format(filename))

	for parser in parsers:
		if parser.detect(rows):
			return parser.parse(rows)

	frappe.throw(
		_("Unrecognised bank statement layout in {0}. Supported: Axis Bank statement export.").format(
			filename
		)
	)
