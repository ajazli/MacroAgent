-- FitBot initial schema migration

CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS logs (
    id         SERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date       DATE NOT NULL DEFAULT CURRENT_DATE,
    type       TEXT NOT NULL CHECK (type IN ('meal', 'weight', 'steps', 'workout', 'water')),
    data       JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS logs_user_id_date_idx ON logs (user_id, date);
CREATE INDEX IF NOT EXISTS logs_type_idx ON logs (type);
CREATE INDEX IF NOT EXISTS logs_date_idx ON logs (date);
