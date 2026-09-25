"""Build a pre-filled Payment Entry / Journal Entry for a statement line.

Returns an unsaved document; the caller either inserts it as a Draft or hands it to
the browser to open unsaved. Nothing here submits - that stays a manual step.
"""

from collections import defaultdict

import frappe
from erpnext.accounts.party import get_party_account
from erpnext.setup.utils import get_exchange_rate
from frappe import _
from frappe.utils import flt

from alfaedge_finance.alfaedge_finance.bank_statement.orders import ORDER_PARTY_FIELD, order_details


def build_voucher(review, line, values: dict, amount=None):
	"""`amount` defaults to the whole line; pass the unbooked remainder for a line that
	other vouchers already partly cover."""
	doctype = values.get("suggested_doctype") or "Journal Entry"
	if doctype == "Payment Entry":
		return build_payment_entry(review, line, values, amount=amount)
	return build_journal_entry(review, line, values, amount=amount)


def _amount(line):
	return flt(line.withdrawal or line.deposit, 2)


def _reference(line):
	# Bank Entry vouchers require a reference number; fall back to the narration.
	return (line.reference or line.particulars or "")[:140]


def build_payment_entry(review, line, values, amount=None):
	party_type, party = values.get("party_type"), values.get("party")
	if not (party_type and party):
		frappe.throw(_("Row {0}: a Payment Entry needs a Party Type and Party.").format(line.idx))

	against_doctype, against_name = values.get("against_doctype"), values.get("against_name")
	amount = flt(amount, 2) if amount else _amount(line)

	if against_doctype and against_name:
		if against_doctype == "Expense Claim":
			from hrms.overrides.employee_payment_entry import get_payment_entry_for_employee

			pe = get_payment_entry_for_employee(
				against_doctype, against_name, bank_account=review.gl_account, bank_amount=amount
			)
		else:
			from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

			pe = get_payment_entry(
				against_doctype,
				against_name,
				bank_account=review.gl_account,
				bank_amount=amount,
				reference_date=line.transaction_date,
			)
		# get_payment_entry pays the document's whole outstanding; pay what the bank shows
		# instead (a part payment, or the remainder of a partly booked line).
		tds_amount = flt(values.get("tds_amount"), 2)
		if pe.paid_from_account_currency == pe.paid_to_account_currency:
			for reference in pe.get("references") or []:
				# With TDS withheld, the whole outstanding is settled: bank amount + TDS.
				reference.allocated_amount = min(flt(reference.outstanding_amount), amount + tds_amount)
			pe.paid_amount = pe.received_amount = amount
		if tds_amount:
			if not values.get("tds_account"):
				frappe.throw(
					_("Row {0}: choose the TDS account for the {1} deducted.").format(line.idx, tds_amount)
				)
			pe.set("deductions", [])
			pe.append(
				"deductions",
				{
					"account": values["tds_account"],
					"cost_center": frappe.get_cached_value("Company", review.company, "cost_center"),
					"amount": tds_amount,
					"description": _("TDS deducted by customer"),
				},
			)
	else:
		pe = _build_unallocated_payment_entry(
			review, line, party_type, party, amount, advance=bool(values.get("is_advance"))
		)

	pe.posting_date = line.transaction_date
	pe.reference_no = _reference(line)
	pe.reference_date = line.transaction_date
	pe.bank_account = review.bank_account
	if values.get("mode_of_payment"):
		pe.mode_of_payment = values["mode_of_payment"]
	remarks = values.get("remarks")
	if values.get("is_advance") and not remarks:
		reference = values.get("advance_reference")
		remarks = _("Advance to {0}").format(party) + (f" - {reference}" if reference else "")
		remarks += f"\n{line.particulars}"
	if remarks:
		# Without custom_remarks, ERPNext overwrites remarks with its own summary on save.
		pe.custom_remarks = 1
		pe.remarks = remarks
	return pe


