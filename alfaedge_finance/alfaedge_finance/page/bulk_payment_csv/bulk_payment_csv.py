# Copyright (c) 2026, alfaEdge
# Page: Bulk Payment CSV
#
# Whitelisted method that returns bulk-payment rows as JSON for the
# client-side editable table in bulk_payment_csv.js.
#
#   - Salary            -> Salary Slips under a selected Payroll Entry
#   - Expense Claims    -> unpaid claims, summed per employee
#   - Purchase Invoices -> overdue invoices, summed per supplier

import json
import re

import frappe
from frappe.utils import flt, formatdate

# Same roles as the page itself - the methods below expose salaries and bank account
# numbers, and whitelisted methods are otherwise callable by any logged-in user.
PAGE_ROLES = ("Accounts Manager", "System Manager")


@frappe.whitelist()
def get_payroll_entries(company):
	frappe.only_for(PAGE_ROLES)
	entries = frappe.get_all(
		"Payroll Entry",
		filters={"company": company, "docstatus": 1},
		fields=["name", "end_date"],
		order_by="end_date desc",
	)
	result = []
	for e in entries:
		label = frappe.utils.getdate(e.end_date).strftime("%B %Y") if e.end_date else e.name
		result.append({"name": e.name, "label": label})
	return result


@frappe.whitelist()
def get_rows(filters):
	frappe.only_for(PAGE_ROLES)
	if isinstance(filters, str):
		filters = json.loads(filters)
	filters = frappe._dict(filters or {})
	validate_filters(filters)

	debit_account_no = get_debit_account_no(filters.company)
	txn_date_str = formatdate(filters.transaction_date, "dd/MM/yyyy")

	if filters.txn_type == "Salary":
		return get_salary_rows(filters, debit_account_no, txn_date_str)
	elif filters.txn_type == "Expense Claims":
		return get_expense_claim_rows(filters, debit_account_no, txn_date_str)
	elif filters.txn_type == "Purchase Invoices":
		return get_purchase_invoice_rows(filters, debit_account_no, txn_date_str)
	return []


def validate_filters(filters):
	if not filters.company:
		frappe.throw("Company is mandatory")
	if not filters.transaction_date:
		frappe.throw("Transaction Date is mandatory")
	if not filters.txn_type:
		frappe.throw("Type is mandatory")
	if filters.txn_type == "Salary" and not filters.get("payroll_entry"):
		frappe.throw("Please select a Payroll Entry")


def get_debit_account_no(company):
	debit_account_no = frappe.db.get_value(
		"Bank Account",
		{"company": company, "is_default": 1, "is_company_account": 1},
		"bank_account_no",
	)
	if not debit_account_no:
		frappe.throw(
			f"No default company Bank Account (is_default & is_company_account both checked) "
			f"found for Company '{company}'"
		)
	return debit_account_no


def get_beneficiary_bank_account(party_type, party):
	fields = ["account_name", "bank_account_no", "branch_code"]
	bank_account = frappe.db.get_value(
		"Bank Account",
		{"party_type": party_type, "party": party, "is_default": 1},
		fields,
		as_dict=True,
	)
	if not bank_account:
		# fall back to any linked bank account if none is flagged as default
		bank_account = frappe.db.get_value(
			"Bank Account",
			{"party_type": party_type, "party": party},
			fields,
			as_dict=True,
		)
	return bank_account


def make_customer_ref_no(account_name, txn_date_str, bank_account_no):
	clean_name = re.sub(r"[^A-Za-z0-9]", "", account_name or "")
	prefix = (clean_name[:2] or "XX").upper()
	ddmm = "".join(txn_date_str.split("/")[:2])  # "DD" + "MM" from "DD/MM/YYYY"
	last3 = re.sub(r"[^0-9A-Za-z]", "", bank_account_no or "")[-3:]
	return f"{prefix}{ddmm}{last3}"


def make_nickname(beneficiary_name):
	return re.sub(r"[^A-Za-z0-9]", "", beneficiary_name or "").lower()


