import json
import sqlite3

SCHEMA = '''
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    caller TEXT,
    pbx_session_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds INTEGER,
    read INTEGER NOT NULL DEFAULT 0,
    caller_name TEXT,
    summary TEXT,
    urgency TEXT,
    category TEXT,
    transcript TEXT NOT NULL,
    usage TEXT NOT NULL,
    cost TEXT,
    total_usd REAL
);
CREATE INDEX IF NOT EXISTS calls_started_at ON calls (started_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''

# Everything save_call() writes. `read` is deliberately absent: only mark_read() sets it.
CALL_COLUMNS = (
    'id', 'session_id', 'caller', 'pbx_session_id', 'started_at', 'ended_at', 'duration_seconds',
    'caller_name', 'summary', 'urgency', 'category', 'transcript', 'usage', 'cost', 'total_usd',
)
JSON_COLUMNS = ('transcript', 'usage', 'cost')
LIST_COLUMNS = (
    'id', 'caller', 'started_at', 'duration_seconds', 'read', 'caller_name', 'summary', 'urgency', 'category',
    'total_usd',
)

# Fixed SQL fragments, picked by name: nothing from a request is ever spliced into SQL.
FILTERS = {
    None: '1',
    'unread': 'read = 0',
    'urgent': "urgency = 'cao'",
    'spam': "category = 'spam'",
}
# started_at is ISO local time in one fixed UTC offset, so its date prefix is the local day.
PERIODS = {
    'day': 'substr(started_at, 1, 10)',
    'week': "strftime('%Y-W%W', substr(started_at, 1, 10))",
    'month': 'substr(started_at, 1, 7)',
}


class Database:
    """Calls and settings in one SQLite file.

    ponytail: synchronous queries on the event loop — well under a millisecond at
    one-subscriber scale. Upgrade path: Postgres + an async driver when this goes multi-tenant.
    """

    def __init__(self, path):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.executescript(SCHEMA)

    def close(self):
        self._conn.close()

    # --- calls -------------------------------------------------------------

    def save_call(self, call):
        cost = call.get('cost')
        row = {column: call.get(column) for column in CALL_COLUMNS}
        row['transcript'] = json.dumps(call.get('transcript') or [], ensure_ascii=False)
        row['usage'] = json.dumps(call.get('usage') or {})
        row['cost'] = json.dumps(cost) if cost else None
        row['total_usd'] = cost['total_usd'] if cost else None
        columns = ', '.join(CALL_COLUMNS)
        placeholders = ', '.join(f':{column}' for column in CALL_COLUMNS)
        updates = ', '.join(f'{column} = excluded.{column}' for column in CALL_COLUMNS if column != 'id')
        with self._conn:
            self._conn.execute(
                f'INSERT INTO calls ({columns}) VALUES ({placeholders}) ON CONFLICT (id) DO UPDATE SET {updates}', row,
            )

    def get_call(self, call_id):
        row = self._conn.execute('SELECT * FROM calls WHERE id = ?', (call_id,)).fetchone()
        if row is None:
            return None
        call = dict(row)
        for column in JSON_COLUMNS:
            call[column] = json.loads(call[column]) if call[column] else None
        call['read'] = bool(call['read'])
        return call

    def list_calls(self, before=None, limit=30, only=None):
        """Newest first. Page by passing the last row's started_at as `before`."""
        if only not in FILTERS:
            raise ValueError(f'unknown filter: {only!r}')
        rows = self._conn.execute(
            f'SELECT {", ".join(LIST_COLUMNS)} FROM calls'
            f' WHERE {FILTERS[only]} AND (:before IS NULL OR started_at < :before)'
            ' ORDER BY started_at DESC LIMIT :limit',
            {'before': before, 'limit': limit},
        ).fetchall()
        return [{**dict(row), 'read': bool(row['read'])} for row in rows]

    def unsummarized_calls(self, since):
        """Finished calls started since `since` that never got their summary (the server stopped first), oldest first."""
        rows = self._conn.execute(
            'SELECT id FROM calls WHERE summary IS NULL AND ended_at IS NOT NULL AND started_at >= ? ORDER BY started_at',
            (since,),
        ).fetchall()
        return [self.get_call(row['id']) for row in rows]

    def mark_read(self, call_id, read=True):
        with self._conn:
            return self._conn.execute('UPDATE calls SET read = ? WHERE id = ?', (int(read), call_id)).rowcount > 0

    def stats(self, by='day'):
        if by not in PERIODS:
            raise ValueError(f'unknown period: {by!r}')
        rows = self._conn.execute(
            f'SELECT {PERIODS[by]} AS period, COUNT(*) AS total,'
            " COALESCE(SUM(urgency = 'cao'), 0) AS urgent, COALESCE(SUM(category = 'spam'), 0) AS spam,"
            ' ROUND(COALESCE(SUM(total_usd), 0), 6) AS total_usd'
            ' FROM calls GROUP BY period ORDER BY period DESC LIMIT 60',
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_older_than(self, cutoff):
        with self._conn:
            ids = [row['id'] for row in self._conn.execute('SELECT id FROM calls WHERE started_at < ?', (cutoff,))]
            self._conn.execute('DELETE FROM calls WHERE started_at < ?', (cutoff,))
        return ids

    # --- settings ----------------------------------------------------------

    def get_settings(self):
        return {row['key']: json.loads(row['value']) for row in self._conn.execute('SELECT key, value FROM settings')}

    def set_settings(self, values):
        """Upserts each key; a None value removes the key, which restores its default."""
        with self._conn:
            for key, value in values.items():
                if value is None:
                    self._conn.execute('DELETE FROM settings WHERE key = ?', (key,))
                else:
                    self._conn.execute(
                        'INSERT INTO settings (key, value) VALUES (?, ?)'
                        ' ON CONFLICT (key) DO UPDATE SET value = excluded.value',
                        (key, json.dumps(value, ensure_ascii=False)),
                    )
