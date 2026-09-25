"""Link-field search for the document a bank line settles, showing each candidate's
party, date, outstanding and total in the dropdown (the standard search only shows the
doctype's search fields, not amounts)."""

import frappe
from frappe.utils import flt, fmt_money

ORDER_TOTAL = "if(ifnull(rounded_total, 0) > 0, rounded_total, grand_total)"
ORDER_OPEN = "ifnull(per_billed, 0) < 100 and status not in ('Closed', 'Completed', 'On Hold')"

# doctype -> (party field, date field, total, outstanding, open condition)
SETTLEABLE = {
	"Purchase Invoice": ("supplier", "posting_date", "grand_total", "outstanding_amount", "1 = 1"),
	"Sales Invoice": ("customer", "posting_date", "grand_total", "outstanding_amount", "1 = 1"),
	"Expense Claim": (
		"employee",
		"posting_date",
		"grand_total",
		"grand_total - ifnull(total_amount_reimbursed, 0)",
		"status = 'Unpaid'",
	),
	"Purchase Order": (
		"supplier",
		"transaction_date",
		ORDER_TOTAL,
		f"{ORDER_TOTAL} - ifnull(advance_paid, 0)",
		ORDER_OPEN,
	),
	"Sales Order": (
		"customer",
		"transaction_date",
		ORDER_TOTAL,
		f"{ORDER_TOTAL} - ifnull(advance_paid, 0)",
		ORDER_OPEN,
	),
}


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def search_settleable(doctype, txt, searchfield, start, page_len, filters):
	if doctype not in SETTLEABLE:
		return []
	frappe.has_permission(doctype, "read", throw=True)
	party_field, date_field, total, outstanding, open_condition = SETTLEABLE[doctype]
	filters = filters or {}

	conditions = ["docstatus = 1", open_condition, f"({outstanding}) > 0.005"]
	values = {"txt": f"%{txt}%", "start": int(start), "page_len": int(page_len)}
	if filters.get("company"):
		conditions.append("company = %(company)s")
		values["company"] = filters["company"]
	if filters.get("party"):
		conditions.append(f"{party_field} = %(party)s")
		values["party"] = filters["party"]
	conditions.append(f"(name like %(txt)s or {party_field} like %(txt)s)")

	currency_field = "'' as currency" if doctype == "Expense Claim" else "currency"
	rows = frappe.db.sql(
		f"""
		select name, {party_field} as party, {date_field} as date, {total} as total,
			{outstanding} as outstanding, {currency_field}
		from `tab{doctype}`
		where {" and ".join(conditions)}
		order by {date_field} desc, name desc
		limit %(start)s, %(page_len)s
		""",
		values,
		as_dict=True,
	)
	company_currency = (
		frappe.get_cached_value("Company", filters.get("company"), "default_currency")
		if filters.get("company")
		else None
	)
	results = []
	for row in rows:
		currency = row.currency or company_currency
		amount = f"Outstanding {fmt_money(flt(row.outstanding), currency=currency)}"
		if abs(flt(row.outstanding) - flt(row.total)) > 0.005:
			amount += f" of {fmt_money(flt(row.total), currency=currency)}"
		results.append((row.name, row.party, str(row.date), amount))
	return results
