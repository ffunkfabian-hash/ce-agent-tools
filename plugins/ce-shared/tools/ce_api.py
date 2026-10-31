#!/usr/bin/env python3
"""Explicit Claude API calls (text, optional PNG/JPEG images) with isolated OAuth, reservations and cache usage."""
from __future__ import annotations

import argparse
import base64
from datetime import date
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

from ce_api_budget import BudgetError, BudgetLedger

DEFAULT_POLICY = Path.home() / 'Claude/cubeescape-nexogen/.ce-api.json'
AUTH_DIR = Path.home() / '.config/nexogen/anthropic'
LEDGER = Path.home() / '.local/state/nexogen/api-budget.json'
API = 'https://api.anthropic.com/v1/'
MODEL_IDS = {'haiku': 'claude-haiku-5-5', 'sonnet': 'claude-sonnet-5-5', 'opus': 'claude-opus-5-5'}
IMAGE_TYPES = {b'\x89PNG\r\n\x1a\n': 'image/png', b'\xff\xd8\xff': 'image/jpeg'}
MAX_IMAGE_EDGE = 8000          # API limit per side
FIELDS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')


class ApiError(Exception):
    pass


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ApiError('Doppelter JSON-Schlüssel')
            result[key] = value
        return result
    def constant(value):
        raise ApiError('Nichtendliche JSON-Zahl')
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError) as error:
        raise ApiError('Ungültiges JSON') from error


def positive_int(value, maximum=1_000_000):
    if type(value) is not int or not 0 < value <= maximum:
        raise ApiError('Ungültige Tokengrenze')
    return value


def amount(value):
    if not isinstance(value, str):
        raise ApiError('Preise und Limits müssen Dezimalstrings sein')
    try:
        number = Decimal(value)
    except Exception as error:
        raise ApiError('Ungültiger Preis') from error
    if not number.is_finite() or number <= 0:
        raise ApiError('Preis oder Limit muss positiv und endlich sein')
    return number


def load_policy(path, require_current=True):
    try:
        policy = strict_json(path.read_text(encoding='utf-8'))
        if type(policy.get('schema_version')) is not int or policy['schema_version'] != 1:
            raise ApiError('Unbekannte API-Konfiguration')
        age = (date.today() - date.fromisoformat(policy['pricing_verified'])).days
        if require_current and not 0 <= age <= 30:
            raise ApiError('API-Preise älter als 30 Tage oder Datum ungültig; vor Nutzung prüfen')
        amount(policy['monthly_usd']); amount(policy['request_usd'])
        positive_int(policy['max_input_tokens'], 50_000)
        positive_int(policy['max_output_tokens'], 8192)
        images = policy.get('max_images', 0)
        if type(images) is not int or not 0 <= images <= 8:
            raise ApiError('max_images muss 0–8 sein')
        if images:
            positive_int(policy.get('max_image_bytes'), 3_750_000)
        if policy['default_model'] not in MODEL_IDS:
            raise ApiError('Unbekanntes Standardmodell')
        if not isinstance(policy['system_file'], str) or not policy['system_file'].strip():
            raise ApiError('Systemdatei muss ein nichtleerer Pfad sein')
        for name in MODEL_IDS:
            for field in ('input', 'output', 'write_5m', 'read'):
                amount(policy['prices_per_million'][name][field])
        return policy
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise ApiError('API-Konfiguration fehlt oder ist ungültig') from error


def ant_environment():
    # No inherited key/profile/endpoint/federation override may change this login.
    env = {key: value for key, value in os.environ.items() if not key.startswith('ANTHROPIC_')}
    env['ANTHROPIC_CONFIG_DIR'] = str(AUTH_DIR)
    return env