def build_row(debit_account_no, amount, bank_account, txn_date_str, party_label, reason=None):
	# Sums of floats drift (14320.229999999998) and this goes straight into the bank file.
	amount = flt(amount, 2)
	if not bank_account or reason:
		return {
			"matched": False,
			"reason": reason or "No Bank Account found",
			"party_label": party_label,
			"debit_account_no": debit_account_no,
			"transaction_amount": amount,
			"beneficiary_name": "",
			"beneficiary_account_no": "",
			"beneficiary_ifsc": "",
			"transaction_date": txn_date_str,
			"payment_mode": "N",
			"customer_ref_no": "",
			"beneficiary_nickname": "",
		}
	beneficiary_name = bank_account.account_name
	return {
		"matched": True,
		"party_label": party_label,
		"debit_account_no": debit_account_no,
		"transaction_amount": amount,
		"beneficiary_name": beneficiary_name,
		"beneficiary_account_no": bank_account.bank_account_no,
		"beneficiary_ifsc": bank_account.branch_code,
		"transaction_date": txn_date_str,
		"payment_mode": "N",
		"customer_ref_no": make_customer_ref_no(beneficiary_name, txn_date_str, bank_account.bank_account_no),
		"beneficiary_nickname": make_nickname(beneficiary_name),
	}


def get_salary_rows(filters, debit_account_no, txn_date_str):
	slips = frappe.get_all(
		"Salary Slip",
		filters={
			"payroll_entry": filters.payroll_entry,
			"company": filters.company,
			"docstatus": 1,
		},
		fields=["employee", "employee_name", "net_pay"],
	)

	rows = []
	for slip in slips:
		bank_account = get_beneficiary_bank_account("Employee", slip.employee)
		rows.append(
			build_row(debit_account_no, slip.net_pay, bank_account, txn_date_str,
				slip.employee_name or slip.employee)
		)
	return rows


def get_expense_claim_rows(filters, debit_account_no, txn_date_str):
	claims = frappe.get_all(
		"Expense Claim",
		filters={
			"company": filters.company,
			"docstatus": 1,
			"approval_status": "Approved",
		},
		fields=["employee", "employee_name", "grand_total", "total_advance_amount", "total_amount_reimbursed"],
	)

	totals = {}
	names = {}
	for c in claims:
		# Same outstanding as HRMS: sanctioned + taxes, less advances and reimbursements.
		outstanding = flt(c.grand_total) - flt(c.total_advance_amount) - flt(c.total_amount_reimbursed)
		if outstanding <= 0:
			continue
		totals[c.employee] = totals.get(c.employee, 0) + outstanding
		names[c.employee] = c.employee_name

	rows = []
	for employee, amount in totals.items():
		bank_account = get_beneficiary_bank_account("Employee", employee)
		rows.append(
			build_row(debit_account_no, amount, bank_account, txn_date_str, names.get(employee, employee))
		)
	return rows


def get_purchase_invoice_rows(filters, debit_account_no, txn_date_str):
	invoices = frappe.get_all(
		"Purchase Invoice",
		filters={
			"company": filters.company,
			"docstatus": 1,
			"outstanding_amount": [">", 0],
			# Due on the payment day counts too - otherwise it slips to the next run.
			"due_date": ["<=", filters.transaction_date],
			"on_hold": 0,
		},
		fields=["supplier", "supplier_name", "outstanding_amount", "currency"],
	)
	company_currency = frappe.get_cached_value("Company", filters.company, "default_currency")

	totals = {}
	names = {}
	foreign = {}
	for inv in invoices:
		names[inv.supplier] = inv.supplier_name
		if inv.currency != company_currency:
			# A USD invoice's outstanding is in USD - adding it to rupee totals would pay
			# $150 as Rs 150. Listed separately; these are paid by wire, not NEFT.
			key = (inv.supplier, inv.currency)
			foreign[key] = foreign.get(key, 0) + inv.outstanding_amount
			continue
		totals[inv.supplier] = totals.get(inv.supplier, 0) + inv.outstanding_amount

	rows = [
		build_row(
			debit_account_no,
			amount,
			None,
			txn_date_str,
			names.get(supplier, supplier),
			reason=f"Billed in {currency} - pay separately",
		)
		for (supplier, currency), amount in foreign.items()
	]
	advances = get_open_supplier_advances(filters.company, list(totals), filters.transaction_date)
	for supplier, amount in totals.items():
		label = names.get(supplier, supplier)
		advance = flt(advances.get(supplier), 2)
		if advance and amount - advance <= 0:
			rows.append(
				build_row(
					debit_account_no,
					amount,
					None,
					txn_date_str,
					label,
					reason=f"Covered by open advance of {advance:,.2f}",
				)
			)
			continue
		bank_account = get_beneficiary_bank_account("Supplier", supplier)
		row = build_row(debit_account_no, amount - advance, bank_account, txn_date_str, label)
		if advance:
			row.update(invoices_due=flt(amount, 2), advance_deducted=advance)
		rows.append(row)
	return rows


