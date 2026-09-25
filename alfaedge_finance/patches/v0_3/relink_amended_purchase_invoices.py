"""Purchase Expense Centers whose invoice was cancelled and amended still point at the
cancelled one (and say "Invoice Submitted"). Point them at the live amendment, or mark
them Invoice Cancelled when there is none."""

import frappe

from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.invoice_creation import (
	STATUS_BY_DOCSTATUS,
)


def execute():
	rows = frappe.db.sql(
		"""
		select pec.name
		from `tabPurchase Expense Center` pec
		join `tabPurchase Invoice` pi on pi.name = pec.purchase_invoice
		where pi.docstatus = 2
		""",
		pluck=True,
	)
	for name in rows:
		live = frappe.get_all(
			"Purchase Invoice",
			filters={"purchase_expense_center": name, "docstatus": ["<", 2]},
			fields=["name", "docstatus"],
			order_by="creation desc",
			limit=1,
		)
		if live:
			values = {"purchase_invoice": live[0].name, "invoice_status": STATUS_BY_DOCSTATUS[live[0].docstatus]}
		else:
			values = {"invoice_status": "Invoice Cancelled"}
		frappe.db.set_value("Purchase Expense Center", name, values, update_modified=False)
