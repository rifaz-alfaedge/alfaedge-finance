"""Resolve the rows of a bulk-payment file into a Journal Entry plan.

Each row's beneficiary account number is looked up in the non-company Bank Accounts
to find the Employee / Supplier, then the open documents that make up the row amount
exactly are found - the same documents the Bulk Payment CSV page sums when it builds
the file:

  Employee -> a Salary Slip with that net pay in an unpaid Payroll Entry, or
              unpaid Expense Claims (all of them, or a unique exact combination)
  Supplier -> open Purchase Invoices (all, the overdue ones, or a unique combination)

When nothing adds up exactly, the party's open documents posted on or before the
file's transaction date (the cut-off) are allocated oldest first, the last one partly
if needed; whatever the documents don't cover is booked on account (e.g. a claim that
was cancelled after the bulk file was made).

Row status: Matched (documents add up exactly), On-account (party known; documents
cover part or none of the row, the rest booked as an advance), Mismatch (documents
chosen add up to more than the row) or Unresolved (no party). Mismatch and Unresolved
block creating the Journal Entry.
"""

from collections import defaultdict
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import flt, getdate

from alfaedge_finance.alfaedge_finance.bank_statement.bulk_upload import normalize_account_no
from alfaedge_finance.alfaedge_finance.bank_statement.matching import exact_sum_combinations, to_paise
from alfaedge_finance.alfaedge_finance.bank_statement.orders import open_orders, order_details

MATCHED = "Matched"
ON_ACCOUNT = "On-account"
MISMATCH = "Mismatch"
UNRESOLVED = "Unresolved"
BLOCKING = (MISMATCH, UNRESOLVED)

KIND_DOCTYPE = {
	"expense_claim": "Expense Claim",
	"salary": "Salary Slip",
	"purchase_invoice": "Purchase Invoice",
	"purchase_order": "Purchase Order",
}
POOL_SIZE = 20
MAX_COMBINATION = 6


def party_by_account_no() -> dict:
	"""{normalised account no: {(party_type, party), ...}} for party Bank Accounts."""
	accounts = defaultdict(set)
	for row in frappe.get_all(
		"Bank Account",
		filters={"is_company_account": 0, "party_type": ["in", ["Employee", "Supplier"]]},
		fields=["bank_account_no", "party_type", "party"],
	):
		if row.party and normalize_account_no(row.bank_account_no):
			accounts[normalize_account_no(row.bank_account_no)].add((row.party_type, row.party))
	return accounts


# ------------------------------------------------------------ open documents


def open_expense_claims(company, employee) -> list[dict]:
	return [
		{"doctype": "Expense Claim", "name": r.name, "amount": flt(r.outstanding, 2), "date": r.posting_date}
		for r in frappe.db.sql(
			"""
			select name, posting_date, grand_total - ifnull(total_amount_reimbursed, 0) as outstanding
			from `tabExpense Claim`
			where docstatus = 1 and company = %(company)s and employee = %(employee)s
				and grand_total - ifnull(total_amount_reimbursed, 0) > 0.005
			order by posting_date asc, name asc
			""",
			{"company": company, "employee": employee},
			as_dict=True,
		)
	]


def open_purchase_invoices(company, supplier, currency) -> list[dict]:
	return [
		{
			"doctype": "Purchase Invoice",
			"name": r.name,
			"amount": flt(r.outstanding_amount, 2),
			"due_date": r.due_date,
			"date": r.posting_date,
		}
		for r in frappe.get_all(
			"Purchase Invoice",
			filters={
				"docstatus": 1,
				"company": company,
				"supplier": supplier,
				"currency": currency,
				"outstanding_amount": [">", 0],
			},
			fields=["name", "outstanding_amount", "due_date", "posting_date"],
			order_by="posting_date asc, name asc",
		)
	]


def payroll_paid_amount(payroll_entry) -> float:
	return flt(
		frappe.db.sql(
			"""
			select sum(jea.debit_in_account_currency - jea.credit_in_account_currency)
			from `tabJournal Entry Account` jea
			join `tabJournal Entry` je on je.name = jea.parent
			where je.docstatus = 1 and jea.reference_type = 'Payroll Entry' and jea.reference_name = %s
			""",
			payroll_entry,
		)[0][0]
	)


def payroll_net_pay(payroll_entry) -> float:
	return flt(
		frappe.db.sql(
			"select sum(net_pay) from `tabSalary Slip` where docstatus = 1 and payroll_entry = %s",
			payroll_entry,
		)[0][0]
	)


def unpaid_salary_slip(company, employee, amount, bank_date) -> dict | None:
	"""The latest submitted Salary Slip of `employee` with exactly this net pay, in a
	submitted Payroll Entry that hasn't been fully paid yet."""
	for slip in frappe.get_all(
		"Salary Slip",
		filters={
			"docstatus": 1,
			"company": company,
			"employee": employee,
			"net_pay": amount,
			"payroll_entry": ["is", "set"],
			"start_date": ["<=", bank_date],
		},
		fields=["name", "payroll_entry", "net_pay"],
		order_by="end_date desc",
	):
		if frappe.db.get_value("Payroll Entry", slip.payroll_entry, "docstatus") != 1:
			continue
		if payroll_paid_amount(slip.payroll_entry) + 0.005 < payroll_net_pay(slip.payroll_entry):
			return {"doctype": "Salary Slip", "name": slip.name, "amount": flt(slip.net_pay, 2)}
	return None


