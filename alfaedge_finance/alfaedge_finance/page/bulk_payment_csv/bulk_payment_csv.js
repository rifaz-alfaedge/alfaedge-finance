// Copyright (c) 2026, alfaEdge
// Page: Bulk Payment CSV

frappe.pages['bulk-payment-csv'].on_page_load = function (wrapper) {
	let page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'Bulk Payment CSV',
		single_column: true,
	});

	new BulkPaymentCSV(page);
};

class BulkPaymentCSV {
	constructor(page) {
		this.page = page;
		this.rows = [];

		// column order/labels must match the bank's template EXACTLY
		this.columns = [
			{ label: "Debit Account Number (Mandatory)", fieldname: "debit_account_no" },
			{ label: "Transaction Amount (Mandatory)", fieldname: "transaction_amount" },
			{ label: "Beneficiary Name (Mandatory)", fieldname: "beneficiary_name" },
			{ label: "Beneficiary Account Number (Mandatory)", fieldname: "beneficiary_account_no" },
			{ label: "Beneficiary IFSC Code (Mandatory)", fieldname: "beneficiary_ifsc" },
			{ label: "Transaction Date (Mandatory)", fieldname: "transaction_date" },
			{ label: "Payment Mode (Mandatory)", fieldname: "payment_mode" },
			{ label: "Customer Reference Number (Mandatory)", fieldname: "customer_ref_no" },
			{ label: "Beneficiary Nickname/Code (Mandatory)", fieldname: "beneficiary_nickname" },
		];

		this.make_filters();
		this.make_table_area();
	}

	make_filters() {
		this.company_field = this.page.add_field({
			fieldname: 'company',
			label: 'Company',
			fieldtype: 'Link',
			options: 'Company',
			default: frappe.defaults.get_user_default('Company'),
			reqd: 1,
			change: () => this.load_payroll_entries(),
		});

		this.txn_type_field = this.page.add_field({
			fieldname: 'txn_type',
			label: 'Type',
			fieldtype: 'Select',
			options: ['Salary', 'Expense Claims', 'Purchase Invoices'],
			default: 'Expense Claims',
			reqd: 1,
			change: () => this.toggle_payroll_entry(),
		});

		this.transaction_date_field = this.page.add_field({
			fieldname: 'transaction_date',
			label: 'Transaction Date',
			fieldtype: 'Date',
			default: frappe.datetime.get_today(),
			reqd: 1,
		});

		this.page.set_primary_action('Generate', () => this.generate(), 'refresh');

		this.make_payroll_entry_selector();
		this.toggle_payroll_entry();
		this.load_payroll_entries();
	}

	// Built as a plain element (not page.add_field) so we don't depend on
	// internal Frappe control-wrapper property names for the HTML fieldtype.
	// Appended into page.page_form (the same row container the other three
	// fields live in) so it sits on the same line; falls back to page.body
	// if page_form isn't available in this Frappe version.
	// Clones the real "Type" Select field's markup so styling matches exactly
	// (guaranteed pixel-identical, rather than guessing Frappe's internal CSS
	// classes). Falls back to a manually built version if cloning fails.
	make_payroll_entry_selector() {
		let $clone = null;
		try {
			if (this.txn_type_field && this.txn_type_field.wrapper) {
				let $source = $(this.txn_type_field.wrapper).closest('[class*="col-"]');
				if ($source.length) {
					$clone = $source.clone();
					$clone.find('label, .control-label').remove();
					let $select = $clone.find('select');
					if ($select.length) {
						$select.attr('id', 'payroll-entry-select').empty().append('<option value="">Payroll Entry...</option>');
					} else {
						$clone = null;
					}
				}
			}
		} catch (e) {
			$clone = null;
		}

		this.payroll_entry_wrapper = $clone && $clone.length ? $clone : $(`
			<div class="col-sm-2">
				<div class="frappe-control" data-fieldtype="Select">
					<div class="control-input-wrapper">
						<div class="control-input">
							<select class="input-with-feedback form-control ellipsis" id="payroll-entry-select">
								<option value="">Payroll Entry...</option>
							</select>
						</div>
					</div>
				</div>
			</div>
		`);

		let target = (this.page.page_form && this.page.page_form.length) ? this.page.page_form : this.page.body;
		this.payroll_entry_wrapper.appendTo(target);

		this.$payroll_entry_select = this.payroll_entry_wrapper.find('#payroll-entry-select');
	}

