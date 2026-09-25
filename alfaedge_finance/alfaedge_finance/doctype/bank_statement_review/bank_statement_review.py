"""Bank Statement Review - a staging step before ERPNext's Bank Reconciliation Tool.

One record per uploaded statement file. Each line is linked to the submitted Payment
Entries / Journal Entries that book it (auto-matched, chosen by the reviewer, or created
here as drafts and submitted by hand), or carries a pre-filled suggestion for what to
create. Every linked document is a row in the `vouchers` table, so one bank line can be
booked by several documents (a bulk transfer, or one card payment for invoices from
several suppliers). "Send to Bank Reconciliation" then creates the native Bank
Transactions; the reconciliation itself (clearance dates) still happens in ERPNext's
own tool, which this app does not touch.
"""

import json
from collections import defaultdict
from datetime import timedelta

import frappe
from erpnext.accounts.utils import get_balance_on
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate
from frappe.utils.background_jobs import is_job_enqueued

from alfaedge_finance.alfaedge_finance.bank_statement import bulk_matching, matching
from alfaedge_finance.alfaedge_finance.bank_statement.bulk_upload import (
	normalize_account_no,
	parse_bulk_file,
)
from alfaedge_finance.alfaedge_finance.bank_statement.narration import counterparty_key, parse_narration
from alfaedge_finance.alfaedge_finance.bank_statement.parsers import parse_statement_file
from alfaedge_finance.alfaedge_finance.bank_statement.suggestion import SuggestionContext, suggest
from alfaedge_finance.alfaedge_finance.bank_statement.voucher_builder import (
	bank_amount_of,
	build_bulk_journal_entry,
	build_split_payment_entries,
	build_voucher,
)

BOOKED = "Already Booked"
PARTLY = "Partly Booked"
CHECK = "Check"
SUGGESTED = "Suggested"
DRAFT = "Draft Created"
IMPORTED = "Already Imported"
IGNORED = "Ignored"
OPEN_STATUSES = (SUGGESTED, CHECK, DRAFT, PARTLY)

SUGGESTION_FIELDS = (
	"suggested_doctype",
	"party_type",
	"party",
	"account",
	"against_doctype",
	"against_name",
	"mode_of_payment",
	"tds_amount",
	"tds_account",
	"is_advance",
	"advance_reference",
)
EDITED_SOURCE = "Edited by reviewer"

DOCSTATUS_LABEL = {0: "Draft", 1: "Submitted", 2: "Cancelled"}

# Set while this module deletes a voucher itself, so the voucher's on_trash hook
# (voucher_sync) doesn't also try to update the review mid-save.
SKIP_SYNC_FLAG = "bank_statement_review_skip_sync"


