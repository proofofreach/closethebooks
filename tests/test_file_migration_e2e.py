"""Full HTTP → retained evidence → Beancount migration scenarios.

Written before implementation. Run with --junitxml=/tmp/file-migration-e2e.xml.
Only fictional temporary workspaces are used. No mocked parsing or posting.
"""
import copy
import datetime as dt
import io
import json
import zipfile
from pathlib import Path

import pytest
from beancount import loader
from beancount.core.data import Transaction

from bookkeeping.app import create_app
from bookkeeping.setup import init_workspace, add_account_source

CSV = b'Entry,When,Ledger,Dr,Cr,CCY,Memo\nJ1,2024-01-01,Checking,100,0,USD,Sale\nJ1,2024-01-01,Sales,0,100,USD,Sale\nJ2,2024-01-02,Office,20,0,USD,Paper\nJ2,2024-01-02,Checking,0,20,USD,Paper\n'
SPEC = {
    'selection': {'header_row': 1, 'delimiter': ','},
    'fields': {'journal': 'Entry', 'date': 'When', 'account': 'Ledger', 'debit': 'Dr', 'credit': 'Cr', 'currency': 'CCY', 'description': 'Memo'},
    'accounts': {'Checking': 'Assets:Bank:Checking', 'Sales': 'Income:Sales', 'Office': 'Expenses:Office'},
    'date_format': 'iso', 'currency': 'USD', 'source_debit': '120', 'source_credit': '120',
    'totals_reference': 'Fictional source Journal report, all dates, accrual, USD',
}


@pytest.fixture
def flow(tmp_path):
    from bookkeeping.app.migrations import bp
    workspace = init_workspace(tmp_path / 'fictional', name='Fictional Migration', start_date=dt.date(2026, 1, 1), git=False)
    app = create_app(workspace, mount_reports=False)
    if 'file_migrations' not in app.blueprints:
        app.register_blueprint(bp)
    app.testing = True
    client = app.test_client()
    token = app.jinja_env.globals['csrf_token']
    return workspace, client, {'X-Books-Token': token}


def ledger_bytes(workspace):
    return {str(p.relative_to(workspace.ledger_root)): p.read_bytes() for p in workspace.ledger_root.rglob('*') if p.is_file()}


def upload(flow, payload=CSV, name='journal.csv', provider='quickbooks'):
    _, client, headers = flow
    response = client.post(f'/migrate/files/{provider}', data={'file': (io.BytesIO(payload), name)}, headers=headers)
    assert response.status_code == 302, response.get_data(as_text=True)
    return response.headers['Location']


def preview(flow, url, spec=None):
    return flow[1].post(url + '/preview', json=copy.deepcopy(spec or SPEC), headers=flow[2])


def apply(flow, url, digest, confirm='apply'):
    return flow[1].post(url + '/apply', json={'digest': digest, 'confirm': confirm}, headers=flow[2])


@pytest.mark.parametrize('provider', ['quickbooks', 'wave'])
def test_csv_preview_apply_replay_and_evidence(flow, provider, tmp_path):
    ws, client, headers = flow
    before = ledger_bytes(ws)
    url = upload(flow, provider=provider)
    assert client.get(url).status_code == 200
    response = preview(flow, url)
    assert response.status_code == 200, response.get_data(as_text=True)
    report = response.json
    assert report['status'] == 'ready'
    assert report['journals'] == 2 and report['rows'] == 4
    assert report['debit'] == report['credit'] == '120'
    assert ledger_bytes(ws) == before
    assert apply(flow, url, report['digest'], confirm='').status_code == 400
    assert ledger_bytes(ws) == before
    result = apply(flow, url, report['digest'])
    assert result.status_code == 200, result.get_data(as_text=True)
    entries, errors, _ = loader.load_file(str(ws.ledger_main))
    assert not errors
    assert len([e for e in entries if isinstance(e, Transaction)]) == 2
    assert client.get(url + '/source').data == CSV
    assert client.get(url + '/report').json['status'] == 'applied'
    after = ledger_bytes(ws)
    assert apply(flow, url, report['digest']).status_code == 400
    assert ledger_bytes(ws) == after
    (tmp_path / 'verified-result.json').write_text(json.dumps(result.json, indent=2))


