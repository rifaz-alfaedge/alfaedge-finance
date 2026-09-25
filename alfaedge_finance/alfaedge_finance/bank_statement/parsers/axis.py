"""Axis Bank "Statement Enquiry" export (.xlsx/.xls).

Layout: ~18 rows of account-holder details, then a
"Statement of Account No - <acc> for the period (From : dd/mm/yyyy To : dd/mm/yyyy )"
row, then a header row (S.NO, Transaction Date, Value Date, Particulars, Amount(INR),
Debit/Credit, Balance(INR), Cheque Number, Branch Name) followed by an OPENING BALANCE
row, the transactions, TRANSACTION TOTAL and CLOSING BALANCE rows, and a legend.
Amounts are Indian-formatted strings ("9,66,370.79") with a separate DR/CR column.
"""

import re
from datetime import date, datetime

import frappe
from frappe import _

from alfaedge_finance.alfaedge_finance.bank_statement.parsers import ParsedStatement, StatementLine

BANK = "Axis Bank"

_PERIOD_RE = re.compile(
	r"Account\s+No\s*-\s*(\d+).*?From\s*:\s*(\d{2}/\d{2}/\d{4})\s*To\s*:\s*(\d{2}/\d{2}/\d{4})",
	re.IGNORECASE,
)
_ACCOUNT_RE = re.compile(r"Account\s+No\s*-\s*(\d+)", re.IGNORECASE)

HEADERS = {
	"sno": "s.no",
	"transaction_date": "transaction date",
	"value_date": "value date",
	"particulars": "particulars",
	"amount": "amount",
	"dr_cr": "debit/credit",
	"balance": "balance",
	"cheque_number": "cheque number",
}


def _cell_text(value) -> str:
	if value is None:
		return ""
	return str(value).strip()


def _find_header_row(rows):
	for index, row in enumerate(rows):
		cells = [_cell_text(c).lower() for c in row]
		if any(c.startswith("transaction date") for c in cells) and any(c == "particulars" for c in cells):
			return index
	return None


def detect(rows) -> bool:
	if _find_header_row(rows) is None:
		return False
	return any(_ACCOUNT_RE.search(_cell_text(row[0])) for row in rows[:40] if row)


def parse_amount(value) -> float | None:
	if value is None:
		return None
	if isinstance(value, int | float):
		return round(float(value), 2)
	text = str(value).strip().replace(",", "")
	if not text:
		return None
	try:
		return round(float(text), 2)
	except ValueError:
		return None


def parse_date(value) -> date | None:
	if value is None:
		return None
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	text = str(value).strip()
	if not text:
		return None
	try:
		return datetime.strptime(text, "%d/%m/%Y").date()
	except ValueError:
		return None


def _column_map(header_row):
	columns = {}
	for index, cell in enumerate(header_row):
		text = _cell_text(cell).lower()
		for key, prefix in HEADERS.items():
			if key not in columns and text.startswith(prefix):
				columns[key] = index
	missing = [k for k in ("transaction_date", "particulars", "amount", "dr_cr") if k not in columns]
	if missing:
		frappe.throw(_("Axis statement header is missing columns: {0}").format(", ".join(missing)))
	return columns


def parse(rows) -> ParsedStatement:
	account_number = from_date = to_date = currency = None
	for row in rows:
		first = _cell_text(row[0]) if row else ""
		match = _PERIOD_RE.search(first)
		if match:
			account_number = match.group(1)
			from_date = parse_date(match.group(2))
			to_date = parse_date(match.group(3))
		elif not account_number and _ACCOUNT_RE.search(first):
			account_number = _ACCOUNT_RE.search(first).group(1)
		if first.lower() == "currency" and len(row) > 2 and _cell_text(row[2]):
			currency = _cell_text(row[2]).upper()

	header_index = _find_header_row(rows)
	columns = _column_map(rows[header_index])

	def get(row, key):
		index = columns.get(key)
		if index is None or index >= len(row):
			return None
		return row[index]

	opening_balance = closing_balance = None
	lines = []
	for row in rows[header_index + 1 :]:
		particulars = _cell_text(get(row, "particulars"))
		label = particulars.upper()
		if label == "OPENING BALANCE":
			opening_balance = parse_amount(get(row, "balance"))
			continue
		if label == "CLOSING BALANCE":
			closing_balance = parse_amount(get(row, "balance"))
			break
		if label.startswith("TRANSACTION TOTAL"):
			continue

		transaction_date = parse_date(get(row, "transaction_date"))
		if not transaction_date:
			continue

		amount = parse_amount(get(row, "amount")) or 0.0
		dr_cr = _cell_text(get(row, "dr_cr")).upper()
		if dr_cr not in ("DR", "CR"):
			frappe.throw(
				_("Row {0}: Debit/Credit must be DR or CR, got '{1}'").format(
					_cell_text(get(row, "sno")) or len(lines) + 1, dr_cr
				)
			)

		lines.append(
			StatementLine(
				sno=len(lines) + 1,
				transaction_date=transaction_date,
				value_date=parse_date(get(row, "value_date")),
				particulars=particulars,
				withdrawal=amount if dr_cr == "DR" else 0.0,
				deposit=amount if dr_cr == "CR" else 0.0,
				balance=parse_amount(get(row, "balance")),
				cheque_number=_cell_text(get(row, "cheque_number")) or None,
			)
		)

	if not lines:
		frappe.throw(_("No transactions found in the statement."))
	if not account_number:
		frappe.throw(_("Could not find the account number in the statement header."))

	statement = ParsedStatement(
		bank=BANK,
		account_number=account_number,
		from_date=from_date or min(line.transaction_date for line in lines),
		to_date=to_date or max(line.transaction_date for line in lines),
		currency=currency,
		opening_balance=opening_balance,
		closing_balance=closing_balance,
		lines=lines,
	)
	validate_balances(statement)
	return statement


def validate_balances(statement: ParsedStatement):
	"""Every row's running balance must follow from the previous one, and opening +
	credits - debits must equal closing. A mismatch means rows were lost or misread,
	so the statement is rejected rather than half-imported."""
	previous = statement.opening_balance
	for line in statement.lines:
		if previous is not None and line.balance is not None:
			expected = round(previous + line.deposit - line.withdrawal, 2)
			if abs(expected - line.balance) > 0.005:
				frappe.throw(
					_(
						"Statement row {0} ({1}): running balance {2} does not follow from the previous balance {3}."
					).format(line.sno, line.transaction_date.strftime("%d/%m/%Y"), line.balance, previous)
				)
		if line.balance is not None:
			previous = line.balance

	if statement.opening_balance is not None and statement.closing_balance is not None:
		expected_closing = round(
			statement.opening_balance + statement.total_deposit - statement.total_withdrawal, 2
		)
		if abs(expected_closing - statement.closing_balance) > 0.005:
			frappe.throw(
				_(
					"Opening balance {0} + credits {1} - debits {2} = {3}, but the statement's closing balance is {4}."
				).format(
					statement.opening_balance,
					statement.total_deposit,
					statement.total_withdrawal,
					expected_closing,
					statement.closing_balance,
				)
			)