class BankStatementReview(Document):
	def validate(self):
		if not self.lines:
			self.load_statement()
			self.analyse()
		self.set_totals()
		self.set_status()

	def after_insert(self):
		# However the review was created (list upload or the form), apply the auto-draft /
		# auto-submit switches once its lines exist.
		if self.flags.get("skip_auto_process"):
			return
		self.auto_process()
		self.save()

	def on_trash(self):
		if any(line.bank_transaction for line in self.lines):
			frappe.throw(
				_("{0} has already been sent to Bank Reconciliation and can no longer be deleted.").format(
					self.name
				)
			)
		# Drafts created from this review go with it; submitted entries are never touched.
		for row in self.vouchers:
			if (
				row.link_type == "Created"
				and frappe.db.get_value(row.voucher_type, row.voucher_no, "docstatus") == 0
			):
				_delete_voucher(row.voucher_type, row.voucher_no)

	# ------------------------------------------------------------------ parsing

	def load_statement(self):
		if not self.statement_file:
			frappe.throw(_("Attach the bank statement file."))
		file_doc = frappe.get_doc("File", {"file_url": self.statement_file})
		statement = parse_statement_file(file_doc.get_content(), file_doc.file_name)

		bank_account_no = (
			frappe.db.get_value("Bank Account", self.bank_account, "bank_account_no") or ""
		).strip()
		if bank_account_no != statement.account_number:
			frappe.throw(
				_("This statement is for account {0}, but Bank Account {1} has account number {2}.").format(
					statement.account_number, self.bank_account, bank_account_no or _("(not set)")
				)
			)
		account_currency = frappe.get_cached_value("Account", self.gl_account, "account_currency")
		if statement.currency and account_currency and statement.currency != account_currency:
			frappe.throw(
				_("Statement currency {0} does not match the bank GL account currency {1}.").format(
					statement.currency, account_currency
				)
			)

		self.statement_account_no = statement.account_number
		self.from_date = statement.from_date
		self.to_date = statement.to_date
		self.currency = statement.currency or account_currency
		self.opening_balance = statement.opening_balance
		self.closing_balance = statement.closing_balance
		self.title = f"{self.bank_account}: {statement.from_date.strftime('%d-%m-%Y')} to {statement.to_date.strftime('%d-%m-%Y')}"

		for line in statement.lines:
			narration = parse_narration(line.particulars)
			self.append(
				"lines",
				{
					"transaction_date": line.transaction_date,
					"value_date": line.value_date,
					"particulars": line.particulars,
					"reference": narration.reference or line.cheque_number,
					"counterparty": narration.counterparty,
					"channel": narration.channel,
					"bulk_count": _bulk_count(line.particulars)
					if narration.channel == "Bulk Upload"
					else None,
					"withdrawal": line.withdrawal,
					"deposit": line.deposit,
					"balance": line.balance,
					"currency": self.currency,
				},
			)

	def set_totals(self):
		self.total_withdrawal = sum(flt(line.withdrawal) for line in self.lines)
		self.total_deposit = sum(flt(line.deposit) for line in self.lines)
		if self.gl_account and self.to_date:
			self.book_balance = get_balance_on(self.gl_account, self.to_date, company=self.company)

	def set_status(self):
		statuses = [line.line_status for line in self.lines]
		if self.lines and all(line.bank_transaction or line.line_status == IMPORTED for line in self.lines):
			self.status = "Sent"
		elif not any(s in (*OPEN_STATUSES, None, "") for s in statuses):
			self.status = "Reviewed"
		else:
			self.status = "Draft"

	# ------------------------------------------------------------ voucher links

	def line_rows(self, line):
		# Joined on the line number: row names don't exist yet during the first save,
		# and lines are never reordered or removed.
		return [row for row in self.vouchers if row.line_no == line.idx]

	def _add_voucher(self, line, link_type, doctype, name, amount=None):
		info = _voucher_info(doctype, name, self.gl_account)
		if amount is not None:
			info["amount"] = flt(amount, 2)
		info["is_advance"] = cint(line.is_advance)
		self.append(
			"vouchers",
			{
				"line_no": line.idx,
				"link_type": link_type,
				"voucher_type": doctype,
				"voucher_no": name,
				**info,
			},
		)

	def refresh_line(self, line):
		"""Re-read the status of every voucher linked to `line` (dropping deleted and
		cancelled ones) and derive the line status from how much of it they book."""
		for row in self.line_rows(line):
			current = frappe.db.get_value(
				row.voucher_type,
				row.voucher_no,
				["docstatus", REFERENCE_FIELD[row.voucher_type]],
				as_dict=True,
			)
			if not current or current.docstatus == 2:
				self.remove(row)
				if row.link_type == "Created":
					# The reviewer deleted or cancelled what was created: keep the suggestion for
					# them to use by hand, but never re-create / re-submit it automatically.
					line.suggestion_source = EDITED_SOURCE
					line.confidence = None
			else:
				row.voucher_status = DOCSTATUS_LABEL[current.docstatus]
				row.reference_no = current.get(REFERENCE_FIELD[row.voucher_type])

		rows = self.line_rows(line)
		submitted = flt(sum(flt(r.amount) for r in rows if r.voucher_status == "Submitted"), 2)
		drafts = flt(sum(flt(r.amount) for r in rows if r.voucher_status == "Draft"), 2)
		line.allocated_amount = submitted
		if line.bank_transaction or line.line_status in (IGNORED, IMPORTED):
			return

		amount = _line_amount(line)
		if not rows:
			if line.line_status in (BOOKED, DRAFT, PARTLY):
				line.line_status = SUGGESTED
				line.match_basis = None
		elif abs(submitted - amount) < 0.005:
			line.line_status = BOOKED
		elif drafts and abs(submitted + drafts - amount) < 0.005:
			line.line_status = DRAFT
		else:
			line.line_status = PARTLY

	def refresh_voucher_links(self):
		for line in self.lines:
			self.refresh_line(line)
		for index, row in enumerate(self.vouchers, start=1):
			row.idx = index

	def _claimed_vouchers(self) -> set:
		"""Vouchers already linked to a line on this or any other review, so one voucher
		is never matched to two bank lines."""
		claimed = {(row.voucher_type, row.voucher_no) for row in self.vouchers}
		for row in frappe.get_all(
			"Bank Statement Review Voucher",
			filters={"parent": ["!=", self.name or ""]},
			fields=["voucher_type", "voucher_no"],
		):
			claimed.add((row.voucher_type, row.voucher_no))
		return claimed

	def _remaining(self, line):
		"""Amount of the line not yet covered by linked vouchers (submitted or draft)."""
		return flt(_line_amount(line) - sum(flt(r.amount) for r in self.line_rows(line)), 2)

	# ----------------------------------------------------------------- analysis

	def analyse(self) -> dict:
		"""Match / suggest every line that isn't settled yet. Safe to run repeatedly:
		lines already sent, ignored, or with linked vouchers are left alone (their
		voucher statuses are refreshed)."""
		before = {line.name or line.idx: line.line_status for line in self.lines}
		self._mark_duplicates()
		self.refresh_voucher_links()

		pending = [
			line
			for line in self.lines
			if not line.bank_transaction
			and line.line_status in (None, "", SUGGESTED, CHECK)
			and not self.line_rows(line)
		]
		if pending:
			self._match(pending)
			context = SuggestionContext(self.company, self.gl_account, self.currency)
			# Documents other lines already point at - by suggestion or by a created voucher.
			context.used_documents = {
				(line.against_doctype, line.against_name)
				for line in self.lines
				if line.against_name and line not in pending
			}
			for row in self.vouchers:
				if row.voucher_type != "Payment Entry":
					continue
				context.used_documents.update(
					(r.reference_doctype, r.reference_name)
					for r in frappe.get_all(
						"Payment Entry Reference",
						filters={"parent": row.voucher_no},
						fields=["reference_doctype", "reference_name"],
					)
				)
			for line in pending:
				if line.line_status == BOOKED:
					continue
				# A Check line still gets a suggestion, for when none of its candidates is right.
				if line.line_status != CHECK:
					line.line_status = SUGGESTED
				source = line.suggestion_source or ""
				if line.bulk_plan or source == EDITED_SOURCE:
					continue
				if _is_bulk(line):
					# Don't guess who a bulk payment went to - by default the bank's file says.
					# The reviewer can still pick the record by hand (kept as EDITED_SOURCE).
					line.update(BULK_PLACEHOLDER)
					continue
				direction = "Withdrawal" if flt(line.withdrawal) else "Deposit"
				result = suggest(
					context, line.particulars, direction, _line_amount(line), line.transaction_date
				)
				line.update(result.as_dict())
				if result.against_name:
					context.used_documents.add((result.against_doctype, result.against_name))

		return self._summarise(before)

	def _mark_duplicates(self):
		"""A line already present as a Bank Transaction, or on another Bank Statement
		Review for this bank account, is marked Already Imported (overlapping daily /
		weekly / monthly uploads). Repeated identical lines are counted, not collapsed."""
		own_transactions = {line.bank_transaction for line in self.lines if line.bank_transaction}
		reviewed_transactions = set(
			frappe.get_all(
				"Bank Statement Review Line",
				filters={"bank_transaction": ["is", "set"]},
				pluck="bank_transaction",
			)
		)
		existing = defaultdict(list)

		for bt in frappe.get_all(
			"Bank Transaction",
			filters={
				"bank_account": self.bank_account,
				"docstatus": ["!=", 2],
				"date": ["between", [self.from_date, self.to_date]],
			},
			fields=["name", "date", "withdrawal", "deposit", "description"],
			order_by="creation asc",
		):
			if bt.name in own_transactions or bt.name in reviewed_transactions:
				continue
			existing[_line_key(bt.date, bt.withdrawal, bt.deposit, bt.description)].append(bt.name)

		other_lines = frappe.db.sql(
			"""
			select line.parent, line.bank_transaction, line.transaction_date, line.withdrawal,
				line.deposit, line.particulars
			from `tabBank Statement Review Line` line
			join `tabBank Statement Review` review on review.name = line.parent
			where review.bank_account = %(bank_account)s and review.name != %(name)s
				and review.creation < %(creation)s
				and line.transaction_date between %(from_date)s and %(to_date)s
				and ifnull(line.line_status, '') != %(imported)s
			order by review.creation asc, line.idx asc
			""",
			{
				"bank_account": self.bank_account,
				"name": self.name or "",
				"from_date": self.from_date,
				"to_date": self.to_date,
				"imported": IMPORTED,
				# Only reviews uploaded before this one: re-running an older review must not
				# hand its lines over to a newer, overlapping one.
				"creation": self.creation or frappe.utils.now_datetime(),
			},
			as_dict=True,
		)
		for row in other_lines:
			# Point at the Bank Transaction once that review has sent it, so this line can link to it.
			existing[_line_key(row.transaction_date, row.withdrawal, row.deposit, row.particulars)].append(
				row.bank_transaction or row.parent
			)

		seen = defaultdict(int)
		for line in self.lines:
			if line.bank_transaction or self.line_rows(line):
				continue  # sent, or already has entries - never re-labelled a duplicate
			key = _line_key(line.transaction_date, line.withdrawal, line.deposit, line.particulars)
			occurrence = seen[key]
			seen[key] += 1
			if occurrence < len(existing[key]):
				line.line_status = IMPORTED
				line.duplicate_of = existing[key][occurrence]
			elif line.line_status == IMPORTED:
				line.line_status = None
				line.duplicate_of = None

	def _match(self, pending):
		tolerance = self.date_tolerance_days if self.date_tolerance_days is not None else 7
		window = timedelta(days=tolerance)
		candidates = matching.get_candidates(
			self.gl_account,
			getdate(self.from_date) - window,
			getdate(self.to_date) + window,
			exclude=self._claimed_vouchers(),
		)
		inputs = [
			matching.LineInput(
				key=str(line.idx),
				transaction_date=getdate(line.transaction_date),
				direction="Withdrawal" if flt(line.withdrawal) else "Deposit",
				amount=_line_amount(line),
				reference=line.reference,
				reference_only=_is_bulk(line),
			)
			for line in pending
		]
		results = matching.match_lines(inputs, candidates, tolerance)

		for line in pending:
			result = results.get(str(line.idx))
			line.alternatives = None
			line.match_basis = result.basis if result else None
			if not result:
				if line.line_status == CHECK:
					line.line_status = SUGGESTED
				continue
			if result.status == matching.BOOKED:
				for candidate in result.candidates:
					self._add_voucher(line, "Matched", candidate.doctype, candidate.name, candidate.amount)
				self.refresh_line(line)
			else:
				line.line_status = CHECK
				line.alternatives = json.dumps(
					[[c.as_dict() for c in option] for option in result.alternatives], default=str
				)

	def _summarise(self, before) -> dict:
		counts = defaultdict(int)
		newly_booked = 0
		for line in self.lines:
			counts[line.line_status or "Pending"] += 1
			previous = before.get(line.name or line.idx)
			if line.line_status == BOOKED and previous != BOOKED:
				newly_booked += 1
		parts = [f"{newly_booked} newly booked"] if newly_booked else []
		parts += [f"{count} {status.lower()}" for status, count in counts.items()]
		summary = ", ".join(parts)
		self.last_run_summary = f"{frappe.utils.now_datetime().strftime('%d-%m-%Y %H:%M')}: {summary}"
		return {"newly_booked": newly_booked, "counts": dict(counts), "summary": summary}

	# ---------------------------------------------------------- client actions

	def _get_line(self, row_name):
		for line in self.lines:
			if line.name == row_name:
				return line
		frappe.throw(_("Line {0} not found on {1}").format(row_name, self.name))

	def _apply_values(self, line, values):
		values = frappe.parse_json(values) if isinstance(values, str) else (values or {})
		changed = False
		for field in SUGGESTION_FIELDS:
			if field in values and (values.get(field) or None) != (line.get(field) or None):
				line.set(field, values.get(field) or None)
				changed = True
		if "split_plan" in values:
			plan = values.get("split_plan") or None
			plan = json.dumps(plan) if isinstance(plan, list) else plan
			if (plan or None) != (line.split_plan or None):
				line.split_plan = plan
				changed = True
		if changed:
			line.suggestion_source = EDITED_SOURCE
			line.confidence = None
		return values

	def _check_open(self, line):
		if line.bank_transaction:
			frappe.throw(_("Row {0} has already been sent to Bank Reconciliation.").format(line.idx))
		if line.line_status not in (SUGGESTED, CHECK, PARTLY):
			frappe.throw(_("Row {0} is {1}; nothing to create.").format(line.idx, line.line_status))

	@frappe.whitelist()
	def rerun(self):
		self.check_permission("write")
		result = self.analyse()
		result["auto"] = self.auto_process()
		result["summary"] = self.last_run_summary.split(": ", 1)[-1]
		self.save()
		return result

	@frappe.whitelist()
	def save_line_values(self, row_name, values):
		self.check_permission("write")
		self._apply_values(self._get_line(row_name), values)
		self.save()

	@frappe.whitelist()
	def create_draft(self, row_name, values=None):
		"""Create one Draft voucher for the line, or - when `values.split_plan` is given -
		one Draft Payment Entry per party of the split."""
		self.check_permission("write")
		line = self._get_line(row_name)
		values = self._apply_values(line, values)
		if line.bulk_plan:
			vouchers = [self._create_bulk_draft(line, values)]
		elif line.split_plan and values.get("use_split", bool(values.get("split_plan"))):
			vouchers = self._create_split_drafts(line, values)
		else:
			vouchers = [self._create_draft_for(line, values)]
		self.save()
		return [{"doctype": v.doctype, "name": v.name} for v in vouchers]

	def _create_draft_for(self, line, values=None, learn=True):
		self._check_open(line)
		remaining = self._remaining(line)
		values = {field: line.get(field) for field in SUGGESTION_FIELDS} | {
			k: v for k, v in (values or {}).items() if k == "remarks" and v
		}
		voucher = build_voucher(self, line, values, amount=remaining)
		voucher.insert()
		self._add_voucher(line, "Created", voucher.doctype, voucher.name, remaining)
		self.refresh_line(line)
		if learn:
			self._remember_rule(line)
		return voucher

	def _create_split_drafts(self, line, values=None):
		self._check_open(line)
		plan = json.loads(line.split_plan) if isinstance(line.split_plan, str) else line.split_plan
		entries = build_split_payment_entries(self, line, plan, values, target=self._remaining(line))
		for entry in entries:
			entry.insert()
			self._add_voucher(line, "Created", entry.doctype, entry.name)
		self.refresh_line(line)
		return entries

	def _create_bulk_draft(self, line, values=None):
		self._check_open(line)
		plan = json.loads(line.bulk_plan)
		je = build_bulk_journal_entry(self, line, plan, target=self._remaining(line), values=values)
		je.insert()
		self._add_voucher(line, "Created", je.doctype, je.name, self._remaining(line))
		self.refresh_line(line)
		return je

	@frappe.whitelist()
	def upload_bulk_file(self, row_name, filename, content):
		"""Read the bank's bulk-payment upload file for a bulk debit line and resolve
		every beneficiary row to a party and the documents it paid."""
		import base64

		self.check_permission("write")
		line = self._get_line(row_name)
		self._check_open(line)
		if not flt(line.withdrawal):
			frappe.throw(_("Row {0} is a deposit; bulk payment files are for bulk debits.").format(line.idx))

		file_bytes = base64.b64decode(content)
		rows = parse_bulk_file(file_bytes, filename)

		warnings = []
		company_account_no = normalize_account_no(
			frappe.db.get_value("Bank Account", self.bank_account, "bank_account_no")
		)
		other_debit = {r.debit_account_no for r in rows if r.debit_account_no} - {company_account_no}
		if other_debit:
			frappe.throw(
				_("The bulk file debits account {0}, not this statement's account {1}.").format(
					", ".join(sorted(other_debit)), company_account_no
				)
			)
		total = flt(sum(r.amount for r in rows), 2)
		remaining = self._remaining(line)
		if abs(total - remaining) > 0.005:
			frappe.throw(
				_("The bulk file adds up to {0}, but row {1} has {2} left to book.").format(
					total, line.idx, remaining
				)
			)
		line.bulk_count = line.bulk_count or _bulk_count(line.particulars)
		if line.bulk_count and line.bulk_count != len(rows):
			warnings.append(
				_("The bank reports {0} beneficiaries for this payment, the file has {1}.").format(
					line.bulk_count, len(rows)
				)
			)

		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": filename,
				"content": file_bytes,
				"is_private": 1,
				"attached_to_doctype": self.doctype,
				"attached_to_name": self.name,
			}
		).insert()
		plan = bulk_matching.resolve_rows(self.company, self.currency, line.transaction_date, rows)
		line.bulk_file = file_doc.file_url
		self._set_bulk_plan(line, plan)
		self.save()
		return {"summary": line.suggestion_source, "warnings": warnings, "plan": plan}

	@frappe.whitelist()
	def save_bulk_plan(self, row_name, plan):
		"""Re-validate a bulk plan edited by the reviewer (parties / documents)."""
		self.check_permission("write")
		line = self._get_line(row_name)
		self._check_open(line)
		plan = frappe.parse_json(plan) if isinstance(plan, str) else plan
		self._set_bulk_plan(line, bulk_matching.validate_plan(self.company, plan))
		self.save()
		return json.loads(line.bulk_plan)

	@frappe.whitelist()
	def clear_bulk_plan(self, row_name):
		self.check_permission("write")
		line = self._get_line(row_name)
		self._check_open(line)
		line.bulk_plan = line.bulk_file = None
		line.suggestion_source = None
		self.analyse()
		self.save()

	def _set_bulk_plan(self, line, plan):
		text, confidence = bulk_matching.summarise(plan)
		line.update(
			{
				"bulk_plan": json.dumps(plan, default=str),
				"suggested_doctype": "Journal Entry",
				"party_type": None,
				"party": None,
				"account": None,
				"against_doctype": None,
				"against_name": None,
				"split_plan": None,
				"confidence": confidence,
				"suggestion_source": text,
			}
		)

	@frappe.whitelist()
	def mark_as_advance(self, row_names, values=None):
		"""Book the given lines as advances: a Payment Entry to the party with no invoice
		(or against an open order), created as a Draft. `values`: party_type / party (used
		for lines without a detected party), against_doctype / against_name (an order,
		single line only), reference (proforma no.), remember (treat the payee as an
		advance from now on). Each line is independent - one failing doesn't stop the rest."""
		self.check_permission("write")
		row_names = frappe.parse_json(row_names) if isinstance(row_names, str) else row_names
		values = frappe.parse_json(values) if isinstance(values, str) else (values or {})
		lines = [self._get_line(name) for name in row_names or []]
		if not lines:
			frappe.throw(_("Select at least one line."))

		for line in lines:
			# A line whose unsubmitted draft was created from the suggestion: replace that draft.
			if line.line_status == DRAFT:
				for row in self.line_rows(line):
					if row.link_type == "Created" and row.voucher_status == "Draft":
						_delete_voucher(row.voucher_type, row.voucher_no)
				self.refresh_line(line)
			self._check_open(line)
			direction_party_type = "Supplier" if flt(line.withdrawal) else "Customer"
			party_type = values.get("party_type") or line.party_type or direction_party_type
			own_party = line.party if line.party_type == party_type else None
			if len(lines) == 1:
				party = values.get("party") or own_party  # the dialog shows and edits this line's party
			else:
				party = own_party or values.get("party")  # each line keeps what was detected for it
			line.update(
				{
					"suggested_doctype": "Payment Entry",
					"party_type": party_type,
					"party": party,
					"account": None,
					"against_doctype": values.get("against_doctype") if len(lines) == 1 else None,
					"against_name": values.get("against_name") if len(lines) == 1 else None,
					"split_plan": None,
					"tds_amount": 0,
					"tds_account": None,
					"is_advance": 1,
					"advance_reference": values.get("reference") or line.advance_reference,
					"suggestion_source": EDITED_SOURCE,
					"confidence": None,
				}
			)

		names = {line.name for line in lines}
		result = self._create_drafts(lambda line: line.name in names)
		if cint(values.get("remember")):
			for line in result["lines"]:
				self._remember_rule(line, advance=True)
		self.save()
		return {"created": [v.name for v in result["vouchers"]], "failed": result["failed"]}

	@frappe.whitelist()
	def delete_draft(self, voucher_type, voucher_no):
		"""Delete a Draft voucher created from this review and put its line back to Suggested."""
		self.check_permission("write")
		rows = [
			r
			for r in self.vouchers
			if r.voucher_type == voucher_type and r.voucher_no == voucher_no and r.link_type == "Created"
		]
		if not rows:
			frappe.throw(_("{0} {1} was not created from {2}.").format(voucher_type, voucher_no, self.name))
		if frappe.db.get_value(voucher_type, voucher_no, "docstatus") != 0:
			frappe.throw(
				_("{0} {1} is not a draft; cancel it from its own form instead.").format(
					voucher_type, voucher_no
				)
			)
		_delete_voucher(voucher_type, voucher_no)
		self.refresh_voucher_links()
		self.save()

	@frappe.whitelist()
	def submit_draft(self, voucher_type, voucher_no):
		"""Submit a Draft voucher created from this review - an explicit reviewer action,
		the same as clicking Submit on its own form (its permissions and validations apply)."""
		self.check_permission("write")
		self._created_draft_row(voucher_type, voucher_no)
		voucher = frappe.get_doc(voucher_type, voucher_no)
		frappe.flags[SKIP_SYNC_FLAG] = True
		try:
			voucher.submit()
		finally:
			frappe.flags[SKIP_SYNC_FLAG] = False
		self.refresh_voucher_links()
		self.save()
		return {"doctype": voucher_type, "name": voucher_no}

	@frappe.whitelist()
	def submit_drafts_in_background(self, vouchers):
		"""Queue the selected Draft vouchers (created from this review) for submission in a
		background job. Each is submitted separately, so one failure doesn't stop the rest;
		the voucher sync hook updates this review as each one is submitted."""
		self.check_permission("write")
		vouchers = frappe.parse_json(vouchers) if isinstance(vouchers, str) else vouchers
		queue = []
		for voucher in vouchers or []:
			self._created_draft_row(voucher["voucher_type"], voucher["voucher_no"])
			if not frappe.has_permission(voucher["voucher_type"], "submit", voucher["voucher_no"]):
				frappe.throw(_("You are not allowed to submit {0}.").format(voucher["voucher_no"]))
			queue.append((voucher["voucher_type"], voucher["voucher_no"]))
		if not queue:
			frappe.throw(_("Select at least one draft to submit."))
		if is_job_enqueued(_submit_job_id(self.name)):
			frappe.throw(
				_("Drafts of {0} are already being submitted - wait for that to finish.").format(self.name)
			)
		self._queue_submission(queue)
		return {"queued": len(queue)}

	@frappe.whitelist()
	def update_voucher_reference(self, voucher_type, voucher_no, reference_no):
		"""Change the Cheque/Reference No of a draft linked to this review. ERPNext doesn't
		allow it on a submitted entry (that needs cancel + amend)."""
		self.check_permission("write")
		rows = [r for r in self.vouchers if r.voucher_type == voucher_type and r.voucher_no == voucher_no]
		if not rows:
			frappe.throw(_("{0} {1} is not linked to {2}.").format(voucher_type, voucher_no, self.name))
		reference_no = (reference_no or "").strip()
		if not reference_no:
			frappe.throw(_("The reference can't be empty."))
		voucher = frappe.get_doc(voucher_type, voucher_no)
		if voucher.docstatus != 0:
			frappe.throw(
				_(
					"{0} {1} is submitted; ERPNext doesn't allow changing its reference. Cancel and amend it instead."
				).format(voucher_type, voucher_no)
			)
		voucher.set(REFERENCE_FIELD[voucher_type], reference_no)
		voucher.save()
		for row in rows:
			row.reference_no = reference_no
		self.save()
		return reference_no

	def _queue_submission(self, vouchers) -> int:
		"""Queue drafts for the background submit job (after this transaction commits).
		Returns how many were queued - 0 if a submit job for this review is already running."""
		if is_job_enqueued(_submit_job_id(self.name)):
			return 0
		frappe.enqueue(
			"alfaedge_finance.alfaedge_finance.doctype.bank_statement_review.bank_statement_review.submit_drafts_job",
			queue="long",
			timeout=60 * 60,
			job_id=_submit_job_id(self.name),
			deduplicate=True,
			enqueue_after_commit=True,
			review_name=self.name,
			vouchers=list(vouchers),
			user=frappe.session.user,
		)
		return len(vouchers)

	def _created_draft_row(self, voucher_type, voucher_no):
		rows = [
			r
			for r in self.vouchers
			if r.voucher_type == voucher_type and r.voucher_no == voucher_no and r.link_type == "Created"
		]
		if not rows:
			frappe.throw(_("{0} {1} was not created from {2}.").format(voucher_type, voucher_no, self.name))
		if frappe.db.get_value(voucher_type, voucher_no, "docstatus") != 0:
			frappe.throw(_("{0} {1} is not a draft.").format(voucher_type, voucher_no))
		return rows[0]

	@frappe.whitelist()
	def get_unsaved_voucher(self, row_name, values=None):
		"""Pre-filled but unsaved voucher for the browser to open as a new form."""
		self.check_permission("read")
		line = self._get_line(row_name)
		values = frappe.parse_json(values) if isinstance(values, str) else (values or {})
		merged = {field: line.get(field) for field in SUGGESTION_FIELDS} | {
			k: v for k, v in values.items() if v
		}
		voucher = build_voucher(self, line, merged, amount=self._remaining(line))
		if voucher.doctype == "Payment Entry":
			# Same as erpnext's create_payment_entry_bts(allow_edit=1): validate fills in
			# party account, exchange rates and totals before the form opens.
			voucher.validate()
		return _as_local_doc(voucher)

	@frappe.whitelist()
	def create_high_confidence_drafts(self):
		self.check_permission("write")
		result = self._create_drafts(lambda line: line.confidence == "High")
		self.save()
		return {"created": [v.name for v in result["vouchers"]], "failed": result["failed"]}

	def _create_drafts(self, wanted, learn=True) -> dict:
		"""Create the suggested draft(s) for every Suggested line `wanted(line)` accepts. Each
		line is its own savepoint, so one that can't be built doesn't stop the rest."""
		vouchers, failed, lines = [], [], []
		for line in self.lines:
			if line.line_status != SUGGESTED or line.bank_transaction or not wanted(line):
				continue
			if _is_bulk(line) and not line.bulk_plan:
				continue  # bulk payments are only booked from the bank's file
			voucher_count, status, allocated = len(self.vouchers), line.line_status, line.allocated_amount
			try:
				frappe.db.savepoint("bsr_draft")
				if line.bulk_plan:
					created = [self._create_bulk_draft(line)]
				elif line.split_plan:
					created = self._create_split_drafts(line)
				else:
					created = [self._create_draft_for(line, learn=learn)]
				frappe.db.release_savepoint("bsr_draft")
				vouchers += created
				lines.append(line)
			except Exception as e:
				# Undo this line's partial work - in the database and on the in-memory review.
				frappe.db.rollback(save_point="bsr_draft")
				for row in self.vouchers[voucher_count:]:
					self.remove(row)
				line.line_status, line.allocated_amount = status, allocated
				frappe.clear_last_message()
				failed.append(f"Row {line.idx}: {frappe.utils.strip_html(str(e))}")
		return {"vouchers": vouchers, "lines": lines, "failed": failed}

	def auto_process(self) -> dict:
		"""After upload / Re-run: create drafts for High (and, if enabled, Medium) confidence
		lines, then queue the High ones for submission in the background."""
		submit_high, draft_medium = cint(self.auto_submit_high), cint(self.auto_draft_medium)
		if not (submit_high or draft_medium):
			return {"drafted": 0, "queued": 0, "failed": []}

		def wanted(line):
			return (line.confidence == "High" and (submit_high or draft_medium)) or (
				line.confidence == "Medium" and draft_medium
			)

		# Rules are only learned from what a reviewer creates, never from automatic drafts -
		# otherwise a wrong guess would come back as a High "rule" match and be auto-submitted.
		result = self._create_drafts(wanted, learn=False)
		to_submit = []
		if submit_high:
			high_lines = {line.idx for line in result["lines"] if line.confidence == "High"}
			to_submit = [
				(row.voucher_type, row.voucher_no)
				for row in self.vouchers
				if row.line_no in high_lines and row.link_type == "Created" and row.voucher_status == "Draft"
			]
		queued = self._queue_submission(to_submit) if to_submit else 0
		summary = f"auto: {len(result['vouchers'])} draft(s) created, {queued} queued for submission"
		if result["failed"]:
			summary += f", {len(result['failed'])} could not be created"
		self.last_run_summary = f"{self.last_run_summary or ''}; {summary}".strip("; ")
		return {"drafted": len(result["vouchers"]), "queued": queued, "failed": result["failed"]}

	@frappe.whitelist()
	def accept_option(self, row_name, option_index):
		"""Link one of a Check line's candidate options (one or several vouchers)."""
		self.check_permission("write")
		line = self._get_line(row_name)
		options = json.loads(line.alternatives or "[]")
		option_index = cint(option_index)
		if not 0 <= option_index < len(options):
			frappe.throw(_("That option is no longer available; re-run reconciliation."))
		for voucher in options[option_index]:
			self._link_existing(line, voucher["voucher_type"], voucher["voucher_no"])
		line.alternatives = None
		line.match_basis = _("Chosen by reviewer")
		self.refresh_line(line)
		self.save()

	@frappe.whitelist()
	def accept_voucher(self, row_name, voucher_type, voucher_no):
		"""Link an existing submitted voucher chosen by hand."""
		self.check_permission("write")
		line = self._get_line(row_name)
		self._link_existing(line, voucher_type, voucher_no)
		line.alternatives = None
		line.match_basis = _("Chosen by reviewer")
		self.refresh_line(line)
		self.save()

	def _link_existing(self, line, voucher_type, voucher_no):
		if voucher_type not in ("Payment Entry", "Journal Entry"):
			frappe.throw(_("Only Payment Entries and Journal Entries can be linked."))
		if line.bank_transaction:
			frappe.throw(_("Row {0} has already been sent to Bank Reconciliation.").format(line.idx))
		if frappe.db.get_value(voucher_type, voucher_no, "docstatus") != 1:
			frappe.throw(_("{0} {1} is not submitted.").format(voucher_type, voucher_no))
		if (voucher_type, voucher_no) in self._claimed_vouchers():
			frappe.throw(_("{0} {1} is already linked to a statement line.").format(voucher_type, voucher_no))
		amount = bank_amount_of(voucher_type, voucher_no, self.gl_account)
		if not amount:
			frappe.throw(
				_("{0} {1} does not touch bank account {2}.").format(
					voucher_type, voucher_no, self.gl_account
				)
			)
		if amount - self._remaining(line) > 0.005:
			frappe.throw(
				_("{0} {1} is {2}, more than the {3} left unbooked on row {4}.").format(
					voucher_type, voucher_no, amount, self._remaining(line), line.idx
				)
			)
		self._add_voucher(line, "Chosen", voucher_type, voucher_no, amount)

	@frappe.whitelist()
	def set_ignored(self, row_name, ignored):
		self.check_permission("write")
		line = self._get_line(row_name)
		if cint(ignored):
			if line.line_status not in (SUGGESTED, CHECK):
				frappe.throw(_("Only Suggested or Check lines can be ignored."))
			line.line_status = IGNORED
		else:
			line.line_status = None
			self.analyse()
		self.save()

	@frappe.whitelist()
	def send_to_bank_reconciliation(self):
		self.check_permission("write")
		created = linked = 0
		for line in self.lines:
			if line.bank_transaction:
				continue
			if line.line_status == IMPORTED:
				if line.duplicate_of and frappe.db.exists("Bank Transaction", line.duplicate_of):
					line.bank_transaction = line.duplicate_of
					linked += 1
				continue

			party_type, party = self._line_party(line)
			bt = frappe.get_doc(
				{
					"doctype": "Bank Transaction",
					"date": line.transaction_date,
					"bank_account": self.bank_account,
					"company": self.company,
					"currency": self.currency,
					"deposit": flt(line.deposit),
					"withdrawal": flt(line.withdrawal),
					"description": line.particulars,
					"reference_number": line.reference,
					"party_type": party_type,
					"party": party,
				}
			)
			bt.insert()
			bt.submit()
			line.bank_transaction = bt.name
			created += 1
		self.save()
		return {"created": created, "linked": linked}

	def _line_party(self, line):
		parties = {(r.party_type, r.party) for r in self.line_rows(line) if r.party}
		if len(parties) == 1:
			return parties.pop()
		if not parties and line.line_status in (SUGGESTED, DRAFT) and line.party_type and line.party:
			return line.party_type, line.party
		return None, None

	def _remember_rule(self, line, advance=False):
		key = counterparty_key(line.counterparty)
		# A bulk upload's "counterparty" is the bank's batch, not a party - never learn it.
		if not key or not (line.party or line.account) or _is_bulk(line):
			return
		direction = "Withdrawal" if flt(line.withdrawal) else "Deposit"
		values = {
			"suggested_doctype": line.suggested_doctype,
			"party_type": line.party_type if line.party else None,
			"party": line.party,
			"account": line.account if line.suggested_doctype == "Journal Entry" else None,
			"mode_of_payment": line.mode_of_payment,
		}
		# Only an explicit "remember as advance" sets the flag; ordinary drafts leave it as is.
		if advance and line.suggested_doctype == "Payment Entry":
			values["treat_as_advance"] = 1
		existing = frappe.db.get_value("Bank Narration Rule", {"rule_key": f"{direction}|{key}"})
		if existing:
			frappe.db.set_value("Bank Narration Rule", existing, values)
		else:
			frappe.get_doc(
				{"doctype": "Bank Narration Rule", "direction": direction, "counterparty_key": key, **values}
			).insert()


