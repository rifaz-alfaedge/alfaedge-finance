"""Sales / Purchase Orders as documents a bank line can settle (advance payments).

An order's outstanding is its (rounded) total less the advance already paid against
it, while it is submitted, not fully billed, and not Closed / Completed / On Hold.
"""

import frappe
from frappe.utils import flt

ORDER_PARTY_FIELD = {"Purchase Order": "supplier", "Sales Order": "customer"}
ORDER_PARTY_TYPE = {"Purchase Order": "Supplier", "Sales Order": "Customer"}
INACTIVE_STATUSES = ("Closed", "Completed", "On Hold", "Cancelled")


def _outstanding_sql():
	return "(if(ifnull(rounded_total, 0) > 0, rounded_total, grand_total) - ifnull(advance_paid, 0))"


def open_orders(doctype, company, currency=None, party=None, amount=None) -> list[dict]:
	"""Open orders, oldest first: [{doctype, name, party, amount, date}]. With `amount`,
	only orders whose outstanding equals it exactly."""
	party_field = ORDER_PARTY_FIELD[doctype]
	conditions = [
		"docstatus = 1",
		"company = %(company)s",
		"ifnull(per_billed, 0) < 100",
		"status not in %(inactive)s",
		f"{_outstanding_sql()} > 0.005",
	]
	if currency:
		conditions.append("currency = %(currency)s")
	if party:
		conditions.append(f"{party_field} = %(party)s")
	if amount is not None:
		conditions.append(f"abs({_outstanding_sql()} - %(amount)s) < 0.005")
	rows = frappe.db.sql(
		f"""
		select name, {party_field} as party, transaction_date, {_outstanding_sql()} as outstanding
		from `tab{doctype}`
		where {" and ".join(conditions)}
		order by transaction_date asc, name asc
		""",
		{
			"company": company,
			"currency": currency,
			"party": party,
			"amount": amount,
			"inactive": INACTIVE_STATUSES,
		},
		as_dict=True,
	)
	return [
		{
			"doctype": doctype,
			"name": r.name,
			"party": r.party,
			"amount": flt(r.outstanding, 2),
			"date": r.transaction_date,
		}
		for r in rows
	]


def order_details(doctype, name) -> frappe._dict:
	"""{total, outstanding, date} of one order."""
	doc = frappe.db.get_value(
		doctype,
		name,
		["grand_total", "rounded_total", "advance_paid", "transaction_date"],
		as_dict=True,
	)
	total = flt(doc.rounded_total) or flt(doc.grand_total)
	return frappe._dict(
		total=total, outstanding=flt(total - flt(doc.advance_paid), 2), date=doc.transaction_date
	)
