-- Shared DPO tables (Postgres). Apply once; used by slackbot (insert feedback_events) and langgraph_autobot.

CREATE TABLE IF NOT EXISTS feedback_events (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    slack_team_id TEXT,
    channel_id TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    user_id TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    rejected_text TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    reaction TEXT NOT NULL DEFAULT 'thumbsdown',
    CONSTRAINT feedback_events_dedupe UNIQUE (channel_id, message_ts, user_id)
);

CREATE TABLE IF NOT EXISTS teacher_labels (
    id SERIAL PRIMARY KEY,
    feedback_id INTEGER NOT NULL REFERENCES feedback_events (id) ON DELETE CASCADE,
    chosen_text TEXT NOT NULL,
    teacher_model TEXT NOT NULL,
    teacher_run_id TEXT NOT NULL,
    labeled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    qc_status TEXT NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_teacher_labels_feedback ON teacher_labels (feedback_id);
CREATE INDEX IF NOT EXISTS idx_teacher_labels_qc ON teacher_labels (qc_status);
CREATE INDEX IF NOT EXISTS idx_feedback_events_created ON feedback_events (created_at);

CREATE TABLE IF NOT EXISTS training_runs (
    id SERIAL PRIMARY KEY,
    run_id UUID NOT NULL,
    week_start DATE NOT NULL,
    week_end DATE NOT NULL,
    hub_revision TEXT,
    endpoint_previous_revision TEXT,
    status TEXT NOT NULL DEFAULT 'started',
    artifact_uri TEXT,
    row_count INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS training_run_rows (
    training_run_id INTEGER NOT NULL REFERENCES training_runs (id) ON DELETE CASCADE,
    feedback_id INTEGER NOT NULL REFERENCES feedback_events (id) ON DELETE CASCADE,
    PRIMARY KEY (training_run_id, feedback_id)
);
