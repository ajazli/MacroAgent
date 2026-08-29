-- Personal-training session balances, kept as a ledger rather than two counters.
--
-- Each row is one event with a signed delta: +N when the trainer sells a
-- package, -1 each time a session is used. The balance is SUM(delta).
--
-- A ledger costs no more than a total/used pair and gives history, "when did
-- they last train", and an undo that is simply deleting the newest row.
--
-- Scoped by chat_id as well as user_id: the groups here are one per client, so
-- the group is the trainer-client pair, and someone in two programs gets two
-- independent balances rather than one merged number.
CREATE TABLE IF NOT EXISTS pt_sessions (
    id         SERIAL PRIMARY KEY,
    chat_id    BIGINT  NOT NULL,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delta      INTEGER NOT NULL,
    note       TEXT,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pt_sessions_chat_user ON pt_sessions (chat_id, user_id);
