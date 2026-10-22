#!/usr/bin/env python3
"""Apply the owner-authorized, unmetered DramaFoundry service configuration.

Run by the existing merged-main release on the production host. Personal
Mantice tokens, budgets, project data and historical usage are not modified.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.request

ROOT = Path('/opt/dramafoundry')
SERVICE_USER = 'dramafoundry-service'
SERVICE_LABEL = 'dramafoundry-team-unlimited'
SECRET_FILE = ROOT / 'env/mantice-service.json'


def private_json(path: Path, body: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(body, output, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())


def main() -> None:
    revision = sys.argv[1]
    release = ROOT / 'releases' / revision
    if Path.cwd().resolve() != release:
        raise RuntimeError('Run the merged release from its own release directory')
    env = {}
    for line in Path('/etc/fornace-llm2.env').read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            env[key] = value.strip().strip('"').strip("'")
    admin_token = env['ADMIN_TOKEN']

    def mantice(method: str, path: str, body: dict | None = None) -> dict:
        headers = {'Authorization': f'Bearer {admin_token}'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if method == 'POST' and path == '/admin/tokens':
            headers['Idempotency-Key'] = 'dramafoundry-service-unlimited-20261022'
        request = urllib.request.Request(
            f'https://llm.fornace.net{path}', method=method, headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    users = mantice('GET', '/admin/users')['data']
    matches = [user for user in users if user['id'] == SERVICE_USER]
    if not matches:
        mantice('POST', '/admin/users', {
            'id': SERVICE_USER, 'name': 'DramaFoundry team service', 'plan': 'default',
        })
    elif len(matches) != 1:
        raise RuntimeError('Ambiguous DramaFoundry service identity')

    inventory = mantice('GET', '/admin/tokens')['data']
    if not SECRET_FILE.exists():
        existing = [row for row in inventory if row['user_id'] == SERVICE_USER]
        if existing:
            raise RuntimeError('Service token exists without its retained credential; preserve it')
        issued = mantice('POST', '/admin/tokens', {
            'user_id': SERVICE_USER, 'label': SERVICE_LABEL,
            'request_limit': 0, 'token_limit': 0, 'max_budget_usd': 0,
            'expires_at': 0, 'allowed_models': [],
        })
        private_json(SECRET_FILE, issued)
        inventory = mantice('GET', '/admin/tokens')['data']
    credential = json.loads(SECRET_FILE.read_text())
    token = credential['token']
    fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
    records = [row for row in inventory if row['fingerprint'] == fingerprint]
    if len(records) != 1 or records[0]['user_id'] != SERVICE_USER:
        raise RuntimeError('Retained service credential does not match live token ownership')
    policy = records[0]
    if (not policy['enabled'] or policy['expires_at'] != 0
            or policy['request_limit'] != 0 or policy['token_limit'] != 0
            or policy['max_budget_usd'] != 0 or policy['allowed_models']):
        raise RuntimeError('DramaFoundry service token is not active and unrestricted')

    with sqlite3.connect(f'file:{ROOT}/data/newapi/one-api.db?mode=ro', uri=True) as db:
        row = db.execute('SELECT key FROM channels WHERE id=2 AND name=?', ('mantice',)).fetchone()
    if row is None:
        raise RuntimeError('The existing Mantice provider channel is missing')
    previous_fingerprint = hashlib.sha256(row[0].encode()).hexdigest()[:12]
    previous = [item for item in inventory if item['fingerprint'] == previous_fingerprint]
    if not (release / 'credits-before.json').exists():
        private_json(release / 'credits-before.json', {
            'provider_channel': 2, 'previous_mantice_token': previous,
            'service_token': policy, 'recorded_at': int(time.time()),
        })

    # The real provisioner supplies NewAPI admin auth. Bearers travel only over
    # this child's stdin and loopback/container HTTP, never argv or build logs.
    reconcile_gateway = r'''
import json, sqlite3, sys
import httpx
from novelvideo.newapi_provisioner import (
    get_provisioner_config, ensure_admin_access_token, admin_headers,
)
from novelvideo.model_gateway_settings import get_model_gateway_settings
cfg = get_provisioner_config()
admin = ensure_admin_access_token(cfg)
request = json.load(sys.stdin)
headers = admin_headers(admin)
with httpx.Client(base_url=cfg.admin_base_url, headers=headers, timeout=30) as client:
    def api(method, path, body=None):
        response = client.request(method, path, json=body)
        response.raise_for_status()
        result = response.json()
        if result.get('success') is not True:
            raise RuntimeError(f'NewAPI {method} {path}: {result.get("message")}')
        return result
    with sqlite3.connect(f'file:{cfg.sqlite_path}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        channel = dict(db.execute('SELECT * FROM channels WHERE id=2 AND name=?', ('mantice',)).fetchone())
        relay_key = get_model_gateway_settings()['custom_newapi_api_key'].removeprefix('sk-')
        relay = db.execute('SELECT id, user_id FROM tokens WHERE key=?', (relay_key,)).fetchone()
    if relay is None or relay['user_id'] != admin.admin_user_id:
        raise RuntimeError('Configured relay token does not belong to the hosted gateway owner')
    # Keep provider models, abilities and every unrelated channel field intact.
    if channel['key'] != request['mantice_token']:
        api('PUT', '/api/channel/', {
            **{key: channel[key] for key in ['id', 'type', 'name', 'models', 'group', 'base_url']},
            'key': request['mantice_token'],
        })
    options = {row['key']: row['value'] for row in api('GET', '/api/option/')['data']}
    ratios = json.loads(options['GroupRatio'])
    if channel['group'] != 'default':
        raise RuntimeError('Unexpected provider group; preserve existing routing')
    ratios['default'] = 0
    for key, value in {
        'GroupRatio': json.dumps(ratios),
        'group_ratio_setting.group_ratio': json.dumps(ratios),
        'quota_setting.enable_free_model_pre_consume': 'false',
    }.items():
        api('PUT', '/api/option/', {'key': key, 'value': value})
    token = api('GET', f'/api/token/{relay["id"]}')['data']
    if token['group'] != 'default':
        raise RuntimeError('Unexpected relay group; preserve existing routing')
    token['unlimited_quota'] = True
    token['expired_time'] = -1
    # GetToken's response is masked; never submit that placeholder as a key.
    api('PUT', '/api/token/', {
        key: token[key] for key in [
            'id', 'name', 'expired_time', 'remain_quota', 'unlimited_quota',
            'model_limits_enabled', 'model_limits', 'allow_ips', 'group',
            'cross_group_retry',
        ]
    })
    updated = api('GET', '/api/option/')['data']
    options = {row['key']: row['value'] for row in updated}
    if json.loads(options['GroupRatio'])['default'] != 0 or options['quota_setting.enable_free_model_pre_consume'] != 'false':
        raise RuntimeError('Gateway did not retain its free-credit policy')
    token = api('GET', f'/api/token/{relay["id"]}')['data']
    if not token['unlimited_quota'] or token['expired_time'] != -1:
        raise RuntimeError('Gateway did not retain its unlimited relay policy')
    print(json.dumps({'relay_token_id': relay['id'], 'group': 'default', 'credit_cost_multiplier': 0, 'unlimited_quota': True}))
'''
    result = subprocess.run(
        ['docker', 'exec', '-i', 'dramafoundry-api-1', 'python', '-c', reconcile_gateway],
        input=json.dumps({'mantice_token': token}), text=True, check=True,
        stdout=subprocess.PIPE,
    )
    print(result.stdout, end='')
    receipt = {
        'commit': revision, 'service_user': SERVICE_USER, 'token_fingerprint': fingerprint,
        'request_limit': 0, 'token_limit': 0, 'max_budget_usd': 0, 'expires_at': 0,
        'all_models': True, 'personal_tokens_unchanged': True,
        'configured_at': int(time.time()),
    }
    path = release / 'credits-configuration.json'
    if not path.exists():
        private_json(path, receipt)
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
