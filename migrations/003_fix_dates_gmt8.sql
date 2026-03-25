-- Migration 003: Correct log dates from UTC to GMT+8 (Singapore Time).
-- Railway servers run in UTC. Any logs created before this fix may have
-- the wrong date if they were inserted near midnight SGT.
-- We recompute the date from the created_at timestamp converted to SGT.

UPDATE logs
SET date = (created_at AT TIME ZONE 'Asia/Singapore')::date
WHERE date != (created_at AT TIME ZONE 'Asia/Singapore')::date;