def _build_unallocated_payment_entry(review, line, party_type, party, amount, advance=False):
	"""An on-account payment (advance/refund) with no invoice allocated - mirrors
	erpnext's own create_payment_entry_bts. ERPNext treats its unallocated amount as an
	advance, offered on the party's next invoice."""
	payment_type = "Receive" if flt(line.deposit) > 0 else "Pay"
	party_account = get_party_account(party_type, party, review.company)
	if advance and party_type == "Employee":
		party_account = (
			frappe.get_cached_value("Company", review.company, "default_employee_advance_account")
			or party_account
		)
	bank_currency = frappe.get_cached_value("Account", review.gl_account, "account_currency")
	party_currency = frappe.get_cached_value("Account", party_account, "account_currency")
	exchange_rate = get_exchange_rate(bank_currency, party_currency, line.transaction_date) or 1
	amount_in_party_currency = flt(amount * exchange_rate, 2)

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = payment_type
	pe.company = review.company
	pe.party_type = party_type
	pe.party = party
	pe.paid_from = party_account if payment_type == "Receive" else review.gl_account
	pe.paid_to = review.gl_account if payment_type == "Receive" else party_account
	pe.paid_from_account_currency = party_currency if payment_type == "Receive" else bank_currency
	pe.paid_to_account_currency = bank_currency if payment_type == "Receive" else party_currency
	pe.paid_amount = amount_in_party_currency if payment_type == "Receive" else amount
	pe.received_amount = amount if payment_type == "Receive" else amount_in_party_currency
	return pe


def build_journal_entry(review, line, values, amount=None):
	account = values.get("account")
	if not account:
		frappe.throw(_("Row {0}: a Journal Entry needs the contra Account.").format(line.idx))

	amount = flt(amount, 2) if amount else _amount(line)
	is_deposit = flt(line.deposit) > 0
	party_type, party = values.get("party_type"), values.get("party")
	account_type = frappe.get_cached_value("Account", account, "account_type")
	if account_type in ("Receivable", "Payable") and not (party_type and party):
		frappe.throw(
			_("Row {0}: account {1} is {2}, so a Party Type and Party are required.").format(
				line.idx, account, account_type
			)
		)

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Bank Entry"
	je.company = review.company
	je.posting_date = line.transaction_date
	je.cheque_no = _reference(line)
	je.cheque_date = line.transaction_date
	je.user_remark = values.get("remarks") or line.particulars
	# debit/credit are set alongside the *_in_account_currency fields so an unsaved JE
	# opened in the browser shows its totals before its first save recomputes them.
	bank_debit, bank_credit = (amount, 0) if is_deposit else (0, amount)
	je.append(
		"accounts",
		{
			"account": review.gl_account,
			"bank_account": review.bank_account,
			"debit_in_account_currency": bank_debit,
			"credit_in_account_currency": bank_credit,
			"debit": bank_debit,
			"credit": bank_credit,
		},
	)
	# Only a receivable / payable account carries a party on a Journal Entry row.
	takes_party = account_type in ("Receivable", "Payable")
	je.append(
		"accounts",
		{
			"account": account,
			"party_type": party_type if party and takes_party else None,
			"party": party if takes_party else None,
			"debit_in_account_currency": bank_credit,
			"credit_in_account_currency": bank_debit,
			"debit": bank_credit,
			"credit": bank_debit,
		},
	)
	je.total_debit = je.total_credit = amount
	return je


