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

	render_unmatched_table() {
		this.unmatched_wrapper.empty();

		if (!this.unmatched_rows || !this.unmatched_rows.length) {
			return;
		}

		this.unmatched_wrapper.append(
			`<div style="font-weight: 600; margin-bottom: 8px; color: #d13438;">
				No Bank Account found (${this.unmatched_rows.length}) — excluded from the CSV export
			</div>`
		);

		let rows_html = this.unmatched_rows
			.map(
				(row) => `<tr>
					<td>${frappe.utils.escape_html(row.party_label || '')}</td>
					<td>${frappe.utils.escape_html(String(row.transaction_amount != null ? row.transaction_amount : ''))}</td>
				</tr>`
			)
			.join('');

		let $table = $(`
			<table class="table table-bordered table-sm" style="background:#fafafa;">
				<thead><tr><th>Employee / Supplier</th><th>Amount</th></tr></thead>
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
		this.ensure_xlsx_lib(() => this.build_and_download_xlsx());
	}

	// SheetJS (the "xlsx" library) isn't guaranteed to be loaded on every
	// Frappe page, so we load it on demand from a CDN if it isn't already
	// present as window.XLSX.
	ensure_xlsx_lib(callback) {
		if (window.XLSX) {
			callback();
			return;
		}
		frappe.dom.freeze('Loading Excel export library...');
		let script = document.createElement('script');
		script.src = 'https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js';
		script.onload = () => {
			frappe.dom.unfreeze();
			callback();
		};
		script.onerror = () => {
			frappe.dom.unfreeze();
			frappe.msgprint('Could not load the Excel export library. Check your internet connection and try again.');
		};
		document.head.appendChild(script);
	}

	build_and_download_xlsx() {
		let header = this.columns.map((c) => c.label);
		let data = [header];
		this.rows.forEach((row) => {
			data.push(this.columns.map((c) => (row[c.fieldname] != null ? row[c.fieldname] : '')));
		});

		let ws = XLSX.utils.aoa_to_sheet(data);

		// Force these columns to be stored as TEXT cells, not numbers — this is
		// what stops Excel/the bank's reader from converting long account
		// numbers into scientific notation (e.g. 9.2402E+14), which happened
		// earlier when this data went through a normal CSV -> Excel round trip.
		let text_fieldnames = [
			'debit_account_no', 'beneficiary_account_no', 'beneficiary_ifsc',
			'transaction_date', 'payment_mode', 'customer_ref_no', 'beneficiary_nickname',
		];
		this.columns.forEach((col, col_idx) => {
			if (!text_fieldnames.includes(col.fieldname)) return;
			for (let row_idx = 1; row_idx <= this.rows.length; row_idx++) {
				let cell_ref = XLSX.utils.encode_cell({ r: row_idx, c: col_idx });
				let cell = ws[cell_ref];
				if (cell) {
					cell.t = 's';
					cell.v = String(cell.v);
				}
			}
		});

		let wb = XLSX.utils.book_new();
		XLSX.utils.book_append_sheet(wb, ws, 'Bulk Payment');

		let filename = `Bulk_Payment_${this.txn_type_field.get_value()}_${frappe.datetime.get_today()}.xlsx`;
		XLSX.writeFile(wb, filename);
	}
}