def ant_command(*args, capture=True):
    binary = shutil.which('ant')
    if not binary:
        raise ApiError('ant fehlt: brew install anthropics/tap/ant')
    AUTH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        result = subprocess.run([binary, '--profile', 'nexogen', *args], env=ant_environment(),
                                capture_output=capture, text=True, timeout=330 if not capture else 60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ApiError('Anmeldung nicht verfügbar; ./ce api login ausführen') from error
    if result.returncode:
        # Never surface CLI stderr/stdout: either can contain credentials.
        raise ApiError('Anmeldung fehlgeschlagen; ./ce api login ausführen')
    return result.stdout.strip() if capture else None


def access_token():
    token = ant_command('auth', 'print-credentials', '--access-token')
    if not token or not re.fullmatch(r'[A-Za-z0-9._~-]+', token):
        raise ApiError('Kein gültiges OAuth-Zugriffstoken; ./ce api login ausführen')
    return token


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError('API-Weiterleitung abgelehnt')


def tls_context():
    context = ssl.create_default_context()
    if sys.platform == 'darwin':
        # Python.org installations may lack a CA bundle. Add the Mac's public
        # system roots; never disable hostname or certificate verification.
        roots = subprocess.run(['/usr/bin/security', 'find-certificate', '-a', '-p',
                                '/System/Library/Keychains/SystemRootCertificates.keychain'],
                               capture_output=True, text=True, timeout=15)
        if roots.returncode or not roots.stdout.strip():
            raise ApiError('macOS-Zertifikatswurzeln nicht lesbar')
        context.load_verify_locations(cadata=roots.stdout)
    return context


def post(endpoint, payload, token):
    if endpoint not in ('messages', 'messages/count_tokens'):
        raise ApiError('Unzulässiger API-Endpunkt')
    request = urllib.request.Request(API + endpoint, data=json.dumps(payload).encode(), headers={
        'Authorization': 'Bearer ' + token, 'anthropic-version': '2023-06-01',
        'content-type': 'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=tls_context())).open(request, timeout=120) as response:
            body = response.read(4_000_001)
            if len(body) > 4_000_000:
                raise ApiError('API-Antwort zu groß')
            value = strict_json(body.decode('utf-8'))
            if not isinstance(value, dict):
                raise ApiError('API-Antwort ist kein JSON-Objekt')
            return value
    except urllib.error.HTTPError as error:
        raise ApiError(f'API-Aufruf fehlgeschlagen (HTTP {error.code}); keine automatische Wiederholung') from None
    except (OSError, UnicodeError) as error:
        raise ApiError('API-Ergebnis unbekannt; keine automatische Wiederholung') from error


def read_text(path):
    try:
        if path.stat().st_size > 150_000:
            raise ApiError('Eingabedatei zu groß (maximal 150 KB)')
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as error:
        raise ApiError('Eingabedatei nicht lesbar') from error
    if not text.strip():
        raise ApiError('Leere Eingabedatei')
    return text


def image_size(data, media_type):
    """(width, height) from the PNG IHDR or the first JPEG SOF marker; None if not found."""
    if media_type == 'image/png':
        return (int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')) if len(data) >= 24 else None
    i = 2
    while i + 9 <= len(data):
        if data[i] != 0xFF:
            return None
        marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], 'big')
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(data[i + 7:i + 9], 'big'), int.from_bytes(data[i + 5:i + 7], 'big')
        if length < 2:
            return None
        i += 2 + length
    return None


def read_images(paths, policy):
    """Image blocks for the user turn; disabled unless the policy sets max_images."""
    if not paths:
        return []
    allowed = policy.get('max_images', 0)
    if not allowed:
        raise ApiError('Bilder sind in der API-Richtlinie nicht freigegeben (max_images fehlt)')
    if len(paths) > allowed:
        raise ApiError(f'Zu viele Bilder (höchstens {allowed})')
    blocks = []
    for path in paths:
        try:
            if path.stat().st_size > policy['max_image_bytes']:
                raise ApiError(f"Bild zu groß (höchstens {policy['max_image_bytes']} Bytes): {path.name}")
            data = path.read_bytes()
        except OSError as error:
            raise ApiError(f'Bild nicht lesbar: {path.name}') from error
        media_type = next((kind for magic, kind in IMAGE_TYPES.items() if data.startswith(magic)), None)
        if media_type is None:
            raise ApiError(f'Nur PNG oder JPEG: {path.name}')
        size = image_size(data, media_type)
        if size is None or not all(0 < edge <= MAX_IMAGE_EDGE for edge in size):
            raise ApiError(f'Bildmaße ungültig oder über {MAX_IMAGE_EDGE} px: {path.name}')
        blocks.append({'type': 'image', 'source': {'type': 'base64', 'media_type': media_type,
                                                   'data': base64.b64encode(data).decode('ascii')}})
    return blocks


def validated_usage(response):
    usage = response.get('usage') if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        raise ApiError('API-Usage fehlt; Kostenreservierung bleibt bestehen')
    result = {}
    for field in FIELDS:
        value = usage.get(field)
        if type(value) is not int or value < 0:
            raise ApiError('API-Usage unvollständig/ungültig; Kostenreservierung bleibt bestehen')
        result[field] = value
    return result


def usage_cost(usage, prices):
    rates = ('input', 'write_5m', 'read', 'output')
    return sum((Decimal(usage[field]) * amount(prices[rate]) for field, rate in zip(FIELDS, rates)), Decimal(0)) / Decimal(1_000_000)


def ask(policy_path, policy, prompt_path, system_path, model, output_tokens, dry_run=False, image_paths=()):
    system = read_text(system_path or policy_path.parent / policy['system_file'])
    prompt = read_text(prompt_path)
    images = read_images(list(image_paths), policy)
    content = images + [{'type': 'text', 'text': prompt}] if images else prompt
    maximum = positive_int(policy['max_output_tokens'] if output_tokens is None else output_tokens,
                           policy['max_output_tokens'])
    name = model or policy['default_model']
    prices = policy['prices_per_million'][name]
    payload = {'model': MODEL_IDS[name], 'max_tokens': maximum,
               'system': [{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral', 'ttl': '5m'}}],
               'messages': [{'role': 'user', 'content': content}]}
    token = access_token()
    count_payload = {key: value for key, value in payload.items() if key != 'max_tokens'}
    counted = post('messages/count_tokens', count_payload, token)
    count = positive_int(counted.get('input_tokens'), policy['max_input_tokens'])
    # Token counting is an estimate. Reserve a cold-cache write plus 10%/256 tokens
    # headroom and maximum output, rather than assuming a future cache hit.
    reserved_input = math.ceil(count * 1.1) + 256
    ceiling = (Decimal(reserved_input) * max(amount(prices[rate]) for rate in ('input', 'write_5m', 'read'))
               + Decimal(maximum) * amount(prices['output'])) / Decimal(1_000_000)
    ledger = BudgetLedger(LEDGER, policy['monthly_usd'], policy['request_usd'])
    if ceiling > amount(policy['request_usd']):
        raise ApiError('Vorsichtige Kostenschätzung überschreitet das Anfragelimit')
    if dry_run:
        return {'dry_run': True, 'model': MODEL_IDS[name], 'images': len(images), 'counted_input_tokens': count,
                'reserved_input_tokens': reserved_input, 'maximum_estimated_usd': str(ceiling),
                'budget': ledger.snapshot()}
    reservation = ledger.reserve(ceiling)
    response = post('messages', payload, token)
    usage = validated_usage(response)
    actual = usage_cost(usage, prices)
    budget = ledger.settle(reservation, actual)
    content = response.get('content')
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise ApiError('Ungültige Modellantwort; gemessene Kosten wurden verbucht')
    text = '\n'.join(block['text'] for block in content if block.get('type') == 'text' and isinstance(block.get('text'), str))
    total = sum(usage[field] for field in FIELDS[:3])
    return {'model': MODEL_IDS[name], 'text': text, 'usage': usage, 'estimated_cost_usd': str(actual),
            'cache_read_fraction': usage['cache_read_input_tokens'] / total if total else None,
            'stop_reason': response.get('stop_reason'), 'budget': budget}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_POLICY)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('login')
    sub.add_parser('status')
    sub.add_parser('budget')
    call = sub.add_parser('ask')
    call.add_argument('--prompt-file', type=Path, required=True)
    call.add_argument('--system-file', type=Path)
    call.add_argument('--model', choices=MODEL_IDS)
    call.add_argument('--max-output-tokens', type=int)
    call.add_argument('--image', type=Path, action='append', default=[],
                      help='PNG/JPEG vor dem Text anhängen (mehrfach; nur mit max_images in der Richtlinie)')
    call.add_argument('--dry-run', action='store_true', help='Kostenlos zählen, keine Modellantwort erzeugen')
    call.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'login':
            ant_command('auth', 'login', capture=False)
            return 0
        policy = load_policy(args.config, require_current=args.command == 'ask')
        if args.command == 'status':
            access_token()
            print(json.dumps({'authenticated': True, 'profile': 'nexogen', 'auth_directory': str(AUTH_DIR),
                              'monthly_usd': policy['monthly_usd'], 'request_usd': policy['request_usd']}))
        elif args.command == 'budget':
            print(json.dumps(BudgetLedger(LEDGER, policy['monthly_usd'], policy['request_usd']).snapshot()))
        else:
            result = ask(args.config, policy, args.prompt_file, args.system_file, args.model,
                         args.max_output_tokens, args.dry_run, args.image)
            if args.json or args.dry_run:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(result['text'])
                print(f"API: {result['model']} · geschätzt {result['estimated_cost_usd']} USD · "
                      f"Cache-Lesen {result['usage']['cache_read_input_tokens']} Tokens", file=sys.stderr)
        return 0
    except (ApiError, BudgetError) as error:
        print(f'ce api: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
        import ce_common as _console
        _console.utf8_console()
    except ImportError:
        pass
    sys.exit(main())