def _exact_documents(documents, amount, used) -> list[dict] | None:
	"""All `documents`, or a unique combination of them, adding up exactly to `amount`."""
	documents = [d for d in documents if (d["doctype"], d["name"]) not in used]
	if not documents:
		return None
	if to_paise(sum(d["amount"] for d in documents)) == to_paise(amount):
		return documents
	single = [d for d in documents if to_paise(d["amount"]) == to_paise(amount)]
	if len(single) == 1:
		return single
	if single:
		return None  # several documents with this exact amount - ambiguous
	combos = exact_sum_combinations(documents[:POOL_SIZE], amount, lambda d: d["amount"], MAX_COMBINATION)
	return combos[0] if len(combos) == 1 else None


def _allocate_oldest_first(documents, amount, used, cutoff) -> list[dict]:
	"""Allocate `amount` to documents posted on or before `cutoff`, oldest first; the last
	one may be allocated partly. Returns the allocations (possibly covering less than
	`amount` - the caller books the rest on account)."""
	allocations, left = [], flt(amount, 2)
	for document in sorted(
		(
			d
			for d in documents
			if (d["doctype"], d["name"]) not in used and d.get("date") and getdate(d["date"]) <= cutoff
		),
		key=lambda d: (getdate(d["date"]), d["name"]),
	):
		if left <= 0.005:
			break
		allocated = flt(min(document["amount"], left), 2)
		allocations.append({**document, "amount": allocated})
		left = flt(left - allocated, 2)
	return allocations


def _up_to(documents, cutoff) -> list[dict]:
	"""Documents posted on or before the cut-off (the file's transaction date)."""
	return [d for d in documents if not d.get("date") or getdate(d["date"]) <= cutoff]


def _parse_file_date(text):
	for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
		try:
			return datetime.strptime((text or "").strip()[:10], fmt).date()
		except ValueError:
			continue
	return None


# ---------------------------------------------------------------- resolving


def resolve_rows(company, currency, bank_date, rows) -> list[dict]:
	"""Turn parsed bulk rows into plan rows with a party, kind, documents and status."""
	accounts = party_by_account_no()
	bank_date = getdate(bank_date)
	used = set()
	plan = []
	for row in rows:
		entry = {
			"row_no": row.row_no,
			"beneficiary_name": row.beneficiary_name,
			"account_no": row.account_no,
			"amount": row.amount,
			"party_type": None,
			"party": None,
			"kind": "on_account",
			"documents": [],
			"status": UNRESOLVED,
			"note": "",
		}
		parties = accounts.get(row.account_no) or set()
		if len(parties) != 1:
			entry["note"] = (
				_("No Employee/Supplier Bank Account has account number {0}").format(row.account_no)
				if not parties
				else _("Account number {0} belongs to several parties").format(row.account_no)
			)
			plan.append(entry)
			continue

		party_type, party = next(iter(parties))
		entry.update(party_type=party_type, party=party)
		# Documents after the date the file was prepared can't be what it paid.
		cutoff = _parse_file_date(row.transaction_date) or bank_date
		documents, kind, partial = None, None, []
		if party_type == "Employee":
			slip = unpaid_salary_slip(company, party, row.amount, bank_date)
			if slip and ("Salary Slip", slip["name"]) not in used:
				documents, kind = [slip], "salary"
			else:
				claims = _up_to(open_expense_claims(company, party), cutoff)
				documents = _exact_documents(claims, row.amount, used)
				kind = "expense_claim"
				if not documents:
					partial = _allocate_oldest_first(claims, row.amount, used, cutoff)
		else:
			invoices = _up_to(open_purchase_invoices(company, party, currency), cutoff)
			overdue = [d for d in invoices if d["due_date"] and getdate(d["due_date"]) < bank_date]
			documents = _exact_documents(invoices, row.amount, used) or _exact_documents(
				overdue, row.amount, used
			)
			kind = "purchase_invoice"
			if not documents:
				# An advance against an open Purchase Order.
				orders = open_orders("Purchase Order", company, currency, party)
				documents = _exact_documents(orders, row.amount, used)
				kind = "purchase_order" if documents else "purchase_invoice"
			if not documents:
				partial = _allocate_oldest_first(invoices, row.amount, used, cutoff)

		if documents:
			entry.update(
				kind=kind,
				documents=[
					{"doctype": d["doctype"], "name": d["name"], "amount": d["amount"]} for d in documents
				],
				status=MATCHED,
			)
			used.update((d["doctype"], d["name"]) for d in documents)
		elif partial:
			covered = flt(sum(d["amount"] for d in partial), 2)
			entry.update(
				kind=kind,
				documents=[
					{"doctype": d["doctype"], "name": d["name"], "amount": d["amount"]} for d in partial
				],
				status=MATCHED if abs(covered - row.amount) < 0.005 else ON_ACCOUNT,
				note=_("Allocated oldest first to {0} document(s) up to {1}{2}").format(
					len(partial),
					cutoff.strftime("%d-%m-%Y"),
					""
					if abs(covered - row.amount) < 0.005
					else _("; {0} not covered by open documents is booked on account").format(
						flt(row.amount - covered, 2)
					),
				),
			)
			used.update((d["doctype"], d["name"]) for d in partial)
		else:
			entry.update(
				kind="on_account",
				status=ON_ACCOUNT,
				note=_("No open documents of {0} up to {1}; booked on account").format(
					party, cutoff.strftime("%d-%m-%Y")
				),
			)
		plan.append(entry)
	return plan


