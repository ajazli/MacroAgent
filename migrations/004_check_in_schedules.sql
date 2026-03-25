-- Weekly check-in schedule per user
CREATE TABLE IF NOT EXISTS check_in_schedules (
    id              SERIAL PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scheduled_date  DATE NOT NULL,
    prompted_at     TIMESTAMP WITH TIME ZONE,
    completed_at    TIMESTAMP WITH TIME ZONE,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (user_id, scheduled_date)
);

CREATE INDEX IF NOT EXISTS check_in_schedules_date_idx ON check_in_schedules(scheduled_date);
CREATE INDEX IF NOT EXISTS check_in_schedules_user_idx ON check_in_schedules(user_id);
