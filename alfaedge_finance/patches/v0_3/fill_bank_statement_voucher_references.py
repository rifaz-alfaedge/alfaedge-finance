"""Fill the new Cheque/Reference No column of existing Bank Statement Review voucher rows
from the linked Payment Entry / Journal Entry."""

import frappe


def execute():
	for doctype, field in (("Payment Entry", "reference_no"), ("Journal Entry", "cheque_no")):
		frappe.db.sql(
			f"""
			update `tabBank Statement Review Voucher` row
			join `tab{doctype}` voucher on voucher.name = row.voucher_no
			set row.reference_no = voucher.{field}
			where row.voucher_type = %s
			""",
			doctype,
		)
