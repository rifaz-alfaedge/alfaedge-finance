"""Step B: for a statement line with no booked voucher, suggest the record to create.

Sources, first hit wins (each records why, so the reviewer can judge it):
  1. Bank Narration Rule for (direction, counterparty key)             - High
  2. Past reconciled Bank Transactions with the same counterparty key,
     only when every past one was booked the same way                   - High
  3. Unique party whose name / party Bank Account name starts with the
     bank's (often truncated) counterparty name, min. 8 characters     - Medium
  4. An open Purchase Invoice / Sales Invoice / Expense Claim (or, for an advance, an
     open Purchase Order / Sales Order) whose
     outstanding amount equals the line amount exactly - for the party
     found above, or (Low) the only such document across all parties
  5. A unique combination of open invoices (2-5, posted within 60 days before the
     bank date) whose outstanding amounts sum exactly to the line - a "split plan",
     e.g. one Amazon card payment covering invoices from several sellers.
     For the party found above first, else across all parties       - Medium / Low
  6. Fallback: Journal Entry with only the bank side filled in           - Low

Foreign-currency parties (a USD invoice paid by card in INR) can never match on amount:
the bank's INR differs from invoice-amount x rate by the card's markup / fees. For
these, an open foreign-currency invoice is picked by the party's name appearing in the
narration (e.g. "ECOM PUR/OPENAI/..." -> OpenAI OpCo, LLC), with the amount only as a
sanity check against the market rate; ERPNext books the difference to Exchange Gain/Loss.
A narration quoting the foreign amount ("... USD 15160.80 ...") is matched on it exactly.

Customer receipts often arrive short of the invoice by the TDS the customer withheld:
2%, 5% or 10% of the invoice's net total (before GST), rounded to the rupee. Where
no exact document fits, an open Sales Invoice whose outstanding minus that TDS
equals the deposit is suggested, with the TDS as a Payment Entry deduction.

Like supplier matching, nothing is fuzzy: names match on an exact prefix of their
alphanumeric form and only a unique hit is used.
"""

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import timedelta

import frappe
from erpnext.setup.utils import get_exchange_rate
from frappe.utils import flt, getdate

from alfaedge_finance.alfaedge_finance.bank_statement.matching import exact_sum_combinations
from alfaedge_finance.alfaedge_finance.bank_statement.narration import (
	BULK_UPLOAD,
	compact,
	counterparty_key,
	parse_narration,
)
from alfaedge_finance.alfaedge_finance.bank_statement.orders import ORDER_PARTY_TYPE, open_orders

MIN_NAME_PREFIX = 8

# Which party types make sense for money going out vs. coming in.
PARTY_TYPES_FOR = {
	"Withdrawal": ("Supplier", "Employee"),
	"Deposit": ("Customer",),
}


@dataclass
class Suggestion:
	suggested_doctype: str = "Journal Entry"
	party_type: str | None = None
	party: str | None = None
	account: str | None = None
	against_doctype: str | None = None
	against_name: str | None = None
	mode_of_payment: str | None = None
	confidence: str = "Low"
	suggestion_source: str = "No match - fill in manually"
	# [{party_type, party, against_doctype, against_name, amount}] when one payment
	# should be split across several invoices / parties.
	split_plan: list = field(default_factory=list)
	# TDS withheld by a customer, booked as a Payment Entry deduction.
	tds_amount: float = 0
	tds_account: str | None = None
	# A payment with no invoice yet (proforma, prepaid usage) - never settles a document.
	is_advance: bool = False

	def as_dict(self):
		data = asdict(self)
		data["split_plan"] = json.dumps(self.split_plan) if self.split_plan else None
		return data


