"""Step A: find already-booked Payment Entries / Journal Entries for statement lines.

Candidates are submitted, uncleared vouchers that hit the Bank Account's own GL
account in the same direction. The amount is always compared in the bank account's
currency (a USD receipt's Payment Entry is matched on received_amount in INR, not
paid_amount in USD) and must match exactly.

Tiers, applied greedily across all lines in this order so a voucher is never claimed
twice:
  1. reference + amount   -> Already Booked
  2. amount + date, one unique candidate, no conflicting reference -> Already Booked
  3. amount + date, several candidates or a conflicting reference   -> Check
  4. several vouchers that together equal the amount (a bulk transfer paying several
     parties, or one card payment covering several invoices):
       - all carry the statement's reference                         -> Already Booked
       - a unique combination of 2-4 nearby vouchers, no reference    -> Check
Nothing here is fuzzy - every tier requires an exact amount (or exact sum).
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import combinations

import frappe
from frappe.utils import flt, getdate

from alfaedge_finance.alfaedge_finance.bank_statement.narration import (
	looks_like_reference,
	references_match,
)

BOOKED = "Already Booked"
CHECK = "Check"


@dataclass
class Candidate:
	doctype: str
	name: str
	posting_date: date
	direction: str  # "Withdrawal" | "Deposit"
	amount: float
	reference: str | None
	party_type: str | None = None
	party: str | None = None

	@property
	def key(self):
		return (self.doctype, self.name)

	def as_dict(self):
		return {
			"voucher_type": self.doctype,
			"voucher_no": self.name,
			"posting_date": str(self.posting_date),
			"amount": self.amount,
			"reference": self.reference,
			"party_type": self.party_type,
			"party": self.party,
		}


@dataclass
class LineInput:
	key: str
	transaction_date: date
	direction: str
	amount: float
	reference: str | None
	# Bulk-upload lines pay many parties at once: only a voucher carrying the bank's
	# batch reference may book them, never an amount/date guess.
	reference_only: bool = False


@dataclass
class MatchResult:
	status: str
	candidates: list[Candidate]
	basis: str
	# Each option is a list of vouchers that together would book the line.
	alternatives: list[list[Candidate]] = field(default_factory=list)


MAX_COMBINATION_POOL = 15
MAX_COMBINATION_SIZE = 4


def to_paise(amount) -> int:
	return round(flt(amount) * 100)


def exact_sum_combinations(items, target, amount_of, max_size, max_results=2):
	"""Up to `max_results` combinations (size 2..max_size) of `items` whose amounts sum
	exactly to `target`. Callers only use a result when it is unique, so stopping at 2
	is enough to tell unique from ambiguous."""
	target = to_paise(target)
	found = []
	for size in range(2, max_size + 1):
		for combo in combinations(items, size):
			if sum(to_paise(amount_of(item)) for item in combo) == target:
				found.append(list(combo))
				if len(found) >= max_results:
					return found
	return found


def get_candidates(gl_account: str, from_date, to_date, exclude: set | None = None) -> list[Candidate]:
	exclude = exclude or set()
	candidates = []

	payment_entries = frappe.db.sql(
		"""
		select name, posting_date, payment_type, paid_from, paid_to, paid_amount, received_amount,
			reference_no, party_type, party
		from `tabPayment Entry`
		where docstatus = 1 and ifnull(clearance_date, '') = ''
			and (paid_from = %(account)s or paid_to = %(account)s)
			and posting_date between %(from_date)s and %(to_date)s
		""",
		{"account": gl_account, "from_date": from_date, "to_date": to_date},
		as_dict=True,
	)
	for pe in payment_entries:
		if pe.paid_from == gl_account:
			direction, amount = "Withdrawal", pe.paid_amount
		else:
			direction, amount = "Deposit", pe.received_amount
		candidates.append(
			Candidate(
				"Payment Entry",
				pe.name,
				getdate(pe.posting_date),
				direction,
				flt(amount, 2),
				pe.reference_no,
				pe.party_type,
				pe.party,
			)
		)

	journal_entries = frappe.db.sql(
		"""
		select je.name, je.posting_date, je.cheque_no,
			sum(jea.debit_in_account_currency) as debit, sum(jea.credit_in_account_currency) as credit
		from `tabJournal Entry` je
		join `tabJournal Entry Account` jea on jea.parent = je.name
		where je.docstatus = 1 and ifnull(je.clearance_date, '') = '' and jea.account = %(account)s
			and je.posting_date between %(from_date)s and %(to_date)s
		group by je.name, je.posting_date, je.cheque_no
		""",
		{"account": gl_account, "from_date": from_date, "to_date": to_date},
		as_dict=True,
	)
	for je in journal_entries:
		net = flt(je.debit) - flt(je.credit)
		if not net:
			continue
		candidates.append(
			Candidate(
				"Journal Entry",
				je.name,
				getdate(je.posting_date),
				"Deposit" if net > 0 else "Withdrawal",
				flt(abs(net), 2),
				je.cheque_no,
			)
		)

	return [c for c in candidates if c.key not in exclude]


def _conflicting_reference(line: LineInput, candidate: Candidate) -> bool:
	return (
		bool(line.reference)
		and looks_like_reference(candidate.reference)
		and not references_match(line.reference, candidate.reference)
	)


def match_lines(lines: list[LineInput], candidates: list[Candidate], tolerance_days: int = 7) -> dict:
	"""Returns {line.key: MatchResult} for every line that found something."""
	window = timedelta(days=tolerance_days)
	used = set()
	results = {}

	def in_window(line, c):
		return (
			c.key not in used
			and c.direction == line.direction
			and abs(c.posting_date - line.transaction_date) <= window
		)

	def eligible(line):
		return [c for c in candidates if in_window(line, c) and abs(c.amount - line.amount) < 0.005]

	def closest(line, options):
		return sorted(options, key=lambda c: (abs((c.posting_date - line.transaction_date).days), c.name))

	# Tier 1: reference + amount.
	for line in lines:
		options = [c for c in eligible(line) if references_match(line.reference, c.reference)]
		if options:
			best = closest(line, options)[0]
			used.add(best.key)
			results[line.key] = MatchResult(BOOKED, [best], "Reference + amount")

	# Tier 4a: several vouchers all carrying this line's reference that together make up the
	# amount - e.g. one bulk NEFT paying several employees, each with their own Payment Entry.
	for line in lines:
		if line.key in results or not line.reference:
			continue
		same_ref = [
			c for c in candidates if in_window(line, c) and references_match(line.reference, c.reference)
		]
		if len(same_ref) > 1 and to_paise(sum(c.amount for c in same_ref)) == to_paise(line.amount):
			used.update(c.key for c in same_ref)
			results[line.key] = MatchResult(
				BOOKED, closest(line, same_ref), f"Reference + amounts ({len(same_ref)} vouchers)"
			)

	# Tiers 2 and 3: amount + date.
	for line in lines:
		if line.key in results or line.reference_only:
			continue
		options = closest(line, eligible(line))
		if not options:
			continue
		best = options[0]
		if len(options) == 1 and not _conflicting_reference(line, best):
			results[line.key] = MatchResult(BOOKED, [best], "Amount + date")
		else:
			reason = (
				"Amount + date, but the voucher's reference differs"
				if _conflicting_reference(line, best)
				else f"Amount + date, {len(options)} possible vouchers"
			)
			results[line.key] = MatchResult(CHECK, [best], reason, alternatives=[[c] for c in options])
		used.add(best.key)

	# Tier 4b: a unique combination of nearby vouchers summing exactly to the amount.
	# Coincidental sums are possible, so this is only ever a Check for the reviewer.
	for line in lines:
		if line.key in results or line.reference_only:
			continue
		pool = closest(line, [c for c in candidates if in_window(line, c) and 0 < c.amount < line.amount])[
			:MAX_COMBINATION_POOL
		]
		combos = exact_sum_combinations(pool, line.amount, lambda c: c.amount, MAX_COMBINATION_SIZE)
		if len(combos) == 1:
			combo = combos[0]
			used.update(c.key for c in combo)
			results[line.key] = MatchResult(
				CHECK, combo, f"{len(combo)} vouchers together equal the amount", alternatives=[combo]
			)

	return results
