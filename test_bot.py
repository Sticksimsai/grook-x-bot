import datetime as dt
import json
import unittest
from unittest.mock import patch
import bot


def post(id='10', author='user', text='@grook is this true?', parent=None):
    p = {'id': id, 'author_id': author, 'text': text,
         'created_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'conversation_id': parent or id}
    if parent:
        p['referenced_tweets'] = [{'id': parent, 'type': 'replied_to'}]
    return p


class FakeX:
    def __init__(self, posts=None, pages=None, fail_send=False):
        self.posts = posts or {}
        self.pages = iter(pages or [])
        self.queries, self.sent = [], []
        self.fail_send = fail_send

    def post(self, id):
        return self.posts.get(id)

    def mentions(self, uid, query):
        self.queries.append(query)
        page = next(self.pages)
        if isinstance(page, Exception):
            raise page
        return page

    def reply(self, id, text):
        self.sent.append((id, text))
        if self.fail_send:
            raise TimeoutError()
        return '999'


class BotTests(unittest.TestCase):
    def setUp(self):
        self.db = bot.database(':memory:')

    def tearDown(self):
        self.db.close()

    def add(self, p):
        self.db.execute('INSERT INTO jobs(id,author,payload) VALUES(?,?,?)', (p['id'], p['author_id'], json.dumps(p)))
        self.db.commit()

    def state(self, id='10'):
        return self.db.execute('SELECT status FROM jobs WHERE id=?', (id,)).fetchone()[0]

    def test_oauth_reference_signature(self):
        # OAuth 1.0 published photos.example.net example.
        header = bot.oauth_header('GET',
            'http://photos.example.net/photos?file=vacation.jpg&size=original',
            'dpf43f3p2l4k3l03', 'kd94hf93k423kf44',
            'nnch734d00sl2jdk', 'pfkkdhi9sl3r4s00',
            nonce='kllo9940pd9333jh', timestamp=1191242096)
        self.assertIn('oauth_signature="tR3%2BTy81lMeYAr%2FFid0kMTYa%2FWM%3D"', header)

    def test_first_run_starts_now(self):
        x = FakeX()
        bot.ingest(self.db, x, 'bot')
        self.assertIsNotNone(bot.meta(self.db, 'start'))
        self.assertEqual(x.queries, [])

    def test_pagination_and_dedupe(self):
        bot.meta(self.db, 'since', '1'); self.db.commit()
        x = FakeX(pages=[{'data': [post('30')], 'meta': {'next_token': 'next'}}, {'data': [post('20'), post('30')]}])
        bot.ingest(self.db, x, 'bot')
        self.assertEqual(bot.meta(self.db, 'since'), '30')
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)
        self.assertEqual(x.queries[1]['pagination_token'], 'next')
        self.assertEqual(x.queries[1]['since_id'], '1')

    def test_pagination_failure_does_not_advance(self):
        bot.meta(self.db, 'since', '1'); self.db.commit()
        x = FakeX(pages=[{'data': [post()], 'meta': {'next_token': 'next'}}, bot.ApiError(429)])
        with self.assertRaises(bot.ApiError):
            bot.ingest(self.db, x, 'bot')
        self.assertEqual(bot.meta(self.db, 'since'), '1')
        self.assertEqual(self.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_context_parent_quote_root_and_missing(self):
        m = post('10', parent='9')
        m['conversation_id'] = '1'
        p = post('9', text='claim', parent='8')
        p['referenced_tweets'].append({'id': '7', 'type': 'quoted'})
        ctx = bot.context(FakeX({'9': p, '7': post('7'), '1': post('1')}), m)
        self.assertEqual([p['id'] for p in ctx['posts']], ['10', '9', '7', '1'])
        self.assertEqual(ctx['missing_post_ids'], ['8'])

    def test_direct_mentions_only(self):
        self.assertTrue(bot.directed(post(text='@Grook'), 'grook', 'bot'))
        self.assertFalse(bot.directed(post(text='@grookfan'), 'grook', 'bot'))
        self.assertFalse(bot.directed(post(author='bot'), 'grook', 'bot'))
        p = post(); p['referenced_tweets'] = [{'id': '1', 'type': 'retweeted'}]
        self.assertFalse(bot.directed(p, 'grook', 'bot'))

    def test_validation(self):
        self.assertTrue(bot.valid_text('Short answer.'))
        self.assertTrue(bot.valid_text('Source: https://example.com/' + 'x'*300))
        self.assertFalse(bot.valid_text('x'*281))
        self.assertFalse(bot.valid_text('界'*141))
        self.assertFalse(bot.valid_text('Tag @someone'))
        self.assertFalse(bot.valid_text('$BTC and $ETH'))

    def test_preview_never_posts_or_replays(self):
        p = post(); self.add(p); x = FakeX({'10': p})
        bot.process(self.db, x, 'bot', 'grook', False, lambda *_: 'A direct answer.')
        bot.process(self.db, x, 'bot', 'grook', True, lambda *_: 'Another answer.')
        self.assertEqual(self.state(), 'draft')
        self.assertEqual(x.sent, [])

    def test_live_reply_once(self):
        p = post(); self.add(p); x = FakeX({'10': p})
        for _ in range(2):
            bot.process(self.db, x, 'bot', 'grook', True, lambda *_: 'Answer.')
        self.assertEqual(x.sent, [('10', 'Answer.')])
        self.assertEqual(self.state(), 'sent')

    def test_ambiguous_send_is_not_retried(self):
        p = post(); self.add(p); x = FakeX({'10': p}, fail_send=True)
        for _ in range(2):
            bot.process(self.db, x, 'bot', 'grook', True, lambda *_: 'Answer.')
        self.assertEqual(len(x.sent), 1)
        self.assertEqual(self.state(), 'uncertain')

    def test_stop_prevents_queued_answers(self):
        self.add(post()); self.add(post('11', text='@grook stop'))
        bot.process(self.db, FakeX(), 'bot', 'grook', True, lambda *_: self.fail('Must not generate'))
        self.assertEqual(self.state(), 'opted_out')
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM optouts').fetchone())

    def test_model_skip(self):
        p = post(); self.add(p)
        bot.process(self.db, FakeX({'10': p}), 'bot', 'grook', True, lambda *_: None)
        self.assertEqual(self.state(), 'skipped')

    def test_deleted_mention_not_answered(self):
        self.add(post())
        bot.process(self.db, FakeX(), 'bot', 'grook', True, lambda *_: self.fail('Must not generate'))
        self.assertEqual(self.state(), 'unavailable')

    def test_failed_generation_stays_retryable(self):
        p = post(); self.add(p)
        def fail(*_): raise ValueError()
        bot.process(self.db, FakeX({'10': p}), 'bot', 'grook', True, fail)
        row = self.db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['attempts'], 1)
        self.assertGreater(row['next_try'], 0)

    def test_daily_cap(self):
        p = post(); self.add(p)
        with patch.dict('os.environ', {'MAX_REPLIES_PER_DAY': '0'}):
            bot.process(self.db, FakeX({'10': p}), 'bot', 'grook', True, lambda *_: self.fail('Budget exhausted'))
        self.assertEqual(self.state(), 'pending')

    def test_long_post_text(self):
        self.assertEqual(bot.text_of({'text': 'short', 'note_tweet': {'text': 'full'}}), 'full')


if __name__ == '__main__':
    unittest.main()