class SuggestionContext:
	"""Loads rules, history and party names once per review run."""

	def __init__(self, company: str, gl_account: str, currency: str):
		self.company = company
		self.gl_account = gl_account
		self.currency = currency
		self._rules = None
		self._history = None
		self._names = None
		self._labels = None
		# Documents already proposed for / settled by another line of this review; a
		# foreign-currency invoice is only offered to one line.
		self.used_documents = set()
		self.default_mode_of_payment = (
			"Bank Transfer" if frappe.db.exists("Mode of Payment", "Bank Transfer") else None
		)

	@property
	def rules(self):
		if self._rules is None:
			self._rules = {
				(r.direction, r.counterparty_key): r
				for r in frappe.get_all(
					"Bank Narration Rule",
					fields=[
						"name",
						"direction",
						"counterparty_key",
						"suggested_doctype",
						"party_type",
						"party",
						"account",
						"mode_of_payment",
						"treat_as_advance",
					],
				)
			}
		return self._rules

	@property
	def history(self):
		if self._history is None:
			self._history = load_history_outcomes()
		return self._history

	@property
	def party_labels(self):
		"""[(party_type, party, display name)] of every active party."""
		if self._labels is None:
			self._labels = []
			for doctype, name_field, filters in (
				("Supplier", "supplier_name", {"disabled": 0}),
				("Customer", "customer_name", {"disabled": 0}),
				("Employee", "employee_name", {"status": "Active"}),
			):
				for row in frappe.get_all(doctype, filters=filters, fields=["name", name_field]):
					self._labels.append((doctype, row.name, row.get(name_field) or row.name))
		return self._labels

	@property
	def names(self):
		if self._names is None:
			self._names = load_party_names()
		return self._names


def load_history_outcomes() -> dict:
	"""{(direction, counterparty_key): [outcome, ...]} from reconciled Bank Transactions,
	where an outcome is (doctype, party_type, party, account)."""
	rows = frappe.db.sql(
		"""
		select bt.name, bt.description, bt.deposit, bt.withdrawal,
			btp.payment_document, btp.payment_entry
		from `tabBank Transaction` bt
		join `tabBank Transaction Payments` btp on btp.parent = bt.name
		where bt.docstatus = 1
		""",
		as_dict=True,
	)
	bank_accounts = set(
		frappe.get_all("Account", filters={"account_type": ["in", ["Bank", "Cash"]]}, pluck="name")
	)

	outcomes = defaultdict(list)
	for row in rows:
		key = counterparty_key(parse_narration(row.description).counterparty)
		# Bulk uploads pay different parties every time - nothing to learn from them.
		if not key or key == counterparty_key(BULK_UPLOAD):
			continue
		direction = "Deposit" if flt(row.deposit) > 0 else "Withdrawal"
		outcome = None
		if row.payment_document == "Payment Entry":
			party_type, party = frappe.db.get_value(
				"Payment Entry", row.payment_entry, ["party_type", "party"]
			) or (None, None)
			if party:
				outcome = ("Payment Entry", party_type, party, None)
		elif row.payment_document == "Journal Entry":
			others = {
				(r.account, r.party_type or None, r.party or None)
				for r in frappe.get_all(
					"Journal Entry Account",
					filters={"parent": row.payment_entry},
					fields=["account", "party_type", "party"],
				)
				if r.account not in bank_accounts
			}
			if len(others) == 1:
				account, party_type, party = others.pop()
				outcome = ("Journal Entry", party_type, party, account)
		elif row.payment_document == "Expense Claim":
			employee = frappe.db.get_value("Expense Claim", row.payment_entry, "employee")
			if employee:
				outcome = ("Payment Entry", "Employee", employee, None)
		# An unusable link still counts, so an inconsistent history isn't mistaken for agreement.
		outcomes[(direction, key)].append(outcome)
	return outcomes


