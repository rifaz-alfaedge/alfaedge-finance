"""Keep Bank Statement Reviews in step with the Payment Entries / Journal Entries they
link to, when those are deleted, submitted or cancelled from their own forms.

Deleting a linked voucher is allowed (hooks.ignore_links_on_delete lists the review's
voucher table); on_trash drops its row and puts the line back to Suggested.
"""

import frappe

from alfaedge_finance.alfaedge_finance.doctype.bank_statement_review.bank_statement_review import (
	SKIP_SYNC_FLAG,
)

VOUCHER_TABLE = "Bank Statement Review Voucher"


def _reviews_linking(doc) -> list[str]:
	return list(
		set(
			frappe.get_all(
				VOUCHER_TABLE,
				filters={
					"voucher_type": doc.doctype,
					"voucher_no": doc.name,
					"parenttype": "Bank Statement Review",
				},
				pluck="parent",
			)
		)
	)


def _sync(doc, remove=False):
	if frappe.flags.get(SKIP_SYNC_FLAG):
		return
	for name in _reviews_linking(doc):
		review = frappe.get_doc("Bank Statement Review", name)
		if remove:
			for row in [
				r for r in review.vouchers if r.voucher_type == doc.doctype and r.voucher_no == doc.name
			]:
				review.remove(row)
		review.refresh_voucher_links()
		review.flags.ignore_permissions = True
		review.save()


def on_voucher_trash(doc, method=None):
	_sync(doc, remove=True)


def on_voucher_status_change(doc, method=None):
	_sync(doc)