	load_payroll_entries() {
		let company = this.company_field.get_value();
		if (!company) {
			this.$payroll_entry_select.html('<option value="">Select a Company first...</option>');
			return;
		}
		frappe.call({
			method: 'alfaedge_finance.alfaedge_finance.page.bulk_payment_csv.bulk_payment_csv.get_payroll_entries',
			args: { company },
			callback: (r) => {
				let entries = r.message || [];
				let options = ['<option value="">Payroll Entry...</option>']
					.concat(
						entries.map(
							(e) => `<option value="${frappe.utils.escape_html(e.name)}">${frappe.utils.escape_html(e.label)}</option>`
						)
					)
					.join('');
				this.$payroll_entry_select.html(options);
			},
		});
	}

	toggle_payroll_entry() {
		let show = this.txn_type_field.get_value() === 'Salary';
		this.payroll_entry_wrapper.toggle(show);
	}

	make_table_area() {
		this.table_wrapper = $(
			`<div class="bulk-payment-table-wrapper" style="margin-top: 20px; overflow-x: auto;"></div>`
		).appendTo(this.page.body);

		this.unmatched_wrapper = $(
			`<div class="bulk-payment-unmatched-wrapper" style="margin-top: 25px; overflow-x: auto;"></div>`
		).appendTo(this.page.body);
	}

	generate() {
		// Guard against the Date control returning a native JS Date object:
		// JSON-serializing a Date converts it to UTC, which rolls back to the
		// previous day for timezones ahead of UTC (like IST) — explicitly
		// normalize to a plain "YYYY-MM-DD" string to avoid that.
		let raw_date = this.transaction_date_field.get_value();
		let transaction_date = (raw_date instanceof Date) ? frappe.datetime.obj_to_str(raw_date) : raw_date;

		let filters = {
			company: this.company_field.get_value(),
			txn_type: this.txn_type_field.get_value(),
			transaction_date: transaction_date,
			payroll_entry: this.$payroll_entry_select.val(),
		};

		if (!filters.company || !filters.txn_type || !filters.transaction_date) {
			frappe.msgprint('Please fill Company, Type and Transaction Date');
			return;
		}
		if (filters.txn_type === 'Salary' && !filters.payroll_entry) {
			frappe.msgprint('Please select a Payroll Entry');
			return;
		}

		frappe.call({
			method: 'alfaedge_finance.alfaedge_finance.page.bulk_payment_csv.bulk_payment_csv.get_rows',
			args: { filters },
			freeze: true,
			freeze_message: 'Fetching records...',
			callback: (r) => {
				let all_rows = r.message || [];
				this.rows = all_rows.filter((row) => row.matched);
				this.unmatched_rows = all_rows.filter((row) => !row.matched);
				this.render_table();
			},
		});
	}