BULK_PLACEHOLDER = {
	"suggested_doctype": None,
	"party_type": None,
	"party": None,
	"account": None,
	"against_doctype": None,
	"against_name": None,
	"split_plan": None,
	"tds_amount": 0,
	"tds_account": None,
	"confidence": None,
	"suggestion_source": "Bulk payment - upload the bank's bulk payment file to see who was paid",
}


def _is_bulk(line) -> bool:
	return line.channel == "Bulk Upload" or bool(
		flt(line.withdrawal) and _bulk_count(line.particulars) and "/AW" in (line.particulars or "").upper()
	)


def _bulk_count(particulars) -> int | None:
	"""NEFT/<batch ref>/<beneficiary count>/AW... -> the count."""
	parts = (particulars or "").split("/")
	return cint(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() else None


def _submit_job_id(review_name) -> str:
	return f"bank-statement-review-submit::{review_name}"


def submit_drafts_job(review_name, vouchers, user=None):
	"""Background job: submit each queued draft on its own, committing after each, and
	report progress / the outcome to the user who queued it."""
	submitted, failed = [], []
	for index, (voucher_type, voucher_no) in enumerate(vouchers, start=1):
		frappe.db.savepoint("bsr_submit")
		try:
			voucher = frappe.get_doc(voucher_type, voucher_no)
			if voucher.docstatus == 0:
				voucher.submit()  # voucher_sync updates the review's line
				submitted.append(voucher_no)
			frappe.db.release_savepoint("bsr_submit")
			frappe.db.commit()
		except Exception as e:
			# Undo only this document; the ones already submitted stay committed.
			frappe.db.rollback(save_point="bsr_submit")
			message = frappe.utils.strip_html(str(e)) or type(e).__name__
			failed.append(f"{voucher_no}: {message}")
			frappe.log_error(title=f"Bank Statement Review {review_name}: could not submit {voucher_no}")
		frappe.clear_messages()
		frappe.publish_realtime(
			"bank_statement_review_submit_progress",
			{"review": review_name, "done": index, "total": len(vouchers)},
			user=user,
		)
	if failed:
		# Also on the review itself, for whoever isn't watching the page.
		frappe.get_doc("Bank Statement Review", review_name).add_comment(
			"Comment",
			_("Background submission: {0} submitted, {1} failed:<br>{2}").format(
				len(submitted), len(failed), "<br>".join(frappe.utils.escape_html(f) for f in failed)
			),
		)
		frappe.db.commit()
	frappe.publish_realtime(
		"bank_statement_review_submit_done",
		{"review": review_name, "submitted": submitted, "failed": failed},
		user=user,
	)


def _line_amount(line) -> float:
	return flt(line.withdrawal or line.deposit, 2)


def _delete_voucher(voucher_type, voucher_no):
	frappe.flags[SKIP_SYNC_FLAG] = True
	try:
		frappe.delete_doc(voucher_type, voucher_no)
	finally:
		frappe.flags[SKIP_SYNC_FLAG] = False


# The voucher field holding the bank / cheque reference ("Cheque/Reference No" on a
# Payment Entry, "Reference Number" on a Journal Entry).
REFERENCE_FIELD = {"Payment Entry": "reference_no", "Journal Entry": "cheque_no"}


def _voucher_info(doctype, name, gl_account) -> dict:
	fields = ["docstatus", "posting_date", REFERENCE_FIELD[doctype]] + (
		["party_type", "party"] if doctype == "Payment Entry" else []
	)
	doc = frappe.db.get_value(doctype, name, fields, as_dict=True) or frappe._dict()
	settles = []
	if doctype == "Payment Entry":
		settles = frappe.get_all(
			"Payment Entry Reference", filters={"parent": name}, pluck="reference_name", order_by="idx"
		)
	else:
		for row in frappe.get_all(
			"Journal Entry Account",
			filters={"parent": name, "account": ["!=", gl_account]},
			fields=["account", "party", "reference_name"],
			order_by="idx",
		):
			settles.append(row.reference_name or row.party or row.account)
	return {
		"party_type": doc.get("party_type"),
		"party": doc.get("party"),
		"posting_date": doc.get("posting_date"),
		"reference_no": doc.get(REFERENCE_FIELD[doctype]),
		"voucher_status": DOCSTATUS_LABEL.get(doc.get("docstatus"), "Draft"),
		"amount": bank_amount_of(doctype, name, gl_account),
		"settles": ", ".join(s for s in settles if s)[:500] or None,
	}


def _line_key(transaction_date, withdrawal, deposit, particulars):
	return (
		str(getdate(transaction_date)),
		flt(withdrawal, 2),
		flt(deposit, 2),
		" ".join((particulars or "").split()).upper(),
	)


def _as_local_doc(doc) -> dict:
	"""Serialise an unsaved document the way Desk expects a new local doc, so the
	browser can `frappe.model.sync` it and open it as an unsaved form."""
	data = doc.as_dict(no_default_fields=False)
	data["name"] = f"new-{frappe.scrub(doc.doctype).replace('_', '-')}-{frappe.generate_hash(length=8)}"
	data["__islocal"] = 1
	data["__unsaved"] = 1
	data["docstatus"] = 0
	for table in doc.meta.get_table_fields():
		for index, row in enumerate(data.get(table.fieldname) or [], start=1):
			row["name"] = (
				f"new-{frappe.scrub(row['doctype']).replace('_', '-')}-{frappe.generate_hash(length=8)}"
			)
			row["parent"] = data["name"]
			row["parenttype"] = doc.doctype
			row["parentfield"] = table.fieldname
			row["idx"] = index
			row["__islocal"] = 1
			row["docstatus"] = 0
	return data
