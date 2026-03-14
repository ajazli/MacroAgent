-- Migration 002: Remove restrictive type CHECK constraint to support
-- exercise types (exercise_pushup, exercise_situp, exercise_plank,
-- exercise_run, exercise_jog) and personal best types (pb_pushup,
-- pb_situp, pb_2_4km). Application layer controls allowed types.

ALTER TABLE logs DROP CONSTRAINT IF EXISTS logs_type_check;
