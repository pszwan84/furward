"""Atomic backend receipt identity and event sequence, sharing the file-owner lock."""

import json
import time

from guide.store import Store, encoded


class BackendStore(Store):
    def __init__(self, path):
        super().__init__(path)
        try:
            self.db.execute("CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            self.db.commit()
        except BaseException:
            self.close()
            raise

    def save(self, state, kind, data, command=None, asr=None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (encoded(state),))
            event_id = self.db.execute("INSERT INTO events(time,kind,data) VALUES(?,?,?)",
                                       (time.time(), kind, encoded(data))).lastrowid
            if command:
                self.db.execute("INSERT INTO commands VALUES(?,?)", command)
            task = state.get("task")
            if task and task.get("request_id"):
                self.db.execute("INSERT OR REPLACE INTO receipts VALUES(?,?)",
                                (task["request_id"], encoded(task)))
            if asr:
                self.db.execute("INSERT INTO asr VALUES(?)", (asr,))
        return event_id

    def receipt(self, key):
        row = self.db.execute("SELECT data FROM receipts WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