	render_table() {
		this.table_wrapper.empty();
		this.render_unmatched_table();

		if (!this.rows.length) {
			this.table_wrapper.html(
				'<div class="text-muted" style="padding: 20px;">No matched records found.</div>'
			);
			this.page.clear_secondary_action();
			return;
		}

		let header = this.columns.map((c) => `<th>${frappe.utils.escape_html(c.label)}</th>`).join('');
		let body = this.rows
			.map((row, i) => {
				let cells = this.columns
					.map((c) => {
						let val = row[c.fieldname] != null ? row[c.fieldname] : '';
						return `<td><input type="text" class="form-control input-sm bp-cell"
							data-row="${i}" data-field="${c.fieldname}"
							value="${frappe.utils.escape_html(String(val))}"></td>`;
					})
					.join('');
				return `<tr data-row="${i}">
					<td class="text-center"><button class="btn btn-xs btn-danger bp-remove-row" data-row="${i}" title="Remove row">&times;</button></td>
					${cells}
				</tr>`;
			})
			.join('');

		let $table = $(`
			<table class="table table-bordered table-sm" style="background:#fff; white-space: nowrap;">
				<thead><tr><th></th>${header}</tr></thead>
				<tbody>${body}</tbody>
			</table>
		`);
		this.table_wrapper.append($table);

		// keep this.rows in sync as the user edits cells
		$table.on('input change', '.bp-cell', (e) => {
			let $el = $(e.currentTarget);
			let i = $el.data('row');
			let field = $el.data('field');
			this.rows[i][field] = $el.val();
		});

		// remove a row and re-render (re-render keeps data-row indexes correct)
		$table.on('click', '.bp-remove-row', (e) => {
			let i = $(e.currentTarget).data('row');
			this.rows.splice(i, 1);
			this.render_table();
		});

		this.page.set_secondary_action('Download Excel', () => this.download_xlsx(), 'download');
	}

	render_advances_table() {
		let adjusted = (this.rows || []).filter((row) => row.advance_deducted);
		if (!adjusted.length) return;

		let fmt = (value) => frappe.utils.escape_html(format_number(value, null, 2));
		let rows_html = adjusted
			.map(
				(row) => `<tr>
					<td>${frappe.utils.escape_html(row.party_label || '')}</td>
					<td class="text-right">${fmt(row.invoices_due)}</td>
					<td class="text-right">${fmt(row.advance_deducted)}</td>
					<td class="text-right">${fmt(row.invoices_due - row.advance_deducted)}</td>
				</tr>`
			)
			.join('');
		this.unmatched_wrapper.append(`
			<div style="font-weight: 600; margin-bottom: 8px;">
				Open advances deducted (${adjusted.length}) — edit the amount above if an advance is meant for a later invoice
			</div>
			<table class="table table-bordered table-sm" style="background:#fafafa; margin-bottom: 20px;">
				<thead><tr><th>Supplier</th><th class="text-right">Invoices due</th><th class="text-right">Open advance</th><th class="text-right">Amount in file</th></tr></thead>
				<tbody>${rows_html}</tbody>
			</table>
		`);
	}

	render_unmatched_table() {
		this.unmatched_wrapper.empty();
		this.render_advances_table();

		if (!this.unmatched_rows || !this.unmatched_rows.length) {
			return;
		}

		this.unmatched_wrapper.append(
			`<div style="font-weight: 600; margin-bottom: 8px; color: #d13438;">
				Not included (${this.unmatched_rows.length}) — excluded from the CSV export
			</div>`
		);

		let rows_html = this.unmatched_rows
			.map(
				(row) => `<tr>
					<td>${frappe.utils.escape_html(row.party_label || '')}</td>
					<td>${frappe.utils.escape_html(String(row.transaction_amount != null ? row.transaction_amount : ''))}</td>
					<td>${frappe.utils.escape_html(row.reason || '')}</td>
				</tr>`
			)
			.join('');

		let $table = $(`
			<table class="table table-bordered table-sm" style="background:#fafafa;">
				<thead><tr><th>Employee / Supplier</th><th>Amount</th><th>Reason</th></tr></thead>
				<tbody>${rows_html}</tbody>
			</table>
		`);
		this.unmatched_wrapper.append($table);
	}

	download_xlsx() {
		if (!this.rows || !this.rows.length) {
			frappe.msgprint('No rows to export.');
			return;
		}
		// Built on the server from the rows as edited here - see download_xlsx in the .py.
		open_url_post(
			'/api/method/alfaedge_finance.alfaedge_finance.page.bulk_payment_csv.bulk_payment_csv.download_xlsx',
			{ rows: JSON.stringify(this.rows), txn_type: this.txn_type_field.get_value() }
		);
	}
}
