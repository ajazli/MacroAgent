-- Approval flags for groups and users.
--
-- Migrations re-run on every boot, so this must never re-approve something the
-- owner has since revoked. The two-step trick below is idempotent:
--
--   ADD COLUMN ... DEFAULT TRUE  gives every row that already exists TRUE, and
--                                does nothing at all on later boots.
--   ALTER COLUMN ... SET DEFAULT FALSE  then makes every NEW row default to FALSE.
--
-- Net effect: everything present when this first ships keeps working exactly as
-- it does today, and anything that turns up afterwards needs approving.
ALTER TABLE groups ADD COLUMN IF NOT EXISTS approved BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE groups ALTER COLUMN approved SET DEFAULT FALSE;

ALTER TABLE users  ADD COLUMN IF NOT EXISTS approved BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users  ALTER COLUMN approved SET DEFAULT FALSE;
