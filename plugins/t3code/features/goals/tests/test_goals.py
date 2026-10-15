"""Replay real persisted goal sources without model calls or agent turns."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest

spec = importlib.util.spec_from_file_location('goal_state', Path(__file__).parents[1] / 'state.py')
assert spec and spec.loader
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


class Goals(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY, model_config TEXT);
            CREATE TABLE state_meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                display_content TEXT, display_kind TEXT, timestamp REAL, active INTEGER DEFAULT 1);''')
        self.db.execute("INSERT INTO sessions VALUES ('thread', '{}')")

    def tearDown(self):
        self.db.close()

    def message(self, role, text, at, **kw):
        self.db.execute('INSERT INTO messages(session_id,role,content,timestamp,display_content,display_kind) VALUES (?,?,?,?,?,?)',
                        ('thread', role, text, at, kw.get('display_content'), kw.get('display_kind')))

    def test_every_provider_uses_same_persisted_goal_and_lifecycle(self):
        for provider in ('openai-codex', 'anthropic', 'claude-cli', 'muse-code', 'antigravity'):
            self.db.execute('UPDATE sessions SET model_config=?', (json.dumps({'provider': provider}),))
            for status in ('active', 'paused', 'done', 'cleared'):
                self.db.execute('INSERT OR REPLACE INTO state_meta VALUES (?,?)',
                    ('goal:thread', json.dumps({'goal': 'Ship the real thing', 'status': status, 'created_at': 4})))
                goal = state.read_goal(self.db, 'thread')
                self.assertIsNone(goal) if status == 'cleared' else self.assertEqual(goal['status'], status)

    def test_native_goal_survives_long_chat_and_model_switch(self):
        self.message('user', '/goal first objective', 1)
        self.message('assistant', 'Goal set: first objective', 2)
        self.message('user', '/goal second objective', 3)
        self.message('assistant', 'Goal set: second objective', 4)
        for i in range(600):
            self.message('user', 'continue', 5+i)
            self.message('assistant', 'Working on it', 5+i)
        self.db.execute('UPDATE sessions SET model_config=?', (json.dumps({'provider': 'antigravity'}),))
        self.assertEqual(state.read_goal(self.db, 'thread')['objective'], 'second objective')
        self.message('user', '/goal clear', 700)
        self.message('assistant', 'Goal cleared: second objective', 701)
        self.assertIsNone(state.read_goal(self.db, 'thread'))

    def test_no_prose_or_unconfirmed_command_inference(self):
        self.message('assistant', 'Goal set: do not trust model prose', 1)
        self.message('user', '/goal unconfirmed', 2)
        self.assertIsNone(state.read_goal(self.db, 'thread'))
        self.message('assistant', 'Goal set: a different objective', 3)
        self.assertIsNone(state.read_goal(self.db, 'thread'))

    def test_status_recovery_and_newer_clear_tombstone(self):
        self.message('user', '/claude:goal status', 5)
        self.message('assistant', 'Goal active: Ship it (3 turns)', 6)
        self.assertEqual(state.read_goal(self.db, 'thread')['checks'], 3)
        self.db.execute('INSERT INTO state_meta VALUES (?, ?)',
            ('goal:thread', json.dumps({'goal': 'Ship it', 'status': 'cleared', 'created_at': 7})))
        self.assertIsNone(state.read_goal(self.db, 'thread'))
        with self.assertRaises(LookupError):
            state.read_goal(self.db, 'foreign-thread')


if __name__ == '__main__':
    unittest.main()
