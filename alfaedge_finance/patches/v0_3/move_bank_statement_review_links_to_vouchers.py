"""Bank Statement Review lines used to hold one matched voucher (existing_voucher*) and
one created draft (created_voucher*) each. Links now live in the `vouchers` table so a
line can be booked by several documents; move any old links there. The old columns are
left in place by Frappe's schema sync, so they can still be read here."""

import json

import frappe


def execute():
	columns = frappe.db.get_table_columns("Bank Statement Review Line")
	if "existing_voucher" not in columns:
		return

	old_links = frappe.db.sql(
		"""
		select parent, idx, existing_voucher_type, existing_voucher, created_voucher_type, created_voucher,
			line_status, alternatives
		from `tabBank Statement Review Line`
		where ifnull(existing_voucher, '') != '' or ifnull(created_voucher, '') != ''
			or ifnull(alternatives, '') != ''
		""",
		as_dict=True,
	)
	by_review = {}
	for row in old_links:
		by_review.setdefault(row.parent, []).append(row)

	for review_name, rows in by_review.items():
		review = frappe.get_doc("Bank Statement Review", review_name)
		linked = {(v.voucher_type, v.voucher_no) for v in review.vouchers}
		for row in rows:
			line = review.lines[row.idx - 1]
			# Old Check lines stored their candidates as a flat list; options are now lists.
			if row.alternatives:
				options = json.loads(row.alternatives)
				if options and isinstance(options[0], dict):
					line.alternatives = json.dumps([[option] for option in options])
			links = []
			if row.existing_voucher and row.line_status != "Check":
				links.append(("Matched", row.existing_voucher_type, row.existing_voucher))
			if row.created_voucher:
				links.append(("Created", row.created_voucher_type, row.created_voucher))
			for link_type, doctype, name in links:
				if (doctype, name) in linked or not frappe.db.exists(doctype, name):
					continue
				review._add_voucher(line, link_type, doctype, name)
				linked.add((doctype, name))
		review.refresh_voucher_links()
		review.flags.ignore_permissions = True
		review.save()