def load_party_names() -> list[tuple[str, str, str]]:
	"""[(compact name, party_type, party)] for every active party and party Bank Account."""
	names = []
	for doctype, name_field, filters in (
		("Supplier", "supplier_name", {"disabled": 0}),
		("Customer", "customer_name", {"disabled": 0}),
		("Employee", "employee_name", {"status": "Active"}),
	):
		for row in frappe.get_all(doctype, filters=filters, fields=["name", name_field]):
			for label in {row.get(name_field), row.name}:
				if compact(label):
					names.append((compact(label), doctype, row.name))

	for row in frappe.get_all(
		"Bank Account",
		filters={"is_company_account": 0, "party_type": ["in", ["Supplier", "Customer", "Employee"]]},
		fields=["account_name", "party_type", "party"],
	):
		if row.party and compact(row.account_name):
			names.append((compact(row.account_name), row.party_type, row.party))
	return names


def find_party_by_name(context: SuggestionContext, counterparty: str | None, direction: str):
	name = compact(counterparty)
	if len(name) < MIN_NAME_PREFIX:
		return None
	hits = set()
	for candidate, party_type, party in context.names:
		shorter, longer = sorted((name, candidate), key=len)
		if len(shorter) >= MIN_NAME_PREFIX and longer.startswith(shorter):
			hits.add((party_type, party))
	if len(hits) > 1:
		hits = {h for h in hits if h[0] in PARTY_TYPES_FOR[direction]}
	return hits.pop() if len(hits) == 1 else None


def find_open_documents(
	context: SuggestionContext,
	direction: str,
	amount: float,
	party_type=None,
	party=None,
	transaction_date=None,
):
	"""Open documents whose outstanding equals `amount` exactly: [(doctype, name, party_type,
	party)]. With `transaction_date`, the one posted closest to the bank date (on or before
	it first) comes first - e.g. this month's invoice rather than an older one of the same
	amount; otherwise oldest first. Invoices come before orders on a tie."""
	found = []
	if direction == "Withdrawal" and party_type in (None, "Supplier"):
		filters = {
			"docstatus": 1,
			"company": context.company,
			"currency": context.currency,
			"outstanding_amount": amount,
		}
		if party:
			filters["supplier"] = party
		for row in frappe.get_all(
			"Purchase Invoice",
			filters=filters,
			fields=["name", "supplier", "posting_date"],
			order_by="posting_date asc",
		):
			found.append(("Purchase Invoice", row.name, "Supplier", row.supplier, row.posting_date))

	if direction == "Deposit" and party_type in (None, "Customer"):
		filters = {
			"docstatus": 1,
			"company": context.company,
			"currency": context.currency,
			"outstanding_amount": amount,
		}
		if party:
			filters["customer"] = party
		for row in frappe.get_all(
			"Sales Invoice",
			filters=filters,
			fields=["name", "customer", "posting_date"],
			order_by="posting_date asc",
		):
			found.append(("Sales Invoice", row.name, "Customer", row.customer, row.posting_date))

	if direction == "Withdrawal" and party_type in (None, "Employee"):
		conditions = "docstatus = 1 and status = 'Unpaid' and company = %(company)s"
		if party:
			conditions += " and employee = %(party)s"
		for row in frappe.db.sql(
			f"""
			select name, employee, posting_date from `tabExpense Claim`
			where {conditions}
				and abs(grand_total - ifnull(total_amount_reimbursed, 0) - %(amount)s) < 0.005
			order by posting_date asc
			""",
			{"company": context.company, "party": party, "amount": amount},
			as_dict=True,
		):
			found.append(("Expense Claim", row.name, "Employee", row.employee, row.posting_date))

	# Advance payments against an order - after invoices, so an invoice wins a tie.
	for doctype, direction_of_order in (("Purchase Order", "Withdrawal"), ("Sales Order", "Deposit")):
		party_type_of_order = ORDER_PARTY_TYPE[doctype]
		if direction != direction_of_order or party_type not in (None, party_type_of_order):
			continue
		for order in open_orders(doctype, context.company, context.currency, party, amount):
			found.append((doctype, order["name"], party_type_of_order, order["party"], order["date"]))

	if transaction_date:
		bank_date = getdate(transaction_date)
		order = {d: i for i, d in enumerate(("Purchase Invoice", "Sales Invoice", "Expense Claim"))}
		found.sort(
			key=lambda f: (
				getdate(f[4]) > bank_date if f[4] else True,
				abs((getdate(f[4]) - bank_date).days) if f[4] else 10**6,
				order.get(f[0], 9),
			)
		)
	return [f[:4] for f in found]


