"""Explicit, previewed column mapping for local bank CSV files."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from itsdangerous import URLSafeSerializer, BadSignature

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

bp = Blueprint('csv_import', __name__)
FIELDS = ('date', 'description', 'amount', 'debit', 'credit', 'reference', 'balance', 'currency')


@bp.route('/import/csv/<filename>', methods=['GET', 'POST'])
def mapping(filename):
    from ..csv_mapping import inspect_csv, load_mapping, parse_mapped_csv, save_mapping
    state = current_app.extensions['books_state']
    if Path(filename).name != filename or filename.startswith('.'):
        abort(404)
    path = state.workspace.inbox / filename
    key, sep, _ = filename.partition('--')
    source = state.sources().get(key) if sep else None
    if not path.is_file() or path.suffix.lower() != '.csv' or source is None or source.statement_preset == 'paypal':
        abort(404)
    try:
        saved = load_mapping(path) or {}
    except (ValueError, OSError):
        saved = {}
    values = dict(saved)
    values.setdefault('delimiter', ',')
    values.setdefault('number_format', '')
    values.setdefault('amount_sign', '')
    values.setdefault('expected_currency', source.currency)
    if request.method == 'POST':
        values = {name: request.form.get(name, '') for name in (*FIELDS, 'delimiter', 'date_format', 'number_format', 'amount_sign', 'balance_order')}
        values['expected_currency'] = source.currency
    error = None
    inspection = None
    preview = None
    preview_token = None
    ambiguous_dates = 0
    signer = URLSafeSerializer(current_app.secret_key, salt='csv-preview')
    try:
        inspection = inspect_csv(path, delimiter=values['delimiter'])
        if request.method == 'POST' and request.form.get('action') != 'columns':
            preview = parse_mapped_csv(path, values)
            if values['date_format'] in ('mdy', 'dmy'):
                other = '%d/%m/%Y' if values['date_format'] == 'mdy' else '%m/%d/%Y'
                for txn in preview.transactions:
                    try:
                        alternate = datetime.strptime(txn.raw[values['date']].strip().replace('-', '/'), other).date().isoformat()
                        ambiguous_dates += alternate != txn.date
                    except ValueError:
                        pass
            signature = hashlib.sha256(json.dumps({
                'source': preview.source_sha256, 'mapping': values,
                'account': source.account, 'invert': source.invert,
                'balance_sign': source.statement_balance_sign,
            }, sort_keys=True).encode()).hexdigest()
            preview_token = signer.dumps(signature)
            if request.form.get('action') == 'save':
                try:
                    reviewed = signer.loads(request.form.get('preview_token', ''))
                except BadSignature:
                    reviewed = None
                if reviewed != signature:
                    raise ValueError('The file or mapping changed. Preview all rows again before saving.')
                if source.lifecycle != 'active':
                    raise ValueError('Activate this account before saving an import mapping.')
                save_mapping(path, values)
                flash(f'Validated {len(preview.transactions)} rows. Saved the CSV mapping for {filename}. Prepare proposals next.', 'success')
                return redirect(url_for('import_page'))
    except (ValueError, OSError, UnicodeError) as exc:
        preview = None
        error = str(exc)
        for key, label in [('date_format', 'Date order'), ('number_format', 'Decimal separator'), ('amount_sign', 'Positive amounts mean')]:
            error = error.replace(key, label)
    return render_template('csv_mapping.html', title='Map bank CSV', filename=filename,
                           source=source, values=values, inspection=inspection,
                           preview=preview, error=error, fields=FIELDS, preview_token=preview_token, ambiguous_dates=ambiguous_dates, columns_read=request.method == 'POST' and request.form.get('action') == 'columns')
