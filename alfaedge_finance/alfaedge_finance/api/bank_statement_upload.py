"""Direct upload of a bank statement file from Desk ("Upload Bank Statement" on the
Bank Statement Review list). The bank account and period are read from the file itself,
so any range works - daily, weekly, monthly or custom.

    POST /api/method/alfaedge_finance.alfaedge_finance.api.bank_statement_upload.upload_bank_statement
    {"filename": "26_04.XLSX", "content": "<base64>", "bank_account": null}
"""

import base64

import frappe
from frappe import _

from alfaedge_finance.alfaedge_finance.bank_statement.parsers import parse_statement_file


@frappe.whitelist()
def upload_bank_statement(filename, content, bank_account=None):
	frappe.has_permission("Bank Statement Review", "create", throw=True)
	file_bytes = base64.b64decode(content)
	statement = parse_statement_file(file_bytes, filename)

	if not bank_account:
		matches = frappe.get_all(
			"Bank Account",
			filters={"is_company_account": 1, "bank_account_no": statement.account_number},
			pluck="name",
		)
		if not matches:
			frappe.throw(
				_(
					"No company Bank Account has account number {0}. Set it on the Bank Account, or choose the Bank Account explicitly."
				).format(statement.account_number)
			)
		if len(matches) > 1:
			frappe.throw(
				_(
					"More than one company Bank Account has account number {0}: {1}. Choose one explicitly."
				).format(statement.account_number, ", ".join(matches))
			)
		bank_account = matches[0]

	file_doc = frappe.get_doc(
		{"doctype": "File", "file_name": filename, "content": file_bytes, "is_private": 1}
	).insert()

	review = frappe.get_doc(
		{
			"doctype": "Bank Statement Review",
			"bank_account": bank_account,
			"statement_file": file_doc.file_url,
		}
	).insert()

	file_doc.db_set(
		{
			"attached_to_doctype": review.doctype,
			"attached_to_name": review.name,
			"attached_to_field": "statement_file",
		}
	)
	return review.name
