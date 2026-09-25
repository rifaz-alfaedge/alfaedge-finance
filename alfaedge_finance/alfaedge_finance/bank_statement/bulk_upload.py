"""Read an Axis bulk-payment upload file - the one the Bulk Payment CSV page generates
(and the bank then executes as a single `NEFT/<batch>/<count>/AW...` debit).

Columns are found by header prefix, ignoring "(Mandatory)" / "(Non Mandatory)":
Debit Account Number, Transaction Amount, Beneficiary Name, Beneficiary Account Number,
Beneficiary IFSC Code, Transaction Date, Customer Reference Number.
"""

import re
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import flt

from alfaedge_finance.alfaedge_finance.bank_statement.parsers import read_rows

COLUMNS = {
	"debit_account_no": "debit account number",
	"amount": "transaction amount",
	"beneficiary_name": "beneficiary name",
	"account_no": "beneficiary account number",
	"ifsc": "beneficiary ifsc code",
	"transaction_date": "transaction date",
	"customer_ref_no": "customer reference number",
}
REQUIRED = ("amount", "account_no")


@dataclass
class BulkRow:
	row_no: int
	beneficiary_name: str
	account_no: str
	ifsc: str
	amount: float
	debit_account_no: str
	customer_ref_no: str
	transaction_date: str


def normalize_account_no(value) -> str:
	"""Account numbers as the bank and ERPNext store them can differ in spacing, and
	Excel turns long numbers into floats (9.2402E+14); compare on digits/letters only."""
	if value is None:
		return ""
	if isinstance(value, float) and value.is_integer():
		value = int(value)
	return re.sub(r"[^0-9A-Za-z]", "", str(value)).upper()


def _header_label(cell) -> str:
	text = str(cell or "").strip().lower()
	return re.sub(r"\s*\((non\s+)?mandatory\)\s*$", "", text).strip()


def parse_bulk_file(content: bytes, filename: str) -> list[BulkRow]:
	try:
		rows = read_rows(content, filename)
	except Exception:
		frappe.throw(_("Could not read {0} as an Excel or CSV file.").format(filename))

	header_index = next(
		(
			i
			for i, row in enumerate(rows)
			if any(_header_label(c).startswith(COLUMNS["account_no"]) for c in row)
		),
		None,
	)
	if header_index is None:
		frappe.throw(_("No 'Beneficiary Account Number' column found in {0}.").format(filename))

	columns = {}
	for index, cell in enumerate(rows[header_index]):
		label = _header_label(cell)
		for key, prefix in COLUMNS.items():
			if key not in columns and label.startswith(prefix):
				columns[key] = index
	missing = [COLUMNS[k] for k in REQUIRED if k not in columns]
	if missing:
		frappe.throw(_("The bulk file is missing columns: {0}").format(", ".join(missing)))

	def get(row, key):
		index = columns.get(key)
		return row[index] if index is not None and index < len(row) else None

	parsed = []
	for row in rows[header_index + 1 :]:
		account_no = normalize_account_no(get(row, "account_no"))
		amount_text = str(get(row, "amount") or "").replace(",", "").strip()
		if not account_no and not amount_text:
			continue
		try:
			amount = flt(float(amount_text), 2)
		except ValueError:
			frappe.throw(_("Row {0}: '{1}' is not an amount.").format(len(parsed) + 1, amount_text))
		parsed.append(
			BulkRow(
				row_no=len(parsed) + 1,
				beneficiary_name=str(get(row, "beneficiary_name") or "").strip(),
				account_no=account_no,
				ifsc=str(get(row, "ifsc") or "").strip(),
				amount=amount,
				debit_account_no=normalize_account_no(get(row, "debit_account_no")),
				customer_ref_no=str(get(row, "customer_ref_no") or "").strip(),
				transaction_date=str(get(row, "transaction_date") or "").strip(),
			)
		)
	if not parsed:
		frappe.throw(_("No payment rows found in {0}.").format(filename))
	return parsed
