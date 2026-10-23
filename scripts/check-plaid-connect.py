"""Local HTTP connection smoke test; no Plaid service or credentials required."""
from __future__ import annotations
import json
import re
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from bookkeeping.plaid import PlaidClient, PlaidItemStore
from bookkeeping.plaid_connect import run_connect


def main():
    calls = []
    def post(path, payload):
        calls.append((path, payload))
        return {
            '/link/token/create': {'link_token':'fictional-link'},
            '/item/public_token/exchange': {'access_token':'fictional-access','item_id':'item-qa'},
            '/item/get': {'item':{'institution_id':'ins-qa'}},
            '/institutions/get_by_id': {'institution':{'name':'Fictional bank'}},
            '/accounts/get': {'accounts':[{'account_id':'account-qa','name':'Checking'}]},
        }[path]
    with tempfile.TemporaryDirectory() as directory:
        store = PlaidItemStore(Path(directory))
        client = PlaidClient('fictional-client','fictional-secret',environment='production',post=post)
        def browser(url):
            with urlopen(url) as response:
                body = response.read().decode()
            nonce = re.search(r'nonce: "([^"]+)"', body)[1]
            try:
                urlopen(Request(url,headers={'Host':'rebinding.example'}))
                raise AssertionError('Foreign Host accepted')
            except HTTPError as error:
                assert error.code == 403
            try:
                urlopen(Request(url+'exchange',data=b'{"nonce":"bad"}',headers={'Content-Type':'application/json'}))
                raise AssertionError('Bad nonce accepted')
            except HTTPError as error:
                assert error.code == 400
            with urlopen(Request(url+'exchange',data=json.dumps({'nonce':nonce,'public_token':'fictional-public'}).encode(),headers={'Content-Type':'application/json'})) as response:
                assert response.status == 200
            return True
        with patch('bookkeeping.plaid_connect.webbrowser.open',side_effect=browser):
            result = run_connect(client,store,'http://127.0.0.1:18767/',timeout_seconds=5)
        assert 'redirect_uri' not in calls[0][1], calls[0]
        assert result['institution_name'] == 'Fictional bank'
        assert store.items()[0]['access_token'] == 'fictional-access'
        assert store.path.stat().st_mode & 0o777 == 0o600
    print('PASS: production popup, HTTP exchange, invalid nonce recovery, Host protection, private token storage.')


if __name__ == '__main__':
    main()
