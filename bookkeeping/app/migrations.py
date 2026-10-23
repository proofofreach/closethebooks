"""File-based ledger-history migration routes.

Registration (owned by the application factory):
    from .migrations import bp as file_migrations_bp
    app.register_blueprint(file_migrations_bp)
Uses the factory's local-request/CSRF guard and books_state extension.
"""
from __future__ import annotations

import io
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from ..file_migration import FIELDS, LIMITATIONS, MAX_UPLOAD_BYTES, PROVIDERS, FileMigration, MigrationError

bp = Blueprint('file_migrations', __name__)


def _service() -> FileMigration:
    return FileMigration(current_app.extensions['books_state'].workspace)


def _provider(provider):
    if provider not in PROVIDERS: abort(404)
    return PROVIDERS[provider]


def _context(provider, stage_id):
    label = _provider(provider)
    service = _service()
    try:
        stage, meta = service.stage(stage_id, provider)
    except FileNotFoundError:
        abort(404)
    return service, stage, {'provider': provider, 'provider_label': label, 'stage_id': stage_id, 'upload': meta, 'limitations': LIMITATIONS}


def _failure(exc, **context):
    message = str(exc) if isinstance(exc, (MigrationError, ValueError)) else 'Migration files could not be read or saved. Check local file permissions and available disk space, then reload the preview.'
    if request.is_json:
        return jsonify(error=message, status='blocked'), 400
    return render_template('file_migration_error.html', title='Migration blocked', error=message, **context), 400


def _selection(values):
    return {key: values.get(key, default) for key, default in (('member', ''), ('sheet', ''), ('header_row', '1'), ('delimiter', ','))}


@bp.get('/migrations')
def index():
    return render_template('file_migrations.html', title='Move your books', providers=PROVIDERS, limitations=LIMITATIONS)


@bp.route('/migrate/files/<provider>', methods=['GET', 'POST'])
def start(provider):
    label = _provider(provider)
    context = {'provider': provider, 'provider_label': label, 'limitations': LIMITATIONS}
    service = _service()
    if request.method == 'POST':
        try:
            upload = request.files.get('file')
            if not upload or not upload.filename:
                raise MigrationError('Choose an accounting export file.')
            stage_id = service.upload(provider, upload.filename, upload.stream.read(MAX_UPLOAD_BYTES + 1))
            return redirect(url_for('.review', provider=provider, stage_id=stage_id))
        except (ValueError, OSError) as exc:
            return _failure(exc, **context)
    return render_template('file_migration_start.html', title=f'Move from {label}', blocker=service.blocker(), **context)


@bp.get('/migrate/files/<provider>/<stage_id>')
def review(provider, stage_id):
    service, stage, context = _context(provider, stage_id)
    try:
        report = service.report(stage_id)
        if report.get('status') in ('applied', 'applying') or (report.get('status') == 'ready' and not request.args.get('edit')):
            return render_template('file_migration_preview.html', title='Review ledger migration', report=report, **context)
        selection = _selection(request.args)
        draft = service.draft(stage_id)
        spec = draft.get('spec', {})
        if not request.args or set(request.args) == {'edit'}:
            selection = spec.get('selection', selection)
        error = None
        try:
            table = service.inspect(stage_id, selection)
        except MigrationError as exc:
            table = service.choices(stage_id, selection)
            error = str(exc)
        return render_template('file_migration_mapping.html', title='Map journal columns',
                               table=table, selection=selection, fields=FIELDS, spec=spec, error=error, **context)
    except (ValueError, OSError) as exc:
        return _failure(exc, **context)


@bp.post('/migrate/files/<provider>/<stage_id>/configure')
def configure(provider, stage_id):
    service, _, context = _context(provider, stage_id)
    spec = {
        'selection': _selection(request.form),
        'fields': {key: request.form.get('field_' + key, '') for key in FIELDS},
        **{key: request.form.get(key, '') for key in ('date_format', 'currency', 'source_debit', 'source_credit', 'totals_reference')},
    }
    try:
        draft = service.configure(stage_id, spec)
        return render_template('file_migration_accounts.html', title='Map source accounts', draft=draft, **context)
    except (ValueError, OSError) as exc:
        if isinstance(exc, MigrationError) and not request.is_json:
            try:
                table = service.inspect(stage_id, spec['selection'])
                return render_template('file_migration_mapping.html', title='Map journal columns', table=table,
                                       selection=spec['selection'], fields=FIELDS, spec=spec, error=str(exc), error_scope='mapping', **context), 400
            except (ValueError, OSError):
                pass
        return _failure(exc, **context)


@bp.post('/migrate/files/<provider>/<stage_id>/preview')
def preview(provider, stage_id):
    service, stage, context = _context(provider, stage_id)
    draft = None
    try:
        if request.is_json:
            spec = request.get_json()
        else:
            if not (stage / 'draft.json').exists():
                raise MigrationError('Map journal columns before mapping accounts.')
            draft = service.draft(stage_id)
            spec = draft['spec']
            spec['accounts'] = {source: request.form.get(f'account_{index}', '').strip() for index, source in enumerate(draft['source_accounts'])}
        report = service.preview(stage_id, spec)
        if request.is_json: return jsonify(report)
        return redirect(url_for('.review', provider=provider, stage_id=stage_id), code=303)
    except (ValueError, OSError) as exc:
        if draft and isinstance(exc, MigrationError) and not request.is_json:
            return render_template('file_migration_accounts.html', title='Map source accounts', draft=draft,
                                   error=str(exc), **context), 400
        return _failure(exc, **context)


@bp.post('/migrate/files/<provider>/<stage_id>/apply')
def apply(provider, stage_id):
    service, _, context = _context(provider, stage_id)
    try:
        data = request.get_json() if request.is_json else request.form
        if not hasattr(data, 'get'):
            raise MigrationError('Apply requires a preview digest and explicit confirmation.')
        report = service.apply(stage_id, data.get('digest', ''), confirm=data.get('confirm', ''))
        if request.is_json: return jsonify(report)
        return redirect(url_for('.review', provider=provider, stage_id=stage_id))
    except (ValueError, OSError) as exc:
        return _failure(exc, **context)


@bp.get('/migrate/files/<provider>/<stage_id>/report')
def report(provider, stage_id):
    service, _, context = _context(provider, stage_id)
    try:
        payload = service.report(stage_id)
        if not payload: abort(404)
        response = jsonify(payload)
        response.headers['Content-Disposition'] = f'attachment; filename="migration-{stage_id}-report.json"'
        return response
    except (ValueError, OSError) as exc:
        return _failure(exc, **context)


@bp.get('/migrate/files/<provider>/<stage_id>/source')
def source(provider, stage_id):
    service, _, context = _context(provider, stage_id)
    try:
        data, meta = service.source(stage_id)
        filename = secure_filename(meta['filename']) or meta['source']
        return send_file(io.BytesIO(data), as_attachment=True, download_name=filename, mimetype='application/octet-stream')
    except (ValueError, OSError) as exc:
        return _failure(exc, **context)


@bp.get('/migrate/files/<provider>/<stage_id>/candidate')
def candidate(provider, stage_id):
    service, stage, context = _context(provider, stage_id)
    try:
        report = service.report(stage_id)
        path = stage / 'candidate.beancount'
        if report.get('status') not in ('ready', 'applied', 'applying') or not path.exists(): abort(404)
        if path.is_symlink(): raise MigrationError('Candidate file must not be a symbolic link.')
        return send_file(path, as_attachment=True, download_name=f'migration-{stage_id}.beancount', mimetype='text/plain')
    except (ValueError, OSError) as exc:
        return _failure(exc, **context)
