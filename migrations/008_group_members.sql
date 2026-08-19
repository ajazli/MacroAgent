-- Roster of which users belong to which group chat.
--
-- Members can ask about each other's logged data, so this table is the boundary
-- that keeps that sharing inside a single group: every lookup filters on
-- chat_id, and a user never seen in a chat is invisible there.
--
-- Populated whenever someone speaks in a group; see bot._on_group_message.
CREATE TABLE IF NOT EXISTS group_members (
    chat_id   BIGINT  NOT NULL,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_seen TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (chat_id, user_id)
);
-- No extra index needed: the primary key leads with chat_id.

-- The name shown in Telegram, which is how people refer to each other and is
-- often nothing like the username we store on users.name.
ALTER TABLE group_members ADD COLUMN IF NOT EXISTS display_name TEXT;

-- Backfill from meals already analysed in each chat, so existing groups have a
-- roster on the first boot after this ships rather than after everyone speaks.
INSERT INTO group_members (chat_id, user_id)
SELECT DISTINCT lm.chat_id, l.user_id
FROM log_messages lm
JOIN logs l ON l.id = lm.log_id
ON CONFLICT (chat_id, user_id) DO NOTHING;
