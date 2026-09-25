### alfaEdge Finance

Finance app for alfaEdge (Code Dynamic Solutions Private Limited). Currently includes
a Bulk Payment CSV tool and Purchase Invoice Automation.

## Purchase Invoice Automation

Turns a supplier-emailed PDF invoice into a draft `Purchase Invoice` with minimal manual
work, while keeping a human in the loop for every review and submission decision.

### Flow

1. A supplier emails a PDF invoice to `purchase@codedynamic.org` (production) or
   `test@codedynamic.org` (dev/testing).
2. A Cloudflare Email Worker (`alfaedge-invoice-email-worker` / `-dev`) parses the MIME
   message and POSTs the PDF + email metadata to the matching site's webhook.
3. The webhook creates a `Purchase Expense Center` record (`status = Pending`) and
   enqueues a background job - it does not wait for extraction, so the Worker gets a
   fast response.
4. The background job calls Bifrost (a self-hosted OpenAI-compatible LLM gateway) to
   extract supplier, line items, and tax breakdown from the PDF, matches the supplier
   (by GSTIN first, falling back to an exact supplier-name match when there's no usable
   GSTIN - see [Supplier matching](#supplier-matching) below), and pre-fills any
   item/tax mappings already known from prior invoices. `status` becomes `Extracted`
   (or `Failed`, with a reason, if anything goes wrong).
5. A reviewer opens the record, maps any remaining line items to internal `Item`s and
   any remaining tax rows to ledger `Account`s (once per unique description/tax type -
   reused automatically on future invoices), then clicks **Create Invoice**.
6. A **Draft** `Purchase Invoice` is created, dated to the supplier's own invoice date
   (`posting_date` = `supplier_invoice_date`). This app never submits an invoice - that
   remains a manual step for the reviewer. `invoice_status` on the source record tracks
   the invoice's actual state (`Invoice Draft` / `Invoice Submitted`), kept in sync
   automatically whenever the Purchase Invoice is submitted or cancelled.

PDFs can also be ingested without email, directly from Desk: a **"New from PDF"** button
on the `Purchase Expense Center` list view accepts one or more PDFs at once (drag-and-
drop is not required - it's a plain file picker) and feeds the exact same pipeline.

### Supplier matching

1. **By GSTIN** (`resolve_by_gst`): exact match against `Supplier.gstin`. The extracted
   `gst_number` goes through two checks before it's trusted, since it's not a
   GSTIN-shaped string. either way, anything that isn't GSTIN-shaped is treated as
   absent rather than stored or matched against:
   - **Shape** (`looks_like_gstin`): must actually be a 15-character Indian GSTIN - an
     overseas supplier has none at all, and the LLM has been caught mislabeling some
     other identifier (an EIN, in one real case) as `gst_number` despite being told not
     to.
   - **Not our own**: some invoices print *our own* company's GSTIN in a "Bill To"
     section, and it's been extracted as the supplier's GSTIN instead of the actual
     supplier's (or absence thereof). If the extracted `gst_number` exactly matches our
     own `Company.gstin`, it's dropped - our own company can never be its own supplier,
     so this is unambiguous regardless of what the extraction prompt says. The prompt
     also now names our own company and GSTIN explicitly, telling the model to exclude
     them, as a first line of defense - this check is the backstop for when that doesn't
     work.
2. **By exact name** (`resolve_by_name`), when GSTIN-based matching finds nothing: an
   exact, case/whitespace-insensitive match against `Supplier.supplier_name`. This is
   what catches an already-known overseas supplier (no GSTIN to match on at all) or a
   domestic one whose GST number the LLM got wrong.
3. **By normalized name + address** (`resolve_by_name_and_address`), when even that
   finds nothing: the name is normalized further (punctuation stripped too, so
   `"Anthropic, PBC"` and `"Anthropic PBC"` are the same) and matched against every
   Supplier's own normalized name.
   - A unique normalized-name match is trusted on its own.
   - If more than one Supplier normalizes to the same name, only one whose linked
     Address also agrees (exact pincode, or matching country+state) is trusted.
   - If *no* Supplier's name matches even loosely, an exact pincode match is still
     tried, but only for a Supplier whose name at least partially overlaps
     (one normalized name contains the other) - a bare pincode match against a
     completely unrelated name is never trusted (could be a different company at the
     same address).
   - Country/state/pincode come from `address`/`city`/`state`/`postal_code`/`country`
     in the extraction.
4. **New supplier**: if none of the above match, the record is flagged `New` for manual
   review, with `address`/`city`/`state`/`postal_code`/`country` filled from extraction
   (`country` defaults to India if extraction doesn't return one). All three checks
   re-run at invoice-creation time too (`_resolve_supplier`), in case the real Supplier
   was created or corrected in the meantime.

None of these tiers are fuzzy string-similarity matching - each requires an exact match
on some normalized/structured field (name, pincode, country+state), deliberately, to
keep the false-positive risk of linking to the wrong Supplier low. A near-miss that
doesn't satisfy any tier is routed to manual review as `New` rather than guessed at.

### DocTypes

| DocType | Purpose |
|---|---|
| `Purchase Expense Center` | The staging/review record for one inbound invoice - extraction status, supplier match, line items (`Purchase Invoice Item`, reused from ERPNext), tax rows, and a link to the `Purchase Invoice` once created. |
| `Purchase Expense Center Tax` | Child table of tax rows (type, rate, amount, mapped ledger account) on a `Purchase Expense Center`. |
| `Purchase Item Mapping` | Global (not per-supplier) map of exact supplier line-item text → internal `Item`. Grows automatically as invoices are reviewed. |
| `Tax Account Mapping` | Global map of tax type (e.g. `CGST`) → ledger `Account`. |
| `Purchase Invoice Automation Settings` | Single. Company, default UOM, default TDS account, Bifrost endpoint/model/key, and the webhook shared secret. |

TDS uses ERPNext's own `Tax Withholding Category` doctype rather than a custom one - see
[TDS deduction](#tds-deduction) below. `Supplier` also gets a custom field,
`exclude_from_auto_tds` (Check), for suppliers that need TDS deducted manually instead
of through ERPNext's own automatic mechanism.

`Purchase Invoice Item` also gets a custom field, `mapped_item` (Link → Item), via the
`custom_field` fixture in `alfaedge_finance/fixtures/`.

### Ingestion endpoints

**Email webhook** (called by the Cloudflare Worker; guest-allowed, secured by a shared
secret):

```
POST /api/method/alfaedge_finance.alfaedge_finance.api.purchase_invoice_webhook.receive_invoice_email
Content-Type: application/json

{
  "email_body": "string",
  "sender_email": "string",
  "subject": "string",
  "webhook_secret": "string",
  "attachments": [
    { "filename": "invoice.pdf", "content_type": "application/pdf", "content": "<base64>" }
  ]
}
```
Returns `{"success": true, "created": ["PEC-2026-00001", ...]}` - one record per PDF
attachment found. This request shape is fixed by the Worker; do not change field names
here without also updating the Worker.

**Manual upload** (authenticated Desk user, no secret required):

```
POST /api/method/alfaedge_finance.alfaedge_finance.api.purchase_invoice_upload.upload_purchase_invoices
Content-Type: application/json

{ "attachments": [ { "filename": "invoice.pdf", "content": "<base64>" } ] }
```
Returns a list of `{filename, name, status}` - one entry per file, `status` is `queued`
or `failed`.

### Configuration

Set these on **Purchase Invoice Automation Settings** (Desk: search "Purchase Invoice
Automation Settings"):

- `company` - the Company automated invoices are created against.
- `default_uom` - UOM used for extracted line items (default `Nos`).
- `bifrost_endpoint` / `bifrost_model` - Bifrost's `/v1/chat/completions` URL and model
  alias.
- `bifrost_virtual_key` - leave blank to fall back to `BIFROST_VIRTUAL_KEY` in
  `site_config.json`.
- `webhook_secret` - leave blank to fall back to `WEBHOOK_SECRET` in
  `site_config.json`. The Cloudflare Worker must be configured with the same value via
  `wrangler secret put WEBHOOK_SECRET`.

**Go-live checklist:** rotate the Bifrost virtual key and webhook secret before
production use if either was ever shared outside of encrypted config (e.g. pasted into
a chat, a script, or committed anywhere) - treat any such exposure as a compromised
credential, not a cosmetic concern.

### Bulk item/tax mapping

The `Purchase Expense Center` list view has a **"Bulk Map Items"** button: it lists
every distinct unmapped line-item description across all records with a per-item Item
picker, and applies all of them at once (a direct SQL update, since `mapped_item` does
not affect invoice totals) while also saving each mapping into `Purchase Item Mapping`
for future invoices. Tax account mapping has no bulk tool - in practice there are only
a handful of distinct tax types (CGST/SGST/IGST/Other), so mapping them individually
during the first few invoice reviews is simpler than building a second bulk dialog.

Either way, mapping is remembered from that point on: saving a `Purchase Expense
Center` with a `mapped_item`/`mapped_account` set on any row - whether it got there via
the bulk dialog, extraction prefill, or just typed in by hand - upserts it into
`Purchase Item Mapping` automatically (`on_update`, see
`purchase_invoice_automation/mapping_sync.py`). `Tax Account Mapping` is only learned
the first time a tax type is mapped: one tax type maps to one account for every future
invoice, so a different account on a single invoice (reverse charge, ineligible ITC)
stays on that invoice. Change the default in `Tax Account Mapping` itself. You never
need to map the same description or tax type twice, and it's permanent regardless of what later happens to
the `Purchase Expense Center` it was mapped on (deleting that record does not remove the
mapping - they're independent doctypes).

Matching is normalized (whitespace collapsed, case-insensitive) before it's used as the
lookup/storage key, so trivial LLM re-wording between two extractions of what is
otherwise the same line item (`"SYS-1 rental"` vs `"  SYS-1   Rental  "`) still matches
the existing mapping (`purchase_invoice_automation/text_normalization.py`). The item's
*displayed* text on the invoice keeps its original casing/spacing - only the mapping key
is normalized. This does not merge two descriptions that are genuinely different text
(e.g. a recurring hosting invoice that appends a different date range or IP address each
month) - those still need mapping once per distinct wording, since we deliberately don't
do fuzzy/similarity matching (a real risk of silently mapping to the wrong Item).

Manage `Purchase Item Mapping` and `Tax Account Mapping` directly, independent of any
`Purchase Expense Center`, from the **Alfaedge Finance** workspace in the Desk sidebar -
standard Frappe list views, so you can add, edit, or delete mapping rows freely.

### Linking back from the Purchase Invoice

The created `Purchase Invoice` gets a read-only `purchase_expense_center` field (a
custom field) pointing back to its source record, and the original PDF is attached to
the Purchase Invoice as well (referencing the same stored file, not a duplicate copy) -
so a reviewer looking at the invoice alone can trace it back to the email/upload and
the original document without going through the Purchase Expense Center first.

### TDS deduction

Two mechanisms exist side by side, and invoice creation picks between them per
supplier - there is no separate custom "TDS Category" list; everything keys off
ERPNext's own `Tax Withholding Category` doctype and the Supplier's own field for it.

**Native (the default, used for most suppliers):** if the resolved Supplier has a
`Tax Withholding Category` set and isn't flagged `Exclude from Automatic TDS`, invoice
creation sets `apply_tds = 1` and `tax_withholding_category` on the Purchase Invoice and
lets ERPNext compute and append the withholding-tax row itself
(`accounts_controller.set_tax_withholding()` - the same thing that happens if a user
checked "Apply Tax Withholding Amount" by hand), including its own threshold logic. We
append nothing ourselves in this case.

**Manual override (for suppliers like OVHtech R&D (India) Private Limited):** some
suppliers should have TDS deducted on every invoice, but their per-invoice amount never
crosses ERPNext's own configured threshold, so native `apply_tds` would silently deduct
nothing (confirmed in testing: a Tax Withholding Category with a real threshold applies
TDS only once the amount clears it). Flag such a Supplier `Exclude from Automatic TDS`
(a custom field) - this app then never writes to that Supplier's own
`tax_withholding_category` and never sets `apply_tds` on invoices for them, and instead
appends the withholding row manually using the `Purchase Expense Center`'s own TDS
section fields: `is_tds_applicable`, `tds_category` (Link → `Tax Withholding Category`,
used only for calculation here, not synced anywhere), `tds_rate`, `tds_account`,
`tds_amount`.

The manual TDS section fills in from, in priority order:
1. **The invoice itself**, if it explicitly states a TDS deduction (e.g. "Less: TDS @
   2%") - extraction fills `tds_rate`/`tds_amount`/`tds_account` (from Settings' Default
   TDS Account) directly; Bifrost is told not to invent a figure that isn't printed.
2. **The Supplier's Tax Withholding Category**, if the invoice doesn't state one -
   resolved the same way ERPNext's own automatic TDS would (currently-effective rate by
   date, this company's configured account), just applied proactively instead of
   waiting on a threshold. Also re-runs client-side whenever a reviewer changes
   `existing_supplier` by hand.
3. **Manual pick**: the reviewer selects a `Tax Withholding Category` directly - this
   auto-fills rate/account and computes `tds_amount` as `extracted_taxable_amount ×
   rate` (on the pre-GST taxable amount, not the GST-inclusive total).

Picking a `tds_category` by hand for an Existing Supplier asks whether to also save it
on the Supplier (unless that Supplier is excluded per above). On yes, it is saved
through the Supplier document, so it shows in the Supplier's history, and that
Supplier's future invoices go through the native path with no further manual
selection. On no, it applies to this invoice only. Invoice creation
blocks if `is_tds_applicable` is checked (manual path) but the account or amount is
missing, same as item/tax mapping. The grand-total reconciliation check accounts for
either path - it reads back whatever ERPNext actually deducted for the native case
(which may be less than expected, or nothing, depending on its own threshold logic)
rather than trusting our own pre-computed estimate.

### Multi-currency

Every field before this assumed the Company's own currency (INR); overseas suppliers
(billed in USD, etc.) need the real thing. `Purchase Expense Center` has a `currency`
field, set from extraction (Bifrost is told to infer it from the invoice, never assume
INR) and falling back to the Company's default currency if extraction doesn't return a
real 3-letter code.

At invoice creation:
- **Currency**: the Supplier's own `default_currency` (if set) takes priority over the
  extracted one - ERPNext hard-requires all of a Supplier's accounting entries to be in
  one currency once that's set, so honouring it is what avoids the
  `"Accounting Entry for Supplier: X can only be made in currency: Y"` error.
- **Conversion rate**: fetched via ERPNext's own `erpnext.setup.utils.get_exchange_rate`
  (checks a manually-recorded `Currency Exchange` rate first, then auto-fetches from the
  configured external provider) for the invoice's posting date. Invoice creation blocks
  with a clear message if no rate can be found, rather than posting at a wrong or zero
  rate.
- Item rows no longer force `base_rate`/`base_amount` to mirror `rate`/`amount` - those
  are left for `calculate_taxes_and_totals()` to derive from `conversion_rate`, which
  only equals rate/amount 1:1 when the invoice currency is the company's own.

**ERPNext also requires the Supplier's Payable account (`credit_to`) to itself be
denominated in that currency** - a separate constraint from the two points above, and
the one that actually blocks invoice creation
(`"Party Account ... currency (INR) and document currency (USD) should be same"`).
Which ledger account to use is a Chart-of-Accounts decision this app never makes on its
own - it will reuse an *existing* one, never create a new one:

- **New supplier, non-Company currency** (`create_supplier_and_address`): sets
  `default_currency` on the new Supplier, then looks for exactly one existing, non-group
  Payable account under the Company already in that currency
  (`find_payable_account_for_currency`) and adds it to the Supplier's own Accounts table
  automatically if found. If none exists yet (first time this currency has come up),
  Supplier creation still succeeds, but invoice creation for them will keep failing on
  the account-currency mismatch until a human creates one - a one-time setup per new
  *currency*, not per supplier, once it exists it's reused automatically for every
  future supplier in that currency.
- **Existing supplier**: unchanged from before - if the Supplier already has both
  `default_currency` and a matching Payable account configured (however that happened),
  invoices from them just work, as confirmed live for Anthropic, PBC.

To do that one-time setup by hand: create a currency-specific Payable account (e.g.
"Creditors USD") under your Payables group, then add it to the Supplier's own
**Accounts** table (`Supplier → Accounting tab → Default Accounts`) for your Company.

## Bank Statement Review

A staging step that runs **before** ERPNext's own Bank Reconciliation Tool, which it
never modifies. ERPNext's tool only reconciles against entries that already exist. For
anything missing it just offers blank "create entry" shortcuts. This step closes that
gap: every statement line either gets its already-booked entry picked automatically, or
gets a suggested, pre-filled record to create.

### Flow

1. **Upload.** Use **Upload Bank Statement** on the `Bank Statement Review` list, and
   pick the bank's own `.xlsx`/`.xls` export as downloaded. Any period works: daily,
   weekly, monthly or custom. The account number and From/To dates are read from the
   file. The bank account is picked by matching that number against
   `Bank Account.bank_account_no`. The statement is rejected if any running balance, or
   opening + credits − debits = closing, doesn't add up.
2. **Auto-pick booked entries** (`bank_statement/matching.py`). Candidates are
   submitted, uncleared Payment Entries and Journal Entries on the bank's GL account,
   in the same direction, within ±7 days (configurable per review). The amount is
   compared in the bank's currency (`received_amount` for a foreign receipt, not its
   USD `paid_amount`) and must match exactly.
   - **Reference + amount → Already Booked.** The reference is extracted from the
     narration (UTR / RRN / branch ref). It's also accepted when one side contains
     the other with at least 8 characters, e.g. `IN4260…` vs `4260…`, or a truncated
     `0081FIR260178`.
   - **Amount + date, one unique candidate → Already Booked.**
   - **Amount + date, but a conflicting reference or several candidates → Check.**
     The reviewer picks from the alternatives, or creates a new record instead.
   - **Several vouchers with this line's reference that add up to it → Already
     Booked.** For example, one bulk NEFT paying several employees, each with their
     own Payment Entry.
   - **A unique combination of 2–4 nearby vouchers that adds up exactly → Check.**
     A coincidental sum is possible, so the reviewer confirms it with "Use these".
   - Vouchers are assigned greedily, and never to two lines.
3. **Suggest what's missing** (`bank_statement/suggestion.py`). The first source that
   hits wins, and the reason is shown on the line.
   1. A **Bank Narration Rule** for this counterparty and direction.
   2. **History.** Past reconciled Bank Transactions from the same counterparty, used
      only when all of them were booked the same way.
   3. A **unique party** (Supplier/Customer/Employee, or a party Bank Account) whose
      name starts with the bank's often-truncated counterparty name, e.g.
      `C LOUNGE BUSINESS CEN` → `C Lounge Business Center LLP`. That needs at least 8
      characters, and the direction breaks ties.
   4. An **open Purchase Invoice / Sales Invoice / Expense Claim** whose outstanding
      amount equals the line exactly. Failing that, an **open Purchase Order / Sales
      Order** (an advance payment), meaning one that's not fully billed, not Closed /
      Completed / On Hold, and whose total less advances paid equals the line. The
      Payment Entry is then built against it. Orders can also be picked by hand in
      Review ("Settles") and in split rows. On a bulk-file row, a supplier with no
      matching invoice can be matched to an open Purchase Order. It's booked as an
      advance JE row (`is_advance`) against the order.
   5. A **split plan**: a unique set of 2–5 open invoices, posted within 60 days before
      the bank date, whose outstanding amounts add up exactly to the line. This covers
      one Amazon card payment for invoices from several sellers, for example.
   6. **Customer TDS.** A customer receipt often arrives short by the TDS the customer
      withheld: 2%, 5% or 10% of the invoice's net total (before GST), rounded to the
      rupee. For example, 10% of ₹29,000 = ₹2,900, so a ₹34,220 invoice is paid as
      ₹31,320. Where no exact document fits, an unpaid Sales Invoice whose outstanding
      minus that TDS equals the deposit is suggested. An open Sales Order with no advance
      yet is checked the same way, for an advance paid net of TDS (the customer's own first, else
      the only customer with one).
      - The Payment Entry settles the whole invoice.
      - It receives the bank amount.
      - It books the TDS as a deduction, to the account used on past receipts
        (`TDS - CDS`).
      - Part-paid invoices are skipped, because TDS is withheld once.
   7. **Foreign-currency parties.** For example, a USD invoice paid by card in INR. Such a
      payment can never match on amount, because the bank's INR differs from the invoice
      amount × rate by the card's markup and fees.
      - An open invoice in another currency is picked when its supplier or customer is
        **named in the narration**. For example, `ECOM PUR/OPENAI/…` → OpenAI OpCo, LLC;
        `ANTHROPIC* CL` → Anthropic, PBC; `APOLLO.IO` → ZenLeads Inc. (dba Apollo.io);
        `LINODE . AKAM` → Akamai.
      - Name matching is exact on significant words and truncations, ignoring "Inc",
        "LLC", "Private" and similar.
      - The amount is only a sanity check. The implied rate must be within −15%/+20% of
        the market rate, and the closest invoice wins.
      - A narration quoting the foreign amount (`… USD 15160.80 …`) is matched on it
        exactly.
      - The Payment Entry pays the bank's INR and allocates the invoice's foreign amount.
        ERPNext books the difference to **Exchange Gain/Loss**, as on past entries.
      - Each such invoice is offered to only one line per review, the earliest.
   8. Otherwise, a Journal Entry with only the bank side filled.
   **Confidence:**
   - **High:** the party (or Journal Entry account) comes from a rule, from history, or
     from the narration (its first word matches the party's, e.g. `OVH` → Ovhtech R&d;
     for `RAZ*Sarvam AI` the merchant after a short gateway prefix is used), plus exactly
     one document with this outstanding.
   - **Medium:** one step is inferred: several same-amount documents (the one posted
     closest to the bank date, on or before it, wins), customer TDS, an exchange rate, a
     split, or a known party with no document (on account).
   - **Low:** a guess. With no match, the line still gets the party type implied by the
     direction and the mode of payment, and the party if it's named in the counterparty
     segment.

   **Automation:** each review has **Auto-submit High confidence** and **Auto-create
   drafts for Medium confidence** (both on by default). On upload and on every Re-run:
   - Medium lines get a Draft entry.
   - High lines get their entry created and **submitted in the background**, through the
     same job as Submit Selected.
   - Low, Check and bulk lines without a file are never touched.
4. **Review.** Each line has a **Review** dialog where every pre-filled field can be
   changed. **Create Draft** makes a *Draft* Payment Entry or Journal Entry, which is
   never submitted. **Open in Form** opens it unsaved instead. There are also options
   to link an existing voucher by hand, or to ignore the line. **Split this payment**
   takes a table of party / invoice / amount rows that must add up to the line, and
   creates **one Draft Payment Entry per party**, each allocated against that party's
   invoices.
   **Create Drafts (High confidence)** does the whole batch at once. Every draft
   created from a line also saves or updates a Bank Narration Rule, so the same
   counterparty is suggested correctly next time.
5. **Re-run Reconciliation**, on the same file, as often as needed. Entries created
   anywhere else become Already Booked. The review also follows its linked documents
   live (`bank_statement/voucher_sync.py`):
   - Submitting a draft books its line straight away.
   - Cancelling or deleting a linked entry puts its line back to Suggested.
   - Drafts can be opened (**View Draft**) or submitted (**Submit Draft**, after a
     confirmation) straight from the line or the Documents table, and deleted from the review
     itself (**Delete Draft(s)** on the line, or
     **Delete** in the Documents table), or from their own form. The link from the
     review never blocks the delete.
6. **Send to Bank Reconciliation** creates the native `Bank Transaction` records, with
   the extracted reference number and the party. It's safe to run more than once: only
   unsent lines go. Then reconcile in ERPNext's Bank Reconciliation Tool as usual.
   **Once anything has been sent, the review can no longer be deleted.** Before that,
   deleting a review also deletes the drafts it created. Submitted entries are never
   touched.

**Bulk payments.** A bank-executed bulk upload shows on the statement as one debit,
e.g. `NEFT/260002358458/6/AW0005273593////` (batch reference / number of
beneficiaries). By default such lines are not guessed at: no rule, history, name or amount/date
matching applies to them, and nothing is learned from them. They are only linked
automatically to an existing entry that carries the bank's exact batch reference.
Otherwise they get an **Upload Bulk File** button. **Review** still lets you book one by
hand, e.g. a single invoice payment that was sent through bulk upload. That manual
choice is kept on re-run. Upload the bank's
bulk-payment file for that debit, e.g. the `.xlsx` the **Bulk Payment CSV** page
generated; `.xls`/`.csv` work too (`bank_statement/bulk_upload.py`). The file must add
up to the line and debit this statement's account.

Each row is then resolved (`bank_statement/bulk_matching.py`):
- **Party:** the beneficiary account number is matched against the Employee/Supplier
  **Bank Accounts** (spaces and Excel's scientific notation ignored).
- **Documents:** the open documents that make up the row amount exactly.
  - Employee: a Salary Slip with that net pay in an unpaid Payroll Entry, or the
    employee's unpaid Expense Claims (all of them, or a unique combination).
  - Supplier: open Purchase Invoices.
- **Cut-off:** only documents posted on or before the file's own **Transaction Date**
  are considered; later ones can't be what the file paid. If no exact set of documents
  fits, the documents are **allocated oldest first**, the last one partly if needed,
  and anything they don't cover goes on-account. That can happen when a claim was
  cancelled after the file was prepared. The row's note says how much went on-account.
- **Row status:**
  - **Matched**: the documents add up exactly.
  - **On-account**: party known but no exact documents. Allowed; it's booked as an
    advance.
  - **Unresolved**: no party.
  - **Mismatch**: reviewer-chosen documents don't add up.
  - Unresolved and Mismatch rows must be fixed in the bulk dialog (party, kind, or
    documents) before creating.

**Create Draft Journal Entry** then builds one Bank Entry, the way these batches were
booked by hand:
- the bank credit
- one debit per Expense Claim on its payable account (party Employee, reference Expense
  Claim)
- one debit per Purchase Invoice on its `credit_to` (reference Purchase Invoice)
- `Payroll Payable` per Payroll Entry for salary
- the party's payable for on-account rows

**Documents table.** Below the lines, every document linked to or created from the
review is listed with its line, type, **Cheque/Reference No**, party, date, amount,
live status and the invoices it settles. The reference can be edited in place (✎) while
the document is a draft. The table opens on **Drafts (not submitted)**; the other
filters are Created, Matched and All. ERPNext doesn't allow it once submitted; that needs cancel and
amend. From there it can be opened, or deleted if it's a draft. Drafts
also have checkboxes, with select-all and a running selected total. **Submit Selected**
queues them for submission in a background job (long queue, one job per review at a
time). Each document is submitted separately, so one failure doesn't stop the rest.
Progress shows live; when the job finishes the page reloads, listing any failures with
ERPNext's reason. One bank
line can be booked by several documents. They all live in the `vouchers` child table
(`Bank Statement Review Voucher`).

**Advances.** Payments made before the invoice exists (against a proforma, or prepaid
usage like Google Cloud):
- **Automatic:** a line whose party is known but has no open invoice is suggested as an
  **Advance**, so it's auto-drafted as one.
- **By hand:** use **Advance** on a line, or tick several lines and use **Mark selected
  as Advance**. The dialog asks for:
  - the party (each line keeps its detected party in bulk)
  - an optional open Purchase or Sales Order
  - an optional proforma number or reference
  - "Always treat payments to this payee as advances", which saves a Bank Narration Rule
    with *Treat as Advance*, so later payments to them are suggested as advances at High
    confidence
- **The entry:** a Payment Entry with no invoice (ERPNext's unallocated amount is the
  advance), or against the chosen order. Employees go to the company's Employee Advance
  account. Remarks read "Advance to <party> - <reference>". Marking a line that already
  has an unsubmitted draft replaces the draft.
- **Where to see them:** advance lines carry an **Advance** badge, and the Documents
  table has an **Advances** filter.
- **Adjustment:** Purchase Invoices created by Purchase Invoice Automation now tick ERPNext's
  **Set Advances and Allocate (FIFO)**, so the supplier's open advances are pulled into
  the draft invoice automatically. For other invoices, use ERPNext's Payment
  Reconciliation tool.

**Overlapping uploads.** Say a daily file is followed later by the monthly one. Any
line already on another review for the same bank account, or already present as a Bank
Transaction, is marked **Already Imported**, linked to where it already is, and never
sent twice.

**Bank Narration Rules** can also be seeded in bulk. **Learn from History** on the
`Bank Narration Rule` list proposes one rule per counterparty that was always booked
the same way.

Only Axis Bank's statement export is parsed today (`bank_statement/parsers/axis.py`).
Another bank means adding a parser module with `detect`/`parse`, and registering it in
`bank_statement/parsers/__init__.py`.

### Running the tests

```bash
bench --site erp.alfaedge.org set-config allow_tests true   # first time only
bench --site erp.alfaedge.org run-tests --app alfaedge_finance
```

Covers: Bifrost markdown-fence stripping, GST- and name-based supplier matching (match /
no match / no GSTIN / GSTIN-shaped garbage rejected), TDS (native `apply_tds` vs. manual
override, Supplier category sync), item/tax mapping normalization, and Purchase Invoice
creation (blocked on missing item/tax mapping, draft-only creation, tax rows, grand-total
tolerance warning, refusing to double-create an invoice, delete/unlink behavior).

### Changelog

See [CHANGELOG.md](CHANGELOG.md) for a version history. Every user-facing change
(feature, fix, or behavior change) gets an entry there - add one as part of the same
change, not as an afterthought.

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app alfaedge_finance
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/alfaedge_finance
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