@pytest.mark.parametrize(('change', 'needle'), [
    ('unbalanced', 'balance'), ('totals', 'total'), ('unmapped', 'account'),
    ('currency', 'currency'), ('negative', 'negative'), ('nan', 'amount'),
    ('both_sides', 'debit'), ('missing_id', 'journal'), ('duplicate_header', 'header'),
    ('extra_row', 'row'), ('date', 'date'), ('journal_date', 'date'),
    ('missing_field', 'column'), ('same_column', 'column'), ('precision', 'amount'),
])
def test_invalid_inputs_never_write_ledger(flow, change, needle):
    ws, _, _ = flow
    original = ledger_bytes(ws)
    data, spec = CSV, copy.deepcopy(SPEC)
    if change == 'unbalanced': data = data.replace(b'Sales,0,100', b'Sales,0,99')
    if change == 'totals': spec['source_debit'] = '121'
    if change == 'unmapped': del spec['accounts']['Office']
    if change == 'currency': data = data.replace(b'USD,Paper', b'EUR,Paper')
    if change == 'negative': data = data.replace(b'Checking,100,0', b'Checking,-100,0')
    if change == 'nan': data = data.replace(b'Checking,100,0', b'Checking,NaN,0')
    if change == 'both_sides': data = data.replace(b'Checking,100,0', b'Checking,100,1')
    if change == 'missing_id': data = data.replace(b'J1,2024-01-01,Sales', b',2024-01-01,Sales')
    if change == 'duplicate_header': data = data.replace(b'Dr,Cr', b'Dr,Dr')
    if change == 'extra_row': data += b'Total,,,,120,120,USD\n'
    if change == 'date': data = data.replace(b'2024-01-01', b'01/02/2024')
    if change == 'journal_date': data = data.replace(b'J1,2024-01-01,Sales', b'J1,2024-01-03,Sales')
    if change == 'missing_field': spec['fields']['debit'] = 'Absent'
    if change == 'same_column': spec['fields']['credit'] = 'Dr'
    if change == 'precision': data = data.replace(b'Checking,100,0', b'Checking,0.00000000001,0')
    url = upload(flow, data)
    response = preview(flow, url, spec)
    assert response.status_code == 400, response.get_data(as_text=True)
    assert needle in response.json['error'].lower()
    assert apply(flow, url, 'invented').status_code == 400
    assert ledger_bytes(ws) == original


@pytest.mark.parametrize('existing', ['source', 'transaction', 'invalid_ledger'])
def test_existing_workspace_is_refused(flow, existing):
    ws, _, _ = flow
    if existing == 'source':
        add_account_source(ws, key='checking', name='Checking', account='Assets:Bank:Checking', opened=dt.date(2026, 1, 1))
    else:
        with ws.ledger_main.open('a') as f:
            f.write('\n2026-01-01 * "Existing"\n  Expenses:Software  5 USD\n  Income:Sales  -5 USD\n' if existing == 'transaction' else '\nnot beancount\n')
    original = ledger_bytes(ws)
    url = upload(flow)
    assert preview(flow, url).status_code == 400
    assert ledger_bytes(ws) == original


@pytest.mark.parametrize('change', ['ledger', 'sources', 'evidence', 'candidate', 'wrong_digest'])
def test_apply_rechecks_every_boundary(flow, change):
    ws, _, _ = flow
    url = upload(flow)
    report = preview(flow, url).json
    stage = ws.root / 'imports' / 'file-migrations' / url.rsplit('/', 1)[1]
    if change == 'ledger':
        with ws.ledger_main.open('a') as f: f.write('\n; changed since preview\n')
    if change == 'sources':
        add_account_source(ws, key='new', name='New', account='Assets:Bank:New', opened=dt.date(2026, 1, 1))
    if change == 'evidence': (stage / 'source.csv').write_bytes(CSV + b'altered')
    if change == 'candidate': (stage / 'candidate.beancount').write_text('altered')
    original = ledger_bytes(ws)
    response = apply(flow, url, 'wrong' if change == 'wrong_digest' else report['digest'])
    assert response.status_code == 400
    assert ledger_bytes(ws) == original


