"""Shared schema for local SQLite and Cloudflare SQLite storage."""
MIGRATIONS = [(1, """
CREATE TABLE users(id INTEGER PRIMARY KEY, timezone TEXT NOT NULL DEFAULT 'Asia/Qyzylorda', default_bank INTEGER);
CREATE TABLE banks(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE categories(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('income','expense')), system TEXT, archived INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id,system));
CREATE TABLE aliases(user_id INTEGER NOT NULL REFERENCES users(id), word TEXT NOT NULL, bank_id INTEGER REFERENCES banks(id), category_id INTEGER REFERENCES categories(id), PRIMARY KEY(user_id,word), CHECK((bank_id IS NULL) != (category_id IS NULL)));
CREATE TABLE operations(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL CHECK(kind IN ('income','expense','opening','transfer','adjustment')), amount INTEGER NOT NULL, day TEXT NOT NULL, bank_id INTEGER NOT NULL REFERENCES banks(id), target_bank INTEGER REFERENCES banks(id), category_id INTEGER REFERENCES categories(id), rate TEXT, cashback INTEGER NOT NULL DEFAULT 0, payout_month TEXT, deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX operations_user_day ON operations(user_id,day);
CREATE UNIQUE INDEX one_payout ON operations(user_id,bank_id,payout_month) WHERE payout_month IS NOT NULL AND deleted=0;
CREATE TABLE rates(user_id INTEGER NOT NULL REFERENCES users(id), bank_id INTEGER NOT NULL REFERENCES banks(id), category_id INTEGER NOT NULL REFERENCES categories(id), month TEXT NOT NULL, rate TEXT NOT NULL, PRIMARY KEY(user_id,bank_id,category_id,month));
CREATE TABLE dialogs(user_id INTEGER PRIMARY KEY REFERENCES users(id), payload TEXT NOT NULL);
CREATE TABLE events(user_id INTEGER NOT NULL REFERENCES users(id), event_key TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(user_id,event_key));
CREATE TABLE reminders(user_id INTEGER NOT NULL REFERENCES users(id), month TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,month));
"""), (2, """
ALTER TABLE events ADD COLUMN created_at TEXT;
UPDATE events SET created_at = strftime('%Y-%m-%dT%H:%M:%S+00:00','now');
"""), (3, """
ALTER TABLE users ADD COLUMN reminder_time TEXT;
CREATE TABLE daily_reminders(user_id INTEGER NOT NULL REFERENCES users(id), day TEXT NOT NULL, PRIMARY KEY(user_id,day));
"""), (4, """
ALTER TABLE users ADD COLUMN guide_completed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN cashback_intro_shown INTEGER NOT NULL DEFAULT 0;
UPDATE users SET guide_completed=1, cashback_intro_shown=1 WHERE EXISTS (SELECT 1 FROM banks WHERE banks.user_id=users.id);
"""), (5, """
CREATE INDEX events_created_at ON events(created_at);
CREATE INDEX daily_reminders_day ON daily_reminders(day);
""")]
