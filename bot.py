#!/usr/bin/env python3
"""Grook mention worker. Python 3.11+, standard library only; Linux/macOS."""
import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
FIELDS = 'author_id,conversation_id,created_at,entities,referenced_tweets,note_tweet,attachments,possibly_sensitive'


def load_env():
    path = HERE / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('\"\''))


def required(name):
    value = os.getenv(name, '').strip()
    if not value:
        raise ValueError(f'Missing {name}; set it in .env or the host environment')
    return value


class ApiError(Exception):
    def __init__(self, status, retry=60):
        super().__init__(f'API HTTP {status}')  # Do not log response bodies or secrets.
        self.status, self.retry = status, retry


def request(method, url, headers, body=None, timeout=45):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**headers, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        reset = exc.headers.get('x-rate-limit-reset', '')
        retry = max(60, int(reset) - int(time.time())) if reset.isdigit() else 60
        raise ApiError(exc.code, retry) from None


def oauth_header(method, url, key, secret, token, token_secret, nonce=None, timestamp=None):
    enc = lambda v: urllib.parse.quote(str(v), safe='~')
    u = urllib.parse.urlsplit(url)
    params = dict(oauth_consumer_key=key, oauth_nonce=nonce or secrets.token_hex(16),
                  oauth_signature_method='HMAC-SHA1', oauth_timestamp=str(timestamp or int(time.time())),
                  oauth_token=token, oauth_version='1.0')
    pairs = sorted((enc(k), enc(v)) for k, v in [*params.items(), *urllib.parse.parse_qsl(u.query)])
    normalized = '&'.join(f'{k}={v}' for k, v in pairs)
    base = '&'.join(map(enc, [method.upper(), f'{u.scheme}://{u.netloc}{u.path}', normalized]))
    signing_key = f'{enc(secret)}&{enc(token_secret)}'
    params['oauth_signature'] = base64.b64encode(hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return 'OAuth ' + ', '.join(f'{enc(k)}="{enc(v)}"' for k, v in sorted(params.items()))


class X:
    def __init__(self):
        self.credentials = [required(n) for n in ('X_API_KEY', 'X_API_SECRET', 'X_ACCESS_TOKEN', 'X_ACCESS_TOKEN_SECRET')]

    def call(self, method, path, query=None, body=None):
        url = 'https://api.x.com/2/' + path
        if query:
            url += '?' + urllib.parse.urlencode(query)
        return request(method, url, {'Authorization': oauth_header(method, url, *self.credentials)}, body)

    def mentions(self, uid, query):
        return self.call('GET', f'users/{uid}/mentions', {'tweet.fields': FIELDS, 'max_results': 100, **query})

    def post(self, post_id):
        try:
            return self.call('GET', f'tweets/{post_id}', {'tweet.fields': FIELDS}).get('data')
        except ApiError as exc:
            if exc.status in (403, 404):
                return None
            raise

    def reply(self, post_id, text):
        return self.call('POST', 'tweets', body={'text': text, 'reply': {'in_reply_to_tweet_id': post_id}})['data']['id']


def text_of(post):
    return (post.get('note_tweet') or post.get('note_post') or {}).get('text') or post.get('text', '')


def refs(post):
    return post.get('referenced_tweets', post.get('referenced_posts', []))


def directed(post, handle, uid):
    if post.get('author_id') == uid or any(r['type'] == 'retweeted' for r in refs(post)):
        return False
    return re.search(r'(?<!\w)@' + re.escape(handle) + r'(?!\w)', text_of(post), re.I) is not None


def command(post, handle):
    stripped = re.sub(r'@' + re.escape(handle) + r'\b', '', text_of(post), flags=re.I).strip().lower()
    if stripped in ('stop', 'unsubscribe', 'opt out', 'opt-out'):
        return 'stop'
    if stripped in ('start', 'subscribe', 'opt in', 'opt-in'):
        return 'start'
    return None


def context(x, mention):
    posts, missing, seen = [], [], set()
    current = mention
    for _ in range(8):
        if not current or current['id'] in seen:
            break
        seen.add(current['id'])
        posts.append(current)
        parent = next((r['id'] for r in refs(current) if r['type'] == 'replied_to'), None)
        if not parent:
            break
        current = x.post(parent)
        if current is None:
            missing.append(parent)
    root = mention.get('conversation_id')
    extra = [r['id'] for p in list(posts) for r in refs(p) if r['type'] == 'quoted']
    if root:
        extra.append(root)
    for post_id in extra[:9]:
        if post_id not in seen:
            seen.add(post_id)
            p = x.post(post_id)
            if p:
                posts.append(p)
            else:
                missing.append(post_id)
    return {'mention_id': mention['id'], 'missing_post_ids': missing,
            'posts': [{'id': p['id'], 'author_id': p.get('author_id'),
                       'text': text_of(p)[:12000], 'references': refs(p),
                       'url': f"https://x.com/i/status/{p['id']}",
                       'has_unseen_media': bool(p.get('attachments')),
                       'possibly_sensitive': p.get('possibly_sensitive', False)} for p in posts]}


def valid_text(text):
    if not isinstance(text, str) or not text.strip() or re.search(r'@[A-Za-z0-9_]+', text):
        return False
    if len(re.findall(r'\$[A-Za-z]+', text)) > 1:
        return False
    # Conservative upper bound: every non-ASCII character counts as 2; URLs as 23.
    without_urls = re.sub(r'https?://\S+', 'x' * 23, text)
    weight = sum(1 if ord(c) < 128 else 2 for c in without_urls)
    return weight <= 280 and not any(ord(c) < 32 and c not in '\n\t' for c in text)


def draft(ctx, handle):
    prompt = (HERE / 'voice.txt').read_text()
    payload = {'model': os.getenv('XAI_MODEL') or 'grok-4.20-0309-non-reasoning',
               'instructions': prompt + f'\nYour handle is @{handle}. Current UTC time: {dt.datetime.now(dt.timezone.utc).isoformat()}',
               'input': [{'role': 'user', 'content': json.dumps(ctx)}],
               'tools': [{'type': 'web_search'}, {'type': 'x_search', 'enable_image_understanding': True}],
               'max_output_tokens': 1200, 'max_turns': 3, 'store': False}
    response = request('POST', 'https://api.x.ai/v1/responses',
                       {'Authorization': 'Bearer ' + required('XAI_API_KEY')}, payload, timeout=120)
    if response.get('status') not in (None, 'completed'):
        raise ValueError('Incomplete model response')
    output = ''.join(c.get('text', '') for item in response.get('output', []) if item.get('type') == 'message'
                     for c in item.get('content', []) if c.get('type') == 'output_text').strip()
    output = re.sub(r'^```(?:json)?\s*|\s*```$', '', output)
    answer = json.loads(output)
    if answer.get('action') == 'skip':
        return None
    text = answer.get('text', '').strip()
    if answer.get('action') != 'reply' or not valid_text(text):
        raise ValueError('Model output did not pass reply validation')
    return text


def database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, author TEXT, payload TEXT,
      status TEXT DEFAULT 'pending', text TEXT, reply_id TEXT, attempts INTEGER DEFAULT 0,
      next_try REAL DEFAULT 0, updated REAL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS optouts (author TEXT PRIMARY KEY);
    ''')
    return db


def meta(db, key, value=None):
    if value is None:
        row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))


def ingest(db, x, uid):
    since, start = meta(db, 'since'), meta(db, 'start')
    if not since and not start:
        # First launch starts now, never replies to a historical backlog.
        start = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        meta(db, 'start', start)
        db.commit()
        return
    query = {'since_id': since} if since else {'start_time': start}
    found, token = [], None
    while True:
        page = x.mentions(uid, {**query, **({'pagination_token': token} if token else {})})
        if page.get('errors'):
            raise ValueError('Mention API returned partial errors; cursor unchanged')
        found.extend(page.get('data', []))
        token = page.get('meta', {}).get('next_token')
        if not token:
            break
    # Advance only after every page is safely collected and queued, in one transaction.
    with db:
        for p in found:
            db.execute('INSERT OR IGNORE INTO jobs(id,author,payload) VALUES(?,?,?)',
                       (p['id'], p.get('author_id', ''), json.dumps(p)))
        if found:
            meta(db, 'since', max((p['id'] for p in found), key=int))


def set_status(db, jid, status, text=None, reply_id=None):
    with db:
        db.execute('UPDATE jobs SET status=?, text=COALESCE(?,text), reply_id=COALESCE(?,reply_id), updated=? WHERE id=?',
                   (status, text, reply_id, time.time(), jid))


def process(db, x, uid, handle, live, generate=draft):
    now = time.time()
    # Commands are handled before any queued answers, so STOP takes immediate precedence.
    jobs = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY length(id),id").fetchall()
    for job in jobs:
        p = json.loads(job['payload'])
        if not directed(p, handle, uid):
            set_status(db, job['id'], 'skipped')
            continue
        cmd = command(p, handle)
        if cmd:
            with db:
                if cmd == 'stop':
                    db.execute('INSERT OR IGNORE INTO optouts VALUES (?)', (job['author'],))
                else:
                    db.execute('DELETE FROM optouts WHERE author=?', (job['author'],))
            set_status(db, job['id'], 'command')
    used = db.execute("SELECT count(*) FROM jobs WHERE status IN ('sent','sending','uncertain','draft') AND updated>?", (now-86400,)).fetchone()[0]
    budget = max(0, int(os.getenv('MAX_REPLIES_PER_DAY', '50')) - used)
    for job in db.execute("SELECT * FROM jobs WHERE status='pending' AND next_try<=? ORDER BY length(id),id", (now,)).fetchall():
        if budget <= 0:
            break
        jid, author = job['id'], job['author']
        if db.execute('SELECT 1 FROM optouts WHERE author=?', (author,)).fetchone():
            set_status(db, jid, 'opted_out')
            continue
        p = json.loads(job['payload'])
        created = dt.datetime.fromisoformat(p['created_at'].replace('Z', '+00:00')).timestamp()
        if now-created > 86400:
            set_status(db, jid, 'expired')
            continue
        recent = db.execute("SELECT 1 FROM jobs WHERE author=? AND status IN ('sent','sending','uncertain','draft') AND updated>?", (author, now-300)).fetchone()
        if recent:
            continue
        try:
            fresh = x.post(jid)
            if not fresh or not directed(fresh, handle, uid):
                set_status(db, jid, 'unavailable')
                continue
            text = generate(context(x, fresh), handle)
            if not text:
                set_status(db, jid, 'skipped')
                continue
            if not valid_text(text):
                raise ValueError('Invalid reply')
            if not live:
                set_status(db, jid, 'draft', text)
                print(json.dumps({'mention': jid, 'draft': text}, ensure_ascii=False), flush=True)
            else:
                if not x.post(jid):
                    set_status(db, jid, 'unavailable')
                    continue
                # Persist BEFORE sending. Never automatically retry an ambiguous write.
                set_status(db, jid, 'sending', text)
                try:
                    reply_id = x.reply(jid, text)
                except Exception:
                    set_status(db, jid, 'uncertain')
                    raise
                set_status(db, jid, 'sent', reply_id=reply_id)
                print(json.dumps({'mention': jid, 'reply': reply_id}), flush=True)
            budget -= 1
        except Exception as exc:
            state = db.execute('SELECT status FROM jobs WHERE id=?', (jid,)).fetchone()[0]
            if state == 'pending':
                attempts = job['attempts'] + 1
                delay = max(min(3600, 60 * 2**attempts), getattr(exc, 'retry', 0))
                with db:
                    db.execute('UPDATE jobs SET attempts=?, next_try=?, status=? WHERE id=?',
                               (attempts, time.time()+delay, 'failed' if attempts >= 5 else 'pending', jid))
            print(json.dumps({'mention': jid, 'error_type': type(exc).__name__,
                              'http_status': getattr(exc, 'status', None)}), flush=True)
            if isinstance(exc, ApiError) and exc.status in (401, 402, 403, 429):
                raise


def main():
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true', help='Show stored drafts and status, without calling APIs')
    args = parser.parse_args()
    live = os.getenv('X_BOT_LIVE', 'false').lower() == 'true'
    if live and os.getenv('X_AI_REPLY_APPROVED', 'false').lower() != 'true':
        raise ValueError('Live mode requires X approval; keep X_BOT_LIVE=false until approved')
    data = Path(os.getenv('DATA_DIR', str(HERE / 'data')))
    data.mkdir(parents=True, exist_ok=True)
    # One instance sharing this persistent directory. Not a distributed lock.
    with (data / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = database(data / 'bot.sqlite3')
        if args.status:
            for row in db.execute('SELECT id,status,text,reply_id,attempts FROM jobs ORDER BY length(id) DESC,id DESC LIMIT 50'):
                print(json.dumps(dict(row), ensure_ascii=False))
            return
        x = X()
        required('XAI_API_KEY')
        me = x.call('GET', 'users/me')['data']
        expected = required('X_BOT_HANDLE').lstrip('@')
        if me['username'].lower() != expected.lower():
            raise ValueError('X credentials belong to a different account than X_BOT_HANDLE')
        account = meta(db, 'account')
        if account and account != me['id']:
            raise ValueError('This database belongs to a different X account; use a new DATA_DIR')
        meta(db, 'account', me['id'])
        db.commit()
        print(f"Grook @{expected}: {'LIVE' if live else 'PREVIEW'}", flush=True)
        while True:
            delay = max(30, int(os.getenv('POLL_SECONDS', '60')))
            try:
                ingest(db, x, me['id'])
                process(db, x, me['id'], expected, live)
            except Exception as exc:
                print(json.dumps({'error_type': type(exc).__name__, 'http_status': getattr(exc, 'status', None)}), flush=True)
                if args.once or isinstance(exc, ApiError) and exc.status in (401, 402, 403):
                    raise SystemExit(1)
                delay = max(delay, getattr(exc, 'retry', 60))
            if args.once:
                break
            time.sleep(delay)


if __name__ == '__main__':
    main()