def get_open_supplier_advances(company, suppliers, up_to_date):
	"""Money already paid to each supplier and not yet set against an invoice - paying the
	full overdue total on top of it would overpay them. Payment Entries' unallocated amount,
	plus advance Journal Entry rows not linked to any invoice (both in company currency;
	only company-currency suppliers reach this)."""
	if not suppliers:
		return {}
	advances = {}
	for supplier, amount in frappe.db.sql(
		"""
		select party, sum(unallocated_amount)
		from `tabPayment Entry`
		where docstatus = 1 and company = %(company)s and payment_type = 'Pay'
			and party_type = 'Supplier' and party in %(suppliers)s
			and unallocated_amount > 0 and posting_date <= %(date)s
		group by party
		""",
		{"company": company, "suppliers": suppliers, "date": up_to_date},
	):
		advances[supplier] = advances.get(supplier, 0) + flt(amount)
	for supplier, amount in frappe.db.sql(
		"""
		select jea.party, sum(jea.debit - jea.credit)
		from `tabJournal Entry Account` jea
		join `tabJournal Entry` je on je.name = jea.parent
		where je.docstatus = 1 and je.company = %(company)s and je.posting_date <= %(date)s
			and jea.party_type = 'Supplier' and jea.party in %(suppliers)s
			and jea.is_advance = 'Yes' and ifnull(jea.reference_name, '') = ''
		group by jea.party
		""",
		{"company": company, "suppliers": suppliers, "date": up_to_date},
	):
		if flt(amount) > 0:
			advances[supplier] = advances.get(supplier, 0) + flt(amount)
	return advances


# The bank's template - column order and labels must match it exactly.
EXPORT_COLUMNS = [
	("Debit Account Number (Mandatory)", "debit_account_no"),
	("Transaction Amount (Mandatory)", "transaction_amount"),
	("Beneficiary Name (Mandatory)", "beneficiary_name"),
	("Beneficiary Account Number (Mandatory)", "beneficiary_account_no"),
	("Beneficiary IFSC Code (Mandatory)", "beneficiary_ifsc"),
	("Transaction Date (Mandatory)", "transaction_date"),
	("Payment Mode (Mandatory)", "payment_mode"),
	("Customer Reference Number (Mandatory)", "customer_ref_no"),
	("Beneficiary Nickname/Code (Mandatory)", "beneficiary_nickname"),
]


@frappe.whitelist()
def download_xlsx(rows, txn_type=None):
	"""Build the bank file on the server (openpyxl) from the rows as edited on the page -
	no spreadsheet library fetched from a CDN into a page showing salaries and account
	numbers. Every column except the amount is a text cell, so Excel or the bank's reader
	never turns a long account number into 9.2402E+14."""
	import io

	from openpyxl import Workbook

	frappe.only_for(PAGE_ROLES)
	rows = frappe.parse_json(rows) or []

	workbook = Workbook()
	sheet = workbook.active
	sheet.title = "Bulk Payment"
	sheet.append([label for label, _ in EXPORT_COLUMNS])
	for row in rows:
		values = []
		for _, fieldname in EXPORT_COLUMNS:
			value = row.get(fieldname)
			if fieldname == "transaction_amount":
				values.append(flt(value, 2))
			else:
				values.append("" if value is None else str(value))
		sheet.append(values)
	for column in sheet.iter_cols(min_row=2):
		for cell in column:
			if cell.data_type == "s":
				cell.number_format = "@"

	buffer = io.BytesIO()
	workbook.save(buffer)
	safe_type = re.sub(r"[^A-Za-z0-9]+", "_", txn_type or "Payments")
	frappe.response["filename"] = f"Bulk_Payment_{safe_type}_{frappe.utils.today()}.xlsx"
	frappe.response["filecontent"] = buffer.getvalue()
	frappe.response["type"] = "binary"
