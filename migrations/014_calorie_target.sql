-- Each client's daily calorie target, set by their trainer.
--
-- On users rather than per group: a calorie target is a property of the person,
-- not of the room they post in, and someone in two programs still has one body.
-- NULL means no target, which is the signal to fall back to flagging unusually
-- large single meals instead.
ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_calorie_target INTEGER;
