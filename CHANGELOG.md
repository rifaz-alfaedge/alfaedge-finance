# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- An invoice's "Bill To" section printing our own company's GSTIN got extracted as the
  *supplier's* GSTIN instead (confirmed live on two records: a genuinely GST-less
  overseas supplier ended up with `supplier_gst` set to our own company's GSTIN). The
  extraction prompt now names our own company and GSTIN explicitly and tells the model
  to exclude them; a hard code-level check also drops any extracted `gst_number` that
  exactly matches our own `Company.gstin`, regardless of what the model returns - our
  own company can never be its own supplier.
- **Create Invoice ignored the reviewer's supplier.** When the extracted GSTIN matched
  some Supplier, that Supplier was used even if the reviewer had picked a different
  Existing Supplier on the Purchase Expense Center. The reviewer's choice now wins.
- **Cancelled and amended invoices.** A Purchase Expense Center kept pointing at a
  cancelled Purchase Invoice after it was amended (seen live: PEC-2026-00015 →
  cancelled PINV-26-00172, while PINV-26-00172-1 was the live invoice). An amendment
  now takes over the link. A cancelled invoice shows the new "Invoice Cancelled"
  status, and Create Invoice is offered again. Deleting an old cancelled invoice no
  longer unlinks its replacement. A patch fixes existing records.
- **Bulk Payment CSV:**
  - USD invoices' outstanding was added to rupee totals, so $150 was exported as
    ₹150. Foreign-currency invoices are now listed separately with the reason
    "pay separately".
  - Amounts are rounded to 2 decimals, so no float noise reaches the bank file.
  - Expense Claims use HRMS's outstanding (grand total less advances and
    reimbursements), approved claims only.
  - Invoices on hold are skipped.
- **Permissions.** Several whitelisted methods were callable by any logged-in user:
  - Create Invoice and Retry Extraction (these create Purchase Invoices and
    Suppliers with elevated rights)
  - PDF upload
  - Bulk Map Items (raw SQL update)
  - Bulk Payment CSV (salaries and bank account numbers)

  They now check the same permissions/roles as the documents and page they serve.
  Bulk Map Items also checks the Item exists, because its SQL update skips Link
  validation.
- **Email webhook.** The secret is compared in constant time, and attachments that
  aren't real PDFs are skipped.
- **Extraction.** An unparseable invoice date, or amounts with thousands separators
  ("1,000.00"), failed the whole extraction. The date is now left blank for the
  reviewer, and the amounts are parsed. Duplicate detection also checks Purchase
  Invoices entered directly in ERPNext (same supplier + bill no). Retry uses the
  intake PDF rather than any file attached later.
- **Tax Account Mapping is learned once.** Saving a Purchase Expense Center with a
  different account for CGST/SGST/IGST (e.g. reverse charge) used to change the
  default for every future invoice without saying so. Now only the first mapping of
  each tax type is learned. Change the default in Tax Account Mapping itself.
- **Supplier TDS category needs confirmation.** Picking a Tax Withholding Category on
  a Purchase Expense Center used to write it onto the Supplier on every save, with no
  entry in its history. The form now asks "also save on the Supplier for future
  invoices?", and a yes saves it through the Supplier document, so it shows in the
  history.
- **Bulk Payment CSV:**
  - Invoices due *on* the transaction date are included; before, they slipped to the
    next run.
  - A supplier's open advances (unallocated Payment Entries and advance Journal
    Entry rows) are deducted from their total. The page shows an "Open advances
    deducted" table, and a supplier fully covered by an advance is left out with that
    reason.
  - The Excel file is now built on the server (openpyxl) from the rows as edited, so
    the page no longer loads a spreadsheet library from an outside CDN.
- **Email webhook limits:** at most 20 attachments per email, 20 MB per attachment
  (larger ones are skipped and logged), and 60 requests an hour per sender IP.