CUSTOMER_TDS_RATES = (2, 5, 10)
TDS_ROUNDING_TOLERANCE = 1.0  # TDS is usually rounded to the rupee


def customer_tds_account(company) -> str | None:
	"""The account customer TDS has been deducted to on past receipts (e.g. "TDS - CDS"),
	else the company's only Asset-side Tax account named like TDS."""
	used = frappe.db.sql(
		"""
		select d.account, count(*) as uses
		from `tabPayment Entry Deduction` d
		join `tabPayment Entry` p on p.name = d.parent
		join `tabAccount` a on a.name = d.account
		where p.docstatus = 1 and p.payment_type = 'Receive' and p.company = %s
			and a.account_type = 'Tax' and a.root_type = 'Asset'
		group by d.account order by uses desc limit 1
		""",
		company,
	)
	if used:
		return used[0][0]
	accounts = frappe.get_all(
		"Account",
		filters={"company": company, "is_group": 0, "root_type": "Asset", "name": ["like", "%TDS%"]},
		pluck="name",
	)
	return accounts[0] if len(accounts) == 1 else None


def find_invoice_with_tds(context: SuggestionContext, amount: float, party=None):
	"""Open Sales Invoices - and Sales Orders, for an advance - whose outstanding less
	2/5/10% TDS on the net total (before GST) equals the deposit:
	[(doctype, name, customer, rate, tds_amount)]. Invoices come first."""
	hits = []
	filters = {
		"docstatus": 1,
		"company": context.company,
		"currency": context.currency,
		"outstanding_amount": [">", amount],
	}
	if party:
		filters["customer"] = party
	for invoice in frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=["name", "customer", "outstanding_amount", "net_total", "grand_total"],
		order_by="posting_date asc",
		limit=200,
	):
		# TDS is withheld once, from the first (full) payment - skip part-paid invoices.
		if abs(flt(invoice.outstanding_amount) - flt(invoice.grand_total)) > 0.005:
			continue
		hits += _tds_hits(
			"Sales Invoice",
			invoice.name,
			invoice.customer,
			invoice.outstanding_amount,
			invoice.net_total,
			amount,
		)

	for order in open_orders("Sales Order", context.company, context.currency, party):
		# Same rule for an order: only while no advance has been paid against it.
		if flt(order["amount"]) <= amount:
			continue
		details = frappe.db.get_value(
			"Sales Order", order["name"], ["net_total", "advance_paid"], as_dict=True
		)
		if flt(details.advance_paid):
			continue
		hits += _tds_hits(
			"Sales Order", order["name"], order["party"], order["amount"], details.net_total, amount
		)
	return hits


def _tds_hits(doctype, name, customer, outstanding, net_total, amount):
	hits = []
	for rate in CUSTOMER_TDS_RATES:
		tds = flt(net_total) * rate / 100
		if abs(flt(outstanding) - tds - amount) <= TDS_ROUNDING_TOLERANCE:
			hits.append((doctype, name, customer, rate, flt(flt(outstanding) - amount, 2)))
	return hits


def _apply_tds(context, suggestion, hit):
	doctype, name, _customer, rate, tds = hit
	suggestion.against_doctype, suggestion.against_name = doctype, name
	suggestion.tds_amount = flt(tds, 2)
	suggestion.tds_account = customer_tds_account(context.company)
	return f"settles {doctype} {name} less {rate}% TDS ({flt(tds, 2)})"


