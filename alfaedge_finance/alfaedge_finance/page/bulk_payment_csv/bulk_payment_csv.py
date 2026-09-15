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
from frappe.utils import formatdate


@frappe.whitelist()
def get_payroll_entries(company):
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


def build_row(debit_account_no, amount, bank_account, txn_date_str, party_label):
	if not bank_account:
		return {
			"matched": False,
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
		},
		fields=["employee", "employee_name", "total_sanctioned_amount", "total_amount_reimbursed"],
	)

	totals = {}
	names = {}
	for c in claims:
		outstanding = (c.total_sanctioned_amount or 0) - (c.total_amount_reimbursed or 0)
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
			"due_date": ["<", filters.transaction_date],
		},
		fields=["supplier", "supplier_name", "outstanding_amount"],
	)

	totals = {}
	names = {}
	for inv in invoices:
		totals[inv.supplier] = totals.get(inv.supplier, 0) + inv.outstanding_amount
		names[inv.supplier] = inv.supplier_name

	rows = []
	for supplier, amount in totals.items():
		bank_account = get_beneficiary_bank_account("Supplier", supplier)
		rows.append(
			build_row(debit_account_no, amount, bank_account, txn_date_str, names.get(supplier, supplier))
		)
	return rows
