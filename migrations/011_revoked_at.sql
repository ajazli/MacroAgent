-- When a group or person was deliberately blocked, as opposed to simply never
-- having been approved.
--
-- Both states are approved = FALSE, but they should behave differently: a group
-- the bot has just been added to is worth telling the owner about, while one the
-- owner revoked five minutes ago is not — announcing it again is noise about a
-- decision they already made.
ALTER TABLE groups ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE users  ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMP WITH TIME ZONE;
