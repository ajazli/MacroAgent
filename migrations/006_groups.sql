-- Stores every group the bot has been added to.
-- clocker_topic_id is resolved lazily and cached here.
CREATE TABLE IF NOT EXISTS groups (
    chat_id          BIGINT PRIMARY KEY,
    title            TEXT,
    clocker_topic_id BIGINT,
    registered_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
