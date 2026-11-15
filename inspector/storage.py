"""Private profile-local records; redact before persistence, reject unsafe paths."""
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import uuid
import time
from .redaction import redact


def _safe_parents(path):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError('symlink in state path')


def _safe_file(path):
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise ValueError('unsafe state file')
    return info.st_dev, info.st_ino


class Store:
    def __init__(self, directory, *, redactor=None, retention_days=0, clock=time.time):
        if type(retention_days) is not int or retention_days < 0:
            raise ValueError('retention_days must be a non-negative integer')
        self.retention_days = retention_days
        self.clock = clock
        self.redactor = redactor
        self.directory = Path(directory).absolute()
        _safe_parents(self.directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_uid != os.getuid():
            raise ValueError('state directory is not owned by current user')
        self.directory.chmod(0o700)
        self.path = self.directory / 'calls.sqlite3'
        self._check_paths()
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('unsafe database')
            os.fchmod(fd, 0o600)
            self.identity = info.st_dev, info.st_ino
        finally:
            os.close(fd)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
            # Separate metadata preserves compatibility with older two-column writers.
            db.execute('CREATE TABLE IF NOT EXISTS call_times (id TEXT PRIMARY KEY, created_at REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS call_times_created ON call_times(created_at)')
            # Unknown legacy age gets one migration-time grace period, never reset.
            db.execute('INSERT OR IGNORE INTO call_times SELECT id, ? FROM calls', (self.clock(),))
            self._purge(db)

    def _check_paths(self):
        _safe_parents(self.directory)
        identity = _safe_file(self.path)
        if hasattr(self, 'identity') and identity != self.identity:
            raise ValueError('database replaced after opening')
        for suffix in ('-journal', '-wal', '-shm'):
            _safe_file(Path(str(self.path) + suffix))

    @contextmanager
    def connect(self):
        self._check_paths()
        db = sqlite3.connect(self.path, timeout=5)
        try:
            self._check_paths()
            with db:
                yield db
        finally:
            db.close()

    def _purge(self, db):
        if not self.retention_days:
            return 0
        cutoff = self.clock() - self.retention_days * 86400
        count = db.execute('DELETE FROM calls WHERE id IN (SELECT id FROM call_times WHERE created_at <= ?)',
                           (cutoff,)).rowcount
        db.execute('DELETE FROM call_times WHERE created_at <= ?', (cutoff,))
        return count

    def purge_expired(self):
        with self.connect() as db:
            return self._purge(db)

    def capture(self, tool, args, result, *, channel_id, user_id, session_id, kind='tool', event_key=None):
        identity = (uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([session_id, str(channel_id), kind, event_key])).hex
                    if event_key else uuid.uuid4().hex)
        record = dict(id=identity, kind=kind, tool=tool, args=args, result=result,
                      channel_id=str(channel_id), user_id=str(user_id), session_id=session_id,
                      message_id=None)
        record = redact(record, self.redactor)
        if not isinstance(record, dict):
            raise TypeError('redaction must preserve the record mapping')
        with self.connect() as db:
            self._purge(db)
            cursor = db.execute('INSERT OR IGNORE INTO calls VALUES (?, ?)', (record['id'], json.dumps(record)))
            if not cursor.rowcount:
                return None
            db.execute('INSERT INTO call_times VALUES (?, ?)', (record['id'], self.clock()))
        return record

    def get(self, record_id):
        with self.connect() as db:
            self._purge(db)
            row = db.execute('SELECT body FROM calls WHERE id=?', (record_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def bind(self, record_id, message_id):
        record = self.get(record_id)
        if record is None:
            raise KeyError(record_id)
        record['message_id'] = str(message_id)
        with self.connect() as db:
            db.execute('UPDATE calls SET body=? WHERE id=?', (json.dumps(record), record_id))