def build_split_payment_entries(
	review, line, plan: list[dict], values: dict | None = None, target: float | None = None
) -> list:
	"""One Payment Entry per party for a payment split across several invoices/parties.

	`plan` rows: {party_type, party, against_doctype, against_name, amount}. Rows of the
	same party share a Payment Entry with one reference row per invoice; a row without an
	invoice is an on-account amount on that party's entry. Amounts must add up exactly to
	`target` (default: the whole statement line)."""
	values = values or {}
	line_amount = flt(target, 2) if target else _amount(line)
	if not plan:
		frappe.throw(_("Row {0}: the split has no rows.").format(line.idx))
	for row in plan:
		if not (row.get("party_type") and row.get("party")) or flt(row.get("amount")) <= 0:
			frappe.throw(_("Row {0}: every split row needs a Party and a positive Amount.").format(line.idx))
	total = flt(sum(flt(row["amount"], 2) for row in plan), 2)
	if abs(total - line_amount) > 0.005:
		frappe.throw(
			_("Row {0}: the split adds up to {1}, but {2} is left to book on this bank line.").format(
				line.idx, total, line_amount
			)
		)

	groups = {}
	for row in plan:
		groups.setdefault((row["party_type"], row["party"]), []).append(row)

	entries = []
	for (party_type, party), rows in groups.items():
		group_amount = flt(sum(flt(r["amount"], 2) for r in rows), 2)
		with_documents = [r for r in rows if r.get("against_doctype") and r.get("against_name")]
		first = with_documents[0] if with_documents else {}
		pe = build_payment_entry(
			review,
			line,
			{
				"party_type": party_type,
				"party": party,
				"against_doctype": first.get("against_doctype"),
				"against_name": first.get("against_name"),
				"mode_of_payment": values.get("mode_of_payment"),
				"remarks": values.get("remarks"),
			},
			amount=group_amount,
		)
		if pe.paid_from_account_currency != pe.paid_to_account_currency:
			# A foreign-currency party: ERPNext converts the bank amount itself for one
			# document; several documents in one split entry can't be allocated safely.
			if len(with_documents) > 1:
				frappe.throw(
					_(
						"Row {0}: {1} is billed in another currency - split its invoices into separate lines or book them one at a time."
					).format(line.idx, party)
				)
			entries.append(pe)
			continue
		if with_documents:
			# get_payment_entry allocated the whole outstanding of the first document;
			# rebuild references with the split's own allocations.
			template = pe.references[0].as_dict() if pe.get("references") else {}
			pe.set("references", [])
			for row in with_documents:
				pe.append("references", _reference_row(row, template))
		pe.paid_amount = pe.received_amount = group_amount
		pe.base_paid_amount = pe.base_received_amount = group_amount
		entries.append(pe)
	return entries


def _reference_row(row, template):
	doctype, name = row["against_doctype"], row["against_name"]
	if doctype in ORDER_PARTY_FIELD:
		details = order_details(doctype, name)
		total, outstanding, due_date = details.total, details.outstanding, details.date
	elif doctype == "Expense Claim":
		details = frappe.db.get_value(
			doctype, name, ["grand_total", "total_amount_reimbursed", "posting_date"], as_dict=True
		)
		total, outstanding = (
			flt(details.grand_total),
			flt(details.grand_total) - flt(details.total_amount_reimbursed),
		)
		due_date = details.posting_date
	else:
		details = frappe.db.get_value(
			doctype, name, ["grand_total", "outstanding_amount", "due_date"], as_dict=True
		)
		total, outstanding, due_date = (
			flt(details.grand_total),
			flt(details.outstanding_amount),
			details.due_date,
		)
	if flt(row["amount"], 2) - outstanding > 0.005:
		frappe.throw(
			_("{0} {1} has only {2} outstanding, but the split allocates {3}.").format(
				doctype, name, outstanding, row["amount"]
			)
		)
	return {
		"reference_doctype": doctype,
		"reference_name": name,
		"total_amount": total,
		"outstanding_amount": outstanding,
		"allocated_amount": flt(row["amount"], 2),
		"due_date": due_date,
		"exchange_rate": template.get("exchange_rate") or 1,
	}


def bank_amount_of(doctype: str, name: str, gl_account: str) -> float:
	"""The voucher's amount on the bank GL account, in the bank account's currency."""
	if doctype == "Payment Entry":
		pe = frappe.db.get_value(
			"Payment Entry", name, ["paid_from", "paid_amount", "received_amount"], as_dict=True
		)
		return flt(pe.paid_amount if pe.paid_from == gl_account else pe.received_amount, 2)
	rows = frappe.get_all(
		"Journal Entry Account",
		filters={"parent": name, "account": gl_account},
		fields=["debit_in_account_currency", "credit_in_account_currency"],
	)
	return flt(
		abs(sum(flt(r.debit_in_account_currency) - flt(r.credit_in_account_currency) for r in rows)), 2
	)