# Implied rate (bank INR / invoice outstanding) vs market rate: allow card markup + fees.
FX_RATIO_RANGE = (0.85, 1.2)
# Words that say nothing about who the party is.
NAME_STOPWORDS = {
	"inc", "llc", "ltd", "limited", "private", "pvt", "india", "technologies", "technology",
	"international", "services", "service", "solutions", "labs", "dba", "co", "opco", "the",
	"and", "group", "pbc", "ag", "gmbh", "io", "com", "www", "pay", "in", "corp", "corporation",
	"company", "systems", "software", "online", "global", "ai",
}  # fmt: skip
_FOREIGN_AMOUNT_RE = re.compile(r"\b([A-Z]{3})\s+([0-9][0-9,]*\.?[0-9]*)\b")


def _name_tokens(text) -> list[str]:
	return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t and t not in NAME_STOPWORDS]


def party_named_in_narration(party_name: str, narration_text: str, counterparty: str | None) -> bool:
	# Callers pass the counterparty (merchant) segment as `narration_text`, never the whole
	# narration: its other segments hold city names and references ("KOCHI", "BANGALORE").
	"""Whether a party's name shows up in the bank narration, allowing for how card and
	bank narrations truncate and glue names ("ANTHROPIC* CL", "APOLLO.IO", "LINODE . AKAM",
	"EXA.AI"). Exact token logic only - no similarity scores."""
	party_tokens = _name_tokens(party_name)
	if not party_tokens:
		return False
	text_tokens = set(_name_tokens(narration_text))
	counterparty_compact = compact(counterparty)
	first = party_tokens[0]
	# The counterparty starts with the party's first significant word ("exa" in "exaai").
	if len(first) >= 3 and counterparty_compact.startswith(first):
		return True
	for token in party_tokens:
		# A distinctive word of the party's name appears as a word of the narration.
		if len(token) >= 5 and (token in text_tokens or token in counterparty_compact):
			return True
		# A narration word is a truncation of a party word ("akam" -> "akamai").
		if any(len(t) >= 4 and token.startswith(t) for t in text_tokens):
			return True
	return False


def _merchant(counterparty: str | None) -> str:
	"""The merchant part of a card counterparty: 'RAZ*Sarvam AI' -> 'Sarvam AI' (a short
	payment-gateway prefix), while 'ANTHROPIC* CL' / 'FACEBK* X' keep their own name."""
	text = counterparty or ""
	if "*" in text:
		prefix, rest = text.split("*", 1)
		if len(prefix.strip()) <= 4 and rest.strip():
			return rest
		return prefix
	return text


def first_word_matches(party_name: str, counterparty: str | None) -> bool:
	"""The first significant word of the party's name and of the bank counterparty are the
	same, or one is a truncation of the other (min. 3 characters): 'OVH' -> 'Ovhtech R&d',
	'GOOGLE CLOUD CYBS' -> 'Google Cloud India'."""
	party_tokens, bank_tokens = _name_tokens(party_name), _name_tokens(_merchant(counterparty))
	if not party_tokens or not bank_tokens:
		return False
	a, b = party_tokens[0], bank_tokens[0]
	return len(min(a, b, key=len)) >= 3 and (a.startswith(b) or b.startswith(a))


def find_party_in_narration(context: SuggestionContext, counterparty: str | None, direction: str):
	"""The only party (of a type that fits the direction) whose name starts like the bank
	counterparty, else None."""
	if not counterparty:
		return None
	hits = {
		(party_type, party)
		for party_type, party, label in context.party_labels
		if party_type in PARTY_TYPES_FOR[direction] and first_word_matches(label, counterparty)
	}
	return hits.pop() if len(hits) == 1 else None


