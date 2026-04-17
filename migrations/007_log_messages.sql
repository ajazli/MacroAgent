-- Maps a bot analysis reply message back to the log entry it represents.
-- Used so users can reply to correct an inaccurate meal analysis.
CREATE TABLE IF NOT EXISTS log_messages (
    log_id     INTEGER  NOT NULL REFERENCES logs(id) ON DELETE CASCADE,
    chat_id    BIGINT   NOT NULL,
    message_id BIGINT   NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