def validate_plan(company, plan) -> list[dict]:
	"""Re-check a (possibly reviewer-edited) plan: parties and documents must exist, be
	submitted, belong to the company and the row's party, and add up to the row amount.
	Returns the plan with statuses and notes recomputed."""
	used = set()
	for row in plan:
		row["amount"] = flt(row.get("amount"), 2)
		row["note"] = ""
		party_type, party = row.get("party_type"), row.get("party")
		if not (party_type in ("Employee", "Supplier") and party and frappe.db.exists(party_type, party)):
			row.update(status=UNRESOLVED, documents=[], kind="on_account")
			row["note"] = _("Pick the Employee or Supplier this row paid")
			continue

		documents, problems = [], []
		invalid = False
		# A partial allocation from the resolver is kept as the cap for that document.
		document_amounts = {d.get("name"): d.get("amount") for d in row.get("documents") or []}
		for document in row.get("documents") or []:
			checked, problem = _check_document(company, row, document, used)
			if problem:
				problems.append(problem)
				invalid = True
			else:
				documents.append(checked)
				used.add((checked["doctype"], checked["name"]))
		# Allocate the row across the listed documents in order (the last one partly if
		# needed); documents beyond the row amount aren't needed.
		left, allocated = row["amount"], []
		for checked in documents:
			if left <= 0.005:
				problems.append(_("{0} not needed - the row is already covered").format(checked["name"]))
				continue
			cap = flt(document_amounts.get(checked["name"])) or checked["amount"]
			checked["amount"] = flt(min(checked["amount"], cap, left), 2)
			allocated.append(checked)
			left = flt(left - checked["amount"], 2)
		row["documents"] = allocated
		if not allocated:
			row["kind"] = "on_account"
		if invalid:
			row["status"] = MISMATCH  # a listed document is wrong - the reviewer must fix it
		elif left <= 0.005:
			row["status"] = MATCHED
		else:
			row["status"] = ON_ACCOUNT
			problems.append(_("{0} not covered by documents is booked on account").format(left))
		row["note"] = "; ".join(problems)
	return plan


def _check_document(company, row, document, used):
	kind = row.get("kind")
	doctype = document.get("doctype") or KIND_DOCTYPE.get(kind)
	name = (document.get("name") or "").strip()
	if doctype not in KIND_DOCTYPE.values() or not name:
		return None, _("unknown document {0}").format(name or "")
	if (doctype, name) in used:
		return None, _("{0} is used on another row").format(name)
	party_field = {
		"Expense Claim": "employee",
		"Salary Slip": "employee",
		"Purchase Invoice": "supplier",
		"Purchase Order": "supplier",
	}[doctype]
	doc = frappe.db.get_value(doctype, name, ["docstatus", "company", party_field], as_dict=True)
	if not doc or doc.docstatus != 1 or doc.company != company:
		return None, _("{0} {1} is not a submitted document of {2}").format(doctype, name, company)
	if doc.get(party_field) != row["party"]:
		return None, _("{0} belongs to {1}, not {2}").format(name, doc.get(party_field), row["party"])

	if doctype == "Expense Claim":
		outstanding = next(
			(d["amount"] for d in open_expense_claims(company, row["party"]) if d["name"] == name), 0
		)
	elif doctype == "Purchase Invoice":
		outstanding = flt(frappe.db.get_value(doctype, name, "outstanding_amount"), 2)
	elif doctype == "Purchase Order":
		outstanding = order_details(doctype, name).outstanding
	else:
		outstanding = flt(frappe.db.get_value(doctype, name, "net_pay"), 2)
	if outstanding <= 0:
		return None, _("{0} has nothing outstanding").format(name)
	row["kind"] = {v: k for k, v in KIND_DOCTYPE.items()}[doctype]
	return {"doctype": doctype, "name": name, "amount": outstanding}, None


def summarise(plan) -> tuple[str, str]:
	"""(suggestion text, confidence) for a plan."""
	counts = defaultdict(int)
	for row in plan:
		counts[row["status"]] += 1
	parts = [f"{counts[s]} {s.lower()}" for s in (MATCHED, ON_ACCOUNT, MISMATCH, UNRESOLVED) if counts[s]]
	text = _("Bulk file: {0} beneficiaries - {1}").format(len(plan), ", ".join(parts))
	if counts[UNRESOLVED] or counts[MISMATCH]:
		return text, "Low"
	return text, "Medium" if counts[ON_ACCOUNT] else "High"
