-- Stores indefinite recurring weekly check-in preferences per user.
-- day_of_week: 0 = Monday … 6 = Sunday
CREATE TABLE IF NOT EXISTS user_weekly_schedules (
    user_id     INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    day_of_week INT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