### Added
- **Bank Statement Review**, a staging step before ERPNext's own Bank Reconciliation
  Tool. The native tool is left untouched.
  - **Upload** the bank's raw `.xlsx`/`.xls` statement with **Upload Bank Statement**.
    Any period works (daily, weekly, monthly or custom). The account and dates come from
    the file, and running and closing balances are validated. Axis Bank's export is
    supported.
  - **Auto-pick** existing submitted Payment Entries and Journal Entries. Matching is on
    the reference extracted from the narration plus the exact amount, then on amount +
    date. Ambiguous cases are flagged "Check" with their alternatives. A voucher is never
    matched twice.
  - **Suggest** the record to create for every line that isn't booked yet: Payment Entry
    or Journal Entry; the Supplier, Customer or Employee, or the ledger account; and the
    invoice or expense claim it settles.
    - Suggestions come from Bank Narration Rules, past reconciled transactions, a unique
      party-name prefix match, or an open document with that exact outstanding amount.
    - Each is pre-filled and editable, and created as a **Draft** (never submitted), or
      opened unsaved in its form.
  - **Re-run Reconciliation** on the same file after creating records.
  - **Send to Bank Reconciliation** creates the native Bank Transactions. Overlapping
    uploads are detected, so a line is never imported twice.
  - New **Bank Narration Rule** DocType. Rules are learned automatically from created
    drafts, or in bulk with **Learn from History**.
  - **One bank line, several documents.** Every linked or created document is a row in
    a new `vouchers` table, and lines can be Partly Booked.
    - Matching books a line from several vouchers that share its reference (a bulk
      transfer), and proposes a unique exact-sum combination as a Check.
    - Suggestions can be a split plan across several open invoices and parties.
    - **Split this payment** creates one Draft Payment Entry per party.
  - **Advances.**
    - Mark one or many lines as an advance: a Payment Entry with no invoice, or against
      an open order, with the proforma reference in its remarks.
    - Payees can be remembered as "always an advance".
    - A known party with no open invoice is suggested as an advance automatically.
    - Advances show a badge and have their own filter.
    - Purchase Invoices created by the automation now pull in the supplier's open
      advances (ERPNext's "Set Advances and Allocate (FIFO)").
  - **Better matching and automation.**
    - The party is found from the narration's first word, e.g. `OVH` → Ovhtech R&d.
    - When several documents have the same amount, the one posted closest to the bank
      date wins.
    - Lines with no match still get the party type and mode of payment filled in.
    - Confidence is stricter: High means a known party plus an exact document.
    - New per-review switches (on by default) create drafts for Medium lines and create
      and submit (in the background) High lines, on upload and on Re-run.
  - **Orders as well as invoices.** Payments can settle an open Purchase Order or Sales
    Order as an advance. This works in suggestions (exact outstanding), in Review and
    split rows, and in bulk-file rows (Purchase Orders).
  - **Foreign-currency invoices paid in INR** (e.g. USD SaaS invoices paid by card) are
    matched by the supplier/customer being named in the narration. The amount is only a
    sanity check against the market rate. The Payment Entry pays the bank's INR against
    the invoice's foreign amount, with the difference booked to Exchange Gain/Loss.
  - **Customer TDS detection.** A receipt short of an unpaid Sales Invoice, or of an open
    Sales Order with no advance yet, by 2%, 5% or 10% of its net total is suggested
    against that document. The difference is booked as
    a TDS deduction on the Payment Entry, to the account past receipts used.
  - **Bulk payments.** A bulk bank debit (`NEFT/<batch>/<count>/AW...`) takes an upload
    of the bank's bulk-payment file (the one the Bulk Payment CSV page generates).
    - Each beneficiary is identified from its bank account number via the
      Employee/Supplier Bank Accounts.
    - Each row is matched to the Salary Slip, Expense Claims or Purchase Invoices that
      make up its amount exactly.
    - It becomes **one Draft Journal Entry**: bank credit, plus a debit row per
      document, or on-account where no documents fit.
    - Rows with an unknown account are flagged and must be fixed first.
    - Only documents up to the file's transaction date count. When none add up
      exactly, they're allocated oldest first and any remainder is booked on account.
  - **Documents table** under the lines, listing every created or matched document with
    its live status. Each can be opened, or deleted if it's a draft.
  - **Drafts from the review.** Each draft can be viewed, submitted (after a
    confirmation) or deleted from its line or the Documents table.
    The Documents table shows each document's Cheque/Reference No, editable on drafts.
    Drafts can also be ticked, individually or all at once, and submitted in bulk as a
    background job, with live progress and a list of any that failed.
  - **Deleting drafts.** Drafts can be deleted from the review. Deleting one from its own
    form is no longer blocked by the review's link. Submitting, cancelling or deleting a
    linked entry updates its line immediately.
  - **Deletion lock.** A review can't be deleted once it has been sent to Bank
    Reconciliation. Before that, deleting it also deletes the drafts it created.
- **Multi-currency support.** `Purchase Expense Center` gets a `currency` field, set
  from extraction (falling back to the Company's default currency). Invoice creation
  sets `Purchase Invoice.currency` (the Supplier's own `default_currency` takes
  priority when set - fixes `"Accounting Entry for Supplier: X can only be made in
  currency: Y"`) and fetches a real `conversion_rate` via ERPNext's own exchange-rate
  utility, blocking with a clear message if none can be found rather than posting at a
  wrong/zero rate. Item rows no longer force `base_rate`/`base_amount` to mirror
  `rate`/`amount`, letting ERPNext derive them correctly from the conversion rate.
  Confirmed live end-to-end for a USD Anthropic invoice. Still requires the Supplier's
  Payable account to itself be set up in that currency (a Chart of Accounts / Supplier
  setup task, not something this app creates automatically).
- A brand-new overseas supplier (created automatically because GST/name/address
  matching found nothing) got no `default_currency` or Payable account at all, so its
  first invoice always fell back to the Company's default (INR) Payable account and
  failed the same way even though the invoice itself was correctly extracted in a
  foreign currency (confirmed live: a new "Exa Labs Inc." USD supplier). New-supplier
  creation now sets `default_currency` and reuses an existing, unambiguous Payable
  account in that currency if one exists on the Company (`find_payable_account_for_currency`)
  - never creates a new ledger account itself, so a first-of-its-kind currency still
  needs that one-time Chart of Accounts setup, but every Supplier after that in the
  same currency picks it up automatically.
- Supplier matching now falls back to an exact, case/whitespace-insensitive match on
  `Supplier.supplier_name` when GSTIN-based matching finds nothing - covers overseas
  suppliers (no GSTIN at all) and cases where the LLM mislabels some other identifier
  as `gst_number`. A structural GSTIN-shape check (`looks_like_gstin`) now rejects
  anything that isn't actually a 15-character Indian GSTIN before it's stored or
  matched against (confirmed live: an overseas invoice produced `9924USA29003OSI` as
  `gst_number`, which isn't a GSTIN). New-supplier creation also now captures and uses
  the extracted `country` instead of always defaulting the Address to India.
- A further supplier-matching fallback, `resolve_by_name_and_address`, for when even
  exact name matching finds nothing: normalizes the name further (punctuation
  stripped too), and if that's ambiguous or empty, confirms/searches using the
  Supplier's linked Address (country, state, pincode). Each tier still requires an
  exact match on some normalized field - not fuzzy string similarity - to keep the
  false-positive risk of linking to the wrong Supplier low.
- **Alfaedge Finance workspace** in the Desk sidebar, with shortcuts and link cards to
  `Purchase Expense Center`, `Purchase Item Mapping`, `Tax Account Mapping`, and
  `Purchase Invoice Automation Settings` - so mappings can be viewed and managed
  directly, without going through a `Purchase Expense Center`.
- Item/tax mapping matching is now normalized (whitespace collapsed, lowercased)
  before being used as the lookup/storage key, so trivial LLM re-wording between two
  extractions of the same line item no longer looks unmapped. A one-time patch
  (`patches/v0_2/normalize_purchase_item_mapping_keys.py`) normalizes existing
  `Purchase Item Mapping` rows and merges any that become identical after
  normalization. Genuinely different wording (e.g. a date range or IP address that
  changes every month) still needs mapping once per distinct wording - this is
  whitespace/case normalization only, not fuzzy matching.
- TDS now uses ERPNext's own `Tax Withholding Category` doctype instead of a custom
  one, and invoice creation picks between two mechanisms per supplier:
  - **Native**: if the Supplier has a Tax Withholding Category and isn't flagged
    `Exclude from Automatic TDS` (new Supplier custom field), the Purchase Invoice gets
    `apply_tds = 1` and ERPNext computes and appends the withholding row itself,
    threshold logic included.
  - **Manual override**: for suppliers flagged excluded (e.g. OVHtech R&D, whose
    per-invoice amount never crosses ERPNext's own threshold, so native `apply_tds`
    would silently deduct nothing), TDS is detected from the invoice itself, the
    Supplier's category, or a manual pick, and appended as our own withholding row
    using the `Purchase Expense Center`'s TDS section fields - unchanged from before,
    just now gated on this flag instead of always running.
  - Picking a Tax Withholding Category on a non-excluded supplier's record now updates
    that Supplier's own field to match, so future invoices from them go native without
    needing to pick it again.
  - The grand-total reconciliation check now reads back what ERPNext actually deducted
    for the native path, rather than trusting a pre-computed estimate.

### Fixed
- A cancelled Purchase Invoice could not be deleted because its source Purchase
  Expense Center still linked to it, and vice versa - a mutual link deadlock. Deleting
  the Purchase Invoice now always succeeds and clears the link, resetting the source
  record's `invoice_status` back to `Pending Review` so "Create Invoice" reappears.
  Deleting a Purchase Expense Center while a Purchase Invoice still links to it remains
  blocked, as intended.

## [0.2.0] - 2026-09-16

### Added
- Contributors listed in `pyproject.toml` (`authors`).

### Changed
- Draft Purchase Invoice `posting_date` is now set to the supplier's own invoice date
  (previously left at today's date).
- `Purchase Expense Center.invoice_status` now tracks the actual Purchase Invoice state
  (`Invoice Draft` / `Invoice Submitted`) instead of a one-shot `Invoice Created`, kept
  in sync automatically when the invoice is submitted or cancelled.

## [0.1.0] - 2026-09-16

### Added
- **Purchase Invoice Automation**: turns a supplier-emailed or manually-uploaded PDF
  invoice into a draft Purchase Invoice.
  - `Purchase Expense Center`, `Purchase Expense Center Tax`, `Purchase Item Mapping`,
    `Tax Account Mapping`, `TDS Category`, and `Purchase Invoice Automation Settings`
    DocTypes.
  - Email webhook (`api.purchase_invoice_webhook`) and manual/bulk Desk PDF upload
    (`api.purchase_invoice_upload`), sharing one background ingestion pipeline.
  - Bifrost (LLM gateway) extraction of supplier, GST, line items, and tax breakdown
    from the PDF, running as a background job.
  - GST-based supplier matching, with automatic new-Supplier + Address creation as a
    fallback, and graceful handling of a GSTIN that fails checksum validation.
  - Global, self-learning item and tax account mapping - mapping a row by hand (or via
    the "Bulk Map Items" tool) is remembered for every future invoice.
  - TDS deduction handling: auto-detected when the invoice itself states one, or
    applied via a reviewer-picked `TDS Category`, posted the same way ERPNext's own
    automatic Tax Withholding does.
  - Draft-only Purchase Invoice creation (never auto-submitted), linked back to its
    source `Purchase Expense Center` via a custom field, with the original PDF
    attached to the invoice as well.
  - Duplicate detection, a retry-on-failure button, and a grand-total reconciliation
    warning against the invoice's own printed total.
  - 19 automated tests covering extraction parsing, GST matching, mapping sync, and
    invoice creation.

### Fixed
- Race condition where `frappe.enqueue()` could start a background job before its
  triggering record's insert had committed, on a fast worker (`enqueue_after_commit`).

## [0.0.1] - 2026-09-15

### Added
- Initial app scaffold and the pre-existing Bulk Payment CSV tool.

[Unreleased]: https://github.com/rifaz-alfaedge/alfaedge-finance/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/rifaz-alfaedge/alfaedge-finance/releases/tag/v0.2.0
[0.1.0]: https://github.com/rifaz-alfaedge/alfaedge-finance/releases/tag/v0.1.0