def zip_bytes(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in members: z.writestr(name, data)
    return out.getvalue()


def test_wave_zip_requires_explicit_member_and_retains_all_evidence(flow):
    data = zip_bytes([('export/accounting.csv', CSV), ('export/customers.csv', b'Name\nFictional\n')])
    url = upload(flow, data, 'wave.zip', 'wave')
    assert preview(flow, url).status_code == 400
    spec = copy.deepcopy(SPEC)
    spec['selection']['member'] = 'export/accounting.csv'
    report = preview(flow, url, spec).json
    assert report['status'] == 'ready'
    assert 'export/customers.csv' in report['unimported_members']
    assert apply(flow, url, report['digest']).status_code == 200
    assert flow[1].get(url + '/source').data == data


@pytest.mark.parametrize('members', [
    [('../escape.csv', CSV)], [('/absolute.csv', CSV)], [('a.csv', CSV), ('a.csv', CSV)],
    [('nested.zip', b'zip')], [('huge.csv', b'0' * (1024 * 1024))],
])
def test_unsafe_archives_are_refused_without_extraction(flow, members):
    ws, client, headers = flow
    original = ledger_bytes(ws)
    response = client.post('/migrate/files/wave', data={'file': (io.BytesIO(zip_bytes(members)), 'bad.zip')}, headers=headers)
    assert response.status_code == 400
    assert ledger_bytes(ws) == original
    assert not (ws.root / 'escape.csv').exists()


def xlsx_bytes(formula=False):
    # Minimal valid OOXML; no optional spreadsheet libraries needed by tests.
    from xml.sax.saxutils import escape
    rows = [r.split(',') for r in CSV.decode().strip().splitlines()]
    xml_rows = []
    for n, row in enumerate(rows, 1):
        cells = []
        for c, value in enumerate(row):
            ref = f'{chr(65 + c)}{n}'
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is>{"<f>SUM(1,1)</f>" if formula and n == 2 and c == 3 else ""}</c>')
        xml_rows.append(f'<row r="{n}">{"".join(cells)}</row>')
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    return zip_bytes([
        ('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Journal" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        ('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>'),
        ('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{ns}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>'),
    ])


@pytest.mark.parametrize('formula', [False, True])
def test_xlsx_end_to_end_and_formula_refusal(flow, formula):
    ws, _, _ = flow
    original = ledger_bytes(ws)
    url = upload(flow, xlsx_bytes(formula), 'journal.xlsx')
    spec = copy.deepcopy(SPEC)
    spec['selection']['sheet'] = 'Journal'
    response = preview(flow, url, spec)
    if formula:
        assert response.status_code == 400 and 'formula' in response.json['error'].lower()
        assert ledger_bytes(ws) == original
    else:
        assert response.status_code == 200, response.get_data(as_text=True)
        assert apply(flow, url, response.json['digest']).status_code == 200
        assert not loader.load_file(str(ws.ledger_main))[1]


def test_routes_enforce_csrf_provider_and_stage_boundaries(flow):
    _, client, _ = flow
    assert client.post('/migrate/files/wave').status_code == 403
    assert client.get('/migrate/files/unknown').status_code == 404
    url = upload(flow)
    assert client.get(url.replace('/quickbooks/', '/wave/')).status_code == 404
    assert client.get('/migrate/files/wave/not-a-stage').status_code == 404


def test_html_mapping_preview_apply_workflow(flow):
    _, client, headers = flow
    url = upload(flow)
    response = client.post(url + '/configure', data={
        **{'field_' + k: v for k, v in SPEC['fields'].items()},
        **{k: SPEC[k] for k in ('date_format', 'currency', 'source_debit', 'source_credit', 'totals_reference')},
        'header_row': '1', 'delimiter': ',',
    }, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert b'Checking' in response.data
    response = client.post(url + '/preview', data={
        'account_0': 'Assets:Bank:Checking', 'account_1': 'Expenses:Office', 'account_2': 'Income:Sales',
    }, headers=headers)
    assert response.status_code == 303, response.get_data(as_text=True)
    response = client.get(response.headers['Location'])
    assert response.status_code == 200
    assert b'Apply ledger history' in response.data
    assert b'open invoices' in response.data.lower()


@pytest.mark.parametrize('change', ['invalid_account', 'merge_accounts', 'missing_totals_reference', 'invalid_default_currency', 'bad_mapping_type'])
def test_strict_contract_rejects_ambiguous_or_unsafe_mapping(flow, change):
    ws, _, _ = flow
    original = ledger_bytes(ws)
    spec, data = copy.deepcopy(SPEC), CSV
    if change == 'invalid_account': spec['accounts']['Checking'] = 'Assets:Bad\n2020-01-01 open Income:Injected'
    if change == 'merge_accounts': spec['accounts']['Office'] = spec['accounts']['Checking']
    if change == 'missing_totals_reference': spec['totals_reference'] = ''
    if change == 'invalid_default_currency': spec['currency'] = 'EUR'
    if change == 'bad_mapping_type': spec['fields'] = ['Dr']
    url = upload(flow, data)
    assert preview(flow, url, spec).status_code == 400
    assert ledger_bytes(ws) == original


def test_failed_repreview_invalidates_old_approval(flow):
    ws, _, _ = flow
    original = ledger_bytes(ws)
    url = upload(flow)
    old = preview(flow, url).json
    spec = copy.deepcopy(SPEC)
    spec['source_credit'] = '900'
    assert preview(flow, url, spec).status_code == 400
    assert apply(flow, url, old['digest']).status_code == 400
    assert ledger_bytes(ws) == original


def test_preview_refresh_and_evidence_survive_app_restart(flow):
    from bookkeeping.app.migrations import bp
    ws, _, _ = flow
    url = upload(flow)
    report = preview(flow, url).json
    restarted = create_app(ws, mount_reports=False)
    if 'file_migrations' not in restarted.blueprints:
        restarted.register_blueprint(bp)
    flow2 = (ws, restarted.test_client(), {'X-Books-Token': restarted.jinja_env.globals['csrf_token']})
    assert flow2[1].get(url).status_code == 200
    assert apply(flow2, url, report['digest']).status_code == 200
    assert not loader.load_file(str(ws.ledger_main))[1]


def test_report_preamble_can_be_corrected_in_the_ui(flow):
    url = upload(flow, b'Fictional Journal report\nAll dates\n' + CSV)
    response = flow[1].get(url)
    assert response.status_code == 200
    assert b'name="header_row"' in response.data
    assert b'role="alert"' in response.data
    response = flow[1].get(url + '?header_row=3&delimiter=,')
    assert response.status_code == 200
    assert b'4 nonblank rows' in response.data
    spec = copy.deepcopy(SPEC)
    spec['selection']['header_row'] = 3
    report = preview(flow, url, spec).json
    assert report['status'] == 'ready' and report['skipped_preamble_rows'] == 2


def test_future_account_registration_and_year_writes_stay_connected(flow):
    from beancount.core.data import Open, Query
    ws, _, _ = flow
    before_queries = [e.query_string for e in loader.load_file(str(ws.ledger_main))[0] if isinstance(e, Query)]
    url = upload(flow)
    report = preview(flow, url).json
    assert apply(flow, url, report['digest']).status_code == 200
    add_account_source(ws, key='future', name='Future account', account='Assets:Bank:Future', opened=dt.date(2027, 1, 1))
    year = ws.ledger_root / 'years' / '2027.beancount'
    year.write_text('2027-01-02 * "Future sale"\n  Assets:Bank:Future  7 USD\n  Income:Sales  -7 USD\n')
    entries, errors, _ = loader.load_file(str(ws.ledger_main))
    assert not errors
    assert any(isinstance(e, Open) and e.account == 'Assets:Bank:Future' for e in entries)
    assert len([e for e in entries if isinstance(e, Transaction)]) == 3
    assert [e.query_string for e in entries if isinstance(e, Query)] == before_queries


def test_identical_split_lines_are_valid_history_not_deduplicated(flow):
    data = CSV + b'J1,2024-01-01,Checking,100,0,USD,Sale\nJ1,2024-01-01,Sales,0,100,USD,Sale\n'
    spec = copy.deepcopy(SPEC)
    spec.update(source_debit='220', source_credit='220')
    url = upload(flow, data)
    report = preview(flow, url, spec).json
    assert report['status'] == 'ready' and report['rows'] == 6
    assert apply(flow, url, report['digest']).status_code == 200
    entries, errors, _ = loader.load_file(str(flow[0].ledger_main))
    assert not errors
    assert sum(len(e.postings) for e in entries if isinstance(e, Transaction)) == 6


@pytest.mark.parametrize('date1904', [False, True])
def test_xlsx_workbook_date_system_is_checked(flow, date1904):
    data = xlsx_bytes()
    members = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            content = archive.read(name).decode()
            if name == 'xl/workbook.xml' and date1904:
                content = content.replace('<sheets>', '<workbookPr date1904="1"/><sheets>')
            if name.endswith('sheet1.xml'):
                origin = dt.date(1904, 1, 1) if date1904 else dt.date(1899, 12, 30)
                for day in [1, 2]:
                    content = content.replace(f'2024-01-0{day}', str((dt.date(2024, 1, day) - origin).days))
            members.append((name, content))
    url = upload(flow, zip_bytes(members), 'serial-dates.xlsx')
    spec = copy.deepcopy(SPEC)
    spec['selection']['sheet'] = 'Journal'
    spec['date_format'] = 'excel1900' if date1904 else 'excel1904'
    response = preview(flow, url, spec)
    assert response.status_code == 400 and 'date system' in response.json['error'].lower()
    spec['date_format'] = 'excel1904' if date1904 else 'excel1900'
    report = preview(flow, url, spec).json
    assert report['first_date'] == '2024-01-01'
    assert apply(flow, url, report['digest']).status_code == 200


@pytest.mark.parametrize('route', ['configure', 'preview'])
def test_interrupted_apply_keeps_evidence_even_after_later_edits(flow, route):
    ws, client, headers = flow
    url = upload(flow)
    report = preview(flow, url).json
    stage = ws.root / 'imports' / 'file-migrations' / url.rsplit('/', 1)[1]
    report['status'] = 'applying'
    (stage / 'report.json').write_text(json.dumps(report))
    with ws.ledger_main.open('a') as f: f.write('\n; later change\n')
    evidence = {p.name: p.read_bytes() for p in stage.iterdir() if p.is_file()}
    response = client.post(url + '/' + route, json=SPEC, headers=headers)
    assert response.status_code == 400
    assert {p.name: p.read_bytes() for p in stage.iterdir() if p.is_file()} == evidence


@pytest.mark.parametrize(('key', 'value'), [('selection', ['wrong']), ('selection', {'delimiter': []}), ('currency', []), ('date_format', {}), ('accounts', {'Checking': []})])
def test_malformed_mapping_payloads_are_actionable(flow, key, value):
    url = upload(flow)
    spec = copy.deepcopy(SPEC)
    spec[key] = value
    response = preview(flow, url, spec)
    assert response.status_code == 400 and response.json['error']


def test_apply_write_failure_rolls_back_every_ledger_file(flow, monkeypatch):
    import bookkeeping.file_migration as migration
    ws, _, _ = flow
    url = upload(flow)
    report = preview(flow, url).json
    before = ledger_bytes(ws)
    original_replace = migration.os.replace
    fired = False
    def fail_commit_once(source, destination):
        nonlocal fired
        if Path(destination) == ws.ledger_main and not fired:
            fired = True
            raise OSError('Fictional disk failure at commit')
        return original_replace(source, destination)
    monkeypatch.setattr(migration.os, 'replace', fail_commit_once)
    assert apply(flow, url, report['digest']).status_code == 400
    assert ledger_bytes(ws) == before


@pytest.mark.parametrize('spec', [None, [], {'selection': None}, {'selection': {'header_row': []}}])
def test_null_or_malformed_contract_returns_400(flow, spec):
    url = upload(flow)
    body = copy.deepcopy(SPEC)
    if isinstance(spec, dict): body.update(spec)
    else: body = spec
    response = flow[1].post(url + '/preview', data=json.dumps(body), content_type='application/json', headers=flow[2])
    assert response.status_code == 400 and response.json['error']


def test_corrupt_draft_is_an_actionable_error(flow):
    ws, client, headers = flow
    url = upload(flow)
    stage = ws.root / 'imports' / 'file-migrations' / url.rsplit('/', 1)[1]
    (stage / 'draft.json').write_text(json.dumps({'spec': [], 'source_accounts': 12}))
    response = client.post(url + '/preview', data={'account_0': 'Assets:Bank:Checking'}, headers=headers)
    assert response.status_code == 400
    assert b'role="alert"' in response.data


def test_validation_errors_preserve_column_and_account_mapping(flow):
    _, client, headers = flow
    url = upload(flow)
    fields = {**{'field_' + k: v for k, v in SPEC['fields'].items()},
              **{k: SPEC[k] for k in ('date_format', 'currency', 'source_debit', 'source_credit', 'totals_reference')},
              'header_row': '1', 'delimiter': ','}
    assert client.post(url + '/configure', data=fields, headers=headers).status_code == 200
    values = {'account_0': 'Assets:Bank:Checking', 'account_1': 'Wrong-Account', 'account_2': 'Income:Sales'}
    response = client.post(url + '/preview', data=values, headers=headers)
    assert response.status_code == 400
    assert b'value="Assets:Bank:Checking"' in response.data
    assert b'value="Wrong-Account"' in response.data
    response = client.get(url + '?edit=1')
    assert b'value="Dr" selected' in response.data