def find_foreign_documents(
	context: SuggestionContext, direction, amount, transaction_date, particulars, party_type=None, party=None
):
	"""Open invoices in a currency other than the bank's for which this line is plausibly
	the payment: [(doctype, name, party_type, party, currency, outstanding, implied_rate, market_rate)],
	best first. Without a known party, the party must be named in the narration."""
	doctype, party_field, own_party_type, name_field = (
		("Purchase Invoice", "supplier", "Supplier", "supplier_name")
		if direction == "Withdrawal"
		else ("Sales Invoice", "customer", "Customer", "customer_name")
	)
	if party_type and party_type != own_party_type:
		return []
	filters = {
		"docstatus": 1,
		"company": context.company,
		"currency": ["!=", context.currency],
		"outstanding_amount": [">", 0],
	}
	if party:
		filters[party_field] = party
	quoted = {
		(c, flt(a.replace(",", ""))) for c, a in _FOREIGN_AMOUNT_RE.findall((particulars or "").upper())
	}
	narration = parse_narration(particulars)

	hits = []
	for doc in frappe.get_all(
		doctype,
		filters=filters,
		fields=["name", party_field, name_field, "currency", "outstanding_amount", "conversion_rate"],
		order_by="posting_date asc",
		limit=200,
	):
		if (doctype, doc.name) in context.used_documents:
			continue
		exact_quote = (doc.currency, flt(doc.outstanding_amount, 2)) in quoted
		if not (
			party
			or exact_quote
			or party_named_in_narration(
				doc.get(name_field) or doc.get(party_field),
				_merchant(narration.counterparty),
				narration.counterparty,
			)
		):
			continue
		market = flt(get_exchange_rate(doc.currency, context.currency, transaction_date)) or flt(
			doc.conversion_rate
		)
		implied = amount / flt(doc.outstanding_amount)
		if not exact_quote and not (FX_RATIO_RANGE[0] <= implied / market <= FX_RATIO_RANGE[1]):
			continue
		hits.append(
			(
				(0 if exact_quote else 1, abs(implied / market - 1)),
				(
					doctype,
					doc.name,
					own_party_type,
					doc.get(party_field),
					doc.currency,
					flt(doc.outstanding_amount, 2),
					implied,
					market,
				),
			)
		)
	return [hit for _key, hit in sorted(hits, key=lambda h: h[0])]


def _apply_foreign(suggestion, hit, count):
	doctype, name, _party_type, _party, currency, outstanding, implied, market = hit
	suggestion.against_doctype, suggestion.against_name = doctype, name
	note = (
		f"settles {doctype} {name} ({currency} {outstanding}) at ~{implied:.2f} vs market {market:.2f};"
		" the difference goes to Exchange Gain/Loss"
	)
	return note + (f" (closest of {count} open)" if count > 1 else "")


SPLIT_LOOKBACK_DAYS = 60
SPLIT_POOL = 20
SPLIT_MAX_SIZE = 5


def find_invoice_combination(
	context: SuggestionContext, direction: str, amount: float, transaction_date, party_type=None, party=None
):
	"""A unique set of open invoices whose outstanding amounts add up exactly to `amount`,
	as a split plan - or None."""
	if direction == "Withdrawal":
		doctype, party_field, party_type_of_doc = "Purchase Invoice", "supplier", "Supplier"
	else:
		doctype, party_field, party_type_of_doc = "Sales Invoice", "customer", "Customer"
	if party_type and party_type != party_type_of_doc:
		return None

	transaction_date = getdate(transaction_date)
	filters = {
		"docstatus": 1,
		"company": context.company,
		"currency": context.currency,
		"outstanding_amount": ["between", [0.01, amount - 0.01]],
		"posting_date": [
			"between",
			[transaction_date - timedelta(days=SPLIT_LOOKBACK_DAYS), transaction_date],
		],
	}
	if party:
		filters[party_field] = party
	pool = frappe.get_all(
		doctype,
		filters=filters,
		fields=["name", party_field, "outstanding_amount", "posting_date"],
		order_by="posting_date desc",
		limit=SPLIT_POOL,
	)
	combos = exact_sum_combinations(pool, amount, lambda d: d.outstanding_amount, SPLIT_MAX_SIZE)
	if len(combos) != 1:
		return None
	return [
		{
			"party_type": party_type_of_doc,
			"party": doc.get(party_field),
			"against_doctype": doctype,
			"against_name": doc.name,
			"amount": flt(doc.outstanding_amount, 2),
		}
		for doc in combos[0]
	]