def build_bulk_journal_entry(review, line, plan: list[dict], target: float | None = None, values=None):
	"""One Journal Entry for a bulk-payment line: the bank credit, and a debit row per
	settled document (Expense Claim / Purchase Invoice / Payroll Entry), or per party
	for on-account rows. Mirrors how these batches were booked by hand."""
	from alfaedge_finance.alfaedge_finance.bank_statement.bulk_matching import BLOCKING

	values = values or {}
	target = flt(target, 2) if target else _amount(line)
	blocked = [str(r["row_no"]) for r in plan if r["status"] in BLOCKING]
	if blocked:
		frappe.throw(
			_("Row {0}: fix bulk file row(s) {1} before creating the Journal Entry.").format(
				line.idx, ", ".join(blocked)
			)
		)
	total = flt(sum(flt(r["amount"], 2) for r in plan), 2)
	if abs(total - target) > 0.005:
		frappe.throw(
			_("Row {0}: the bulk file adds up to {1}, but {2} is left to book on this bank line.").format(
				line.idx, total, target
			)
		)

	per_employee_payroll = frappe.db.get_single_value(
		"Payroll Settings", "process_payroll_accounting_entry_based_on_employee"
	)
	debit_rows = []
	payroll_totals = defaultdict(float)
	for row in plan:
		for document in row["documents"]:
			doctype, name, amount = document["doctype"], document["name"], flt(document["amount"], 2)
			if doctype == "Expense Claim":
				debit_rows.append(
					{
						"account": frappe.db.get_value(doctype, name, "payable_account"),
						"party_type": "Employee",
						"party": row["party"],
						"reference_type": doctype,
						"reference_name": name,
						"amount": amount,
					}
				)
			elif doctype == "Purchase Order":
				debit_rows.append(
					{
						"account": get_party_account("Supplier", row["party"], review.company),
						"party_type": "Supplier",
						"party": row["party"],
						"reference_type": doctype,
						"reference_name": name,
						"is_advance": "Yes",
						"amount": amount,
					}
				)
			elif doctype == "Purchase Invoice":
				debit_rows.append(
					{
						"account": frappe.db.get_value(doctype, name, "credit_to"),
						"party_type": "Supplier",
						"party": row["party"],
						"reference_type": doctype,
						"reference_name": name,
						"amount": amount,
					}
				)
			else:  # Salary Slip -> Payroll Payable against its Payroll Entry
				payroll_entry = frappe.db.get_value(doctype, name, "payroll_entry")
				if per_employee_payroll:
					debit_rows.append(
						{
							"account": frappe.db.get_value(
								"Payroll Entry", payroll_entry, "payroll_payable_account"
							),
							"party_type": "Employee",
							"party": row["party"],
							"reference_type": "Payroll Entry",
							"reference_name": payroll_entry,
							"amount": amount,
						}
					)
				else:
					payroll_totals[payroll_entry] += amount
		# Whatever the documents don't cover is an advance to the party.
		on_account = flt(flt(row["amount"], 2) - sum(flt(d["amount"], 2) for d in row["documents"]), 2)
		if on_account > 0.005:
			debit_rows.append(
				{
					"account": get_party_account(row["party_type"], row["party"], review.company),
					"party_type": row["party_type"],
					"party": row["party"],
					"amount": on_account,
					"is_advance": "Yes",
				}
			)
	for payroll_entry, amount in payroll_totals.items():
		debit_rows.append(
			{
				"account": frappe.db.get_value("Payroll Entry", payroll_entry, "payroll_payable_account"),
				"reference_type": "Payroll Entry",
				"reference_name": payroll_entry,
				"amount": flt(amount, 2),
			}
		)

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Bank Entry"
	je.company = review.company
	je.posting_date = line.transaction_date
	je.cheque_no = _reference(line)
	je.cheque_date = line.transaction_date
	je.user_remark = (
		values.get("remarks")
		or _("Bulk payment {0}: {1}").format(
			line.reference or "", ", ".join(r["beneficiary_name"] or r["party"] for r in plan)
		)[:1000]
	)
	je.append(
		"accounts",
		{
			"account": review.gl_account,
			"bank_account": review.bank_account,
			"credit_in_account_currency": target,
			"credit": target,
		},
	)
	for row in debit_rows:
		amount = row.pop("amount")
		je.append("accounts", {**row, "debit_in_account_currency": amount, "debit": amount})
	je.total_debit = je.total_credit = target
	return je
