-- Owners beyond the one named in the environment.
--
-- The env var stays the root owner: it cannot be removed by a command, so there
-- is always a way back in if the flags below are ever set wrong. Everyone else
-- is promoted at runtime with /addowner, which means naming them by @handle
-- instead of hunting for a numeric id and redeploying.
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_owner BOOLEAN NOT NULL DEFAULT FALSE;