def suggest(
	context: SuggestionContext, particulars: str, direction: str, amount: float, transaction_date=None
) -> Suggestion:
	"""Confidence: High = party (or account) known from a rule / history / the narration AND
	an exact document (or a Journal Entry account); Medium = one step inferred (a date
	tie-break, TDS, exchange rate, or a party with no document yet); Low = a guess."""
	narration = parse_narration(particulars)
	key = counterparty_key(narration.counterparty)
	suggestion = None

	rule = context.rules.get((direction, key)) if key else None
	if rule:
		suggestion = Suggestion(
			suggested_doctype=rule.suggested_doctype,
			party_type=rule.party_type,
			party=rule.party,
			account=rule.account,
			mode_of_payment=rule.mode_of_payment,
			confidence="High",
			suggestion_source=f"Bank Narration Rule ({key})",
			is_advance=bool(rule.treat_as_advance),
		)
		if suggestion.is_advance:
			suggestion.suggestion_source += f"; advance to {rule.party} (payee marked as advance)"

	if not suggestion and key:
		outcomes = context.history.get((direction, key)) or []
		if outcomes and None not in outcomes and len(set(outcomes)) == 1:
			doctype, party_type, party, account = outcomes[0]
			suggestion = Suggestion(
				suggested_doctype=doctype,
				party_type=party_type,
				party=party,
				account=account,
				confidence="High",
				suggestion_source=f"Same as {len(outcomes)} past reconciled transaction(s) from '{key}'",
			)

	if not suggestion:
		party = find_party_by_name(context, narration.counterparty, direction) or find_party_in_narration(
			context, narration.counterparty, direction
		)
		if party:
			suggestion = Suggestion(
				suggested_doctype="Payment Entry",
				party_type=party[0],
				party=party[1],
				confidence="High",
				suggestion_source=f"Bank name '{narration.counterparty}' matches {party[0]} {party[1]}",
			)

	# Settle an open document when one has exactly this outstanding amount - or, failing
	# that, TDS / exchange-rate / combination matches for that party.
	if (
		suggestion
		and suggestion.party
		and suggestion.suggested_doctype == "Payment Entry"
		and not suggestion.is_advance
	):
		documents = find_open_documents(
			context, direction, amount, suggestion.party_type, suggestion.party, transaction_date
		)
		if documents:
			suggestion.against_doctype, suggestion.against_name = documents[0][:2]
			suggestion.suggestion_source += f"; settles {documents[0][0]} {documents[0][1]}"
			if len(documents) > 1:
				suggestion.suggestion_source += f" (closest by date of {len(documents)} with this amount)"
				suggestion.confidence = "Medium"
		elif (
			direction == "Deposit"
			and suggestion.party_type == "Customer"
			and (hits := find_invoice_with_tds(context, amount, suggestion.party))
		):
			suggestion.suggestion_source += (
				"; "
				+ _apply_tds(context, suggestion, hits[0])
				+ (f" (oldest of {len(hits)} possible)" if len(hits) > 1 else "")
			)
			suggestion.confidence = "Medium"
		elif transaction_date and (
			fx := find_foreign_documents(
				context,
				direction,
				amount,
				transaction_date,
				particulars,
				suggestion.party_type,
				suggestion.party,
			)
		):
			suggestion.suggestion_source += "; " + _apply_foreign(suggestion, fx[0], len(fx))
			suggestion.confidence = "Medium"
		elif transaction_date and (
			plan := find_invoice_combination(
				context, direction, amount, transaction_date, suggestion.party_type, suggestion.party
			)
		):
			suggestion.split_plan = plan
			suggestion.suggestion_source += f"; settles {len(plan)} open invoices that add up exactly"
			suggestion.confidence = "Medium"
		else:
			# Party known, nothing to settle: most likely an advance (proforma, prepaid usage) -
			# worth a draft, not a submit.
			suggestion.confidence = "Medium"
			suggestion.is_advance = True
			suggestion.suggestion_source += "; no open invoice yet - booked as an advance"
	elif not suggestion:
		documents = find_open_documents(context, direction, amount, transaction_date=transaction_date)
		if documents:
			doctype, name, party_type, party = documents[0]
			label = next(
				(lbl for pt, p, lbl in context.party_labels if (pt, p) == (party_type, party)), party
			)
			named = first_word_matches(label, narration.counterparty) or party_named_in_narration(
				label, _merchant(narration.counterparty), narration.counterparty
			)
			if named or len(documents) == 1:
				suggestion = Suggestion(
					suggested_doctype="Payment Entry",
					party_type=party_type,
					party=party,
					against_doctype=doctype,
					against_name=name,
					confidence="High" if named and len({d[3] for d in documents}) == 1 else "Low",
					suggestion_source=(
						f"{doctype} {name} with outstanding exactly {amount}"
						+ (
							f"; {party} named in the narration"
							if named
							else "; only open document with this amount"
						)
					),
				)

	if not suggestion and transaction_date:
		fx = find_foreign_documents(context, direction, amount, transaction_date, particulars)
		# The party must be unambiguous; the closest invoice of that party is proposed.
		if fx and len({h[3] for h in fx}) == 1:
			suggestion = Suggestion(
				suggested_doctype="Payment Entry",
				party_type=fx[0][2],
				party=fx[0][3],
				confidence="Medium",
				suggestion_source=f"{fx[0][3]} named in the narration; ",
			)
			suggestion.suggestion_source += _apply_foreign(suggestion, fx[0], len(fx))

	if not suggestion and direction == "Deposit":
		hits = find_invoice_with_tds(context, amount)
		# Several identical invoices of one customer still identify the customer.
		if hits and len({h[2] for h in hits}) == 1:
			suggestion = Suggestion(
				suggested_doctype="Payment Entry",
				party_type="Customer",
				party=hits[0][2],
				confidence="Low",
			)
			suggestion.suggestion_source = (
				"Only customer with a matching invoice: "
				+ _apply_tds(context, suggestion, hits[0])
				+ (f" (oldest of {len(hits)} possible)" if len(hits) > 1 else "")
			)

	if not suggestion and transaction_date:
		plan = find_invoice_combination(context, direction, amount, transaction_date)
		if plan:
			parties = {(row["party_type"], row["party"]) for row in plan}
			single = parties.pop() if len(parties) == 1 else (None, None)
			suggestion = Suggestion(
				suggested_doctype="Payment Entry",
				party_type=single[0],
				party=single[1],
				split_plan=plan,
				confidence="Low",
				suggestion_source=(
					f"{len(plan)} open invoices from {len({r['party'] for r in plan})} "
					"party(ies) add up exactly to this amount"
				),
			)

	if not suggestion:
		# No document fits, but the party may still be named in the narration (e.g. a second
		# card payment whose invoice isn't entered yet): pre-fill it, at Low confidence.
		named = {
			(party_type, party)
			for party_type, party, label in context.party_labels
			if party_type in PARTY_TYPES_FOR[direction]
			and party_named_in_narration(label, _merchant(narration.counterparty), narration.counterparty)
		}
		if len(named) == 1:
			party_type, party = named.pop()
			suggestion = Suggestion(
				suggested_doctype="Payment Entry",
				party_type=party_type,
				party=party,
				confidence="Low",
				suggestion_source=f"{party} named in the narration; no open document fits - check before creating",
			)

	if not suggestion:
		# Nothing identified: still pre-fill what the direction implies, so the reviewer
		# only has to pick the party or account.
		suggestion = Suggestion(
			party_type="Supplier" if direction == "Withdrawal" else "Customer",
			suggestion_source="No match - pick the party (Payment Entry) or the account (Journal Entry)",
		)
	if not suggestion.mode_of_payment:
		suggestion.mode_of_payment = context.default_mode_of_payment
	return suggestion
