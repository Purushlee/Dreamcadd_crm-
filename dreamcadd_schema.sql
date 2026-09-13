-- =====================================================================
-- DreamCadd Lead Management System - PostgreSQL Schema
-- Architecture: Excel (MD upload) -> n8n -> PostgreSQL -> WhatsApp Cloud API
-- =====================================================================

-- Clean re-run support (drop in dependency order)
DROP TABLE IF EXISTS whatsapp_messages CASCADE;
DROP TABLE IF EXISTS followups CASCADE;
DROP TABLE IF EXISTS lead_assignments CASCADE;
DROP TABLE IF EXISTS leads CASCADE;
DROP TABLE IF EXISTS courses CASCADE;
DROP TABLE IF EXISTS users CASCADE;

-- =====================================================================
-- 1. USERS  (MD + Telecallers)
-- =====================================================================
CREATE TABLE users (
    user_id      SERIAL PRIMARY KEY,
    name         VARCHAR(150) NOT NULL,
    phone        VARCHAR(20) UNIQUE,
    role         VARCHAR(20) NOT NULL CHECK (role IN ('MD', 'TELECALLER', 'ADMIN')),
    status       VARCHAR(20) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'INACTIVE')),
    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMP NOT NULL DEFAULT NOW()
);

-- =====================================================================
-- 2. COURSES
-- =====================================================================
CREATE TABLE courses (
    course_id    SERIAL PRIMARY KEY,
    course_name  VARCHAR(150) NOT NULL,
    duration     VARCHAR(50),
    fee          NUMERIC(10, 2),
    description  TEXT,
    status       VARCHAR(20) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'INACTIVE')),
    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMP NOT NULL DEFAULT NOW()
);

-- =====================================================================
-- 3. LEADS
--    phone is the natural de-dup key from the Excel import workflow
--    (New / Existing / Duplicate / Changed logic in n8n keys off this).
-- =====================================================================
CREATE TABLE leads (
    lead_id       SERIAL PRIMARY KEY,
    name          VARCHAR(150) NOT NULL,
    phone         VARCHAR(20) NOT NULL UNIQUE,
    email         VARCHAR(150),
    education     VARCHAR(100),
    college       VARCHAR(150),
    interested_course_id INTEGER REFERENCES courses(course_id) ON DELETE SET NULL,
    status        VARCHAR(30) NOT NULL DEFAULT 'NEW'
                  CHECK (status IN ('NEW', 'ASSIGNED', 'CONTACTED', 'INTERESTED',
                                     'NOT_INTERESTED', 'CONVERTED', 'DROPPED')),
    assigned_to   INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    source_batch  VARCHAR(100),          -- e.g. which Excel upload this came from
    created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_leads_phone ON leads(phone);
CREATE INDEX idx_leads_assigned_to ON leads(assigned_to);
CREATE INDEX idx_leads_status ON leads(status);

-- =====================================================================
-- 4. LEAD_ASSIGNMENTS
--    Full history of who a lead was assigned to, over time.
--    (leads.assigned_to is the current caller; this table is the audit trail)
-- =====================================================================
CREATE TABLE lead_assignments (
    assignment_id SERIAL PRIMARY KEY,
    lead_id       INTEGER NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
    caller_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    assigned_date TIMESTAMP NOT NULL DEFAULT NOW(),
    status        VARCHAR(20) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'REASSIGNED', 'CLOSED')),
    created_at    TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_assignments_lead ON lead_assignments(lead_id);
CREATE INDEX idx_assignments_caller ON lead_assignments(caller_id);

-- =====================================================================
-- 5. FOLLOWUPS
-- =====================================================================
CREATE TABLE followups (
    followup_id   SERIAL PRIMARY KEY,
    lead_id       INTEGER NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
    caller_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    followup_date TIMESTAMP NOT NULL DEFAULT NOW(),
    status        VARCHAR(30) NOT NULL DEFAULT 'PENDING'
                  CHECK (status IN ('PENDING', 'DONE', 'NO_RESPONSE', 'RESCHEDULED')),
    remarks       TEXT,
    created_at    TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_followups_lead ON followups(lead_id);
CREATE INDEX idx_followups_caller ON followups(caller_id);

-- =====================================================================
-- 6. WHATSAPP_MESSAGES
--    direction: OUTBOUND = system/telecaller -> student, INBOUND = student reply
-- =====================================================================
CREATE TABLE whatsapp_messages (
    message_id   SERIAL PRIMARY KEY,
    lead_id      INTEGER NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
    message      TEXT NOT NULL,
    direction    VARCHAR(10) NOT NULL CHECK (direction IN ('INBOUND', 'OUTBOUND')),
    status       VARCHAR(20) NOT NULL DEFAULT 'SENT'
                  CHECK (status IN ('SENT', 'DELIVERED', 'READ', 'FAILED', 'RECEIVED')),
    timestamp    TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_wa_messages_lead ON whatsapp_messages(lead_id);
CREATE INDEX idx_wa_messages_timestamp ON whatsapp_messages(timestamp);

-- =====================================================================
-- Trigger helper: auto-update updated_at on row change
-- =====================================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_courses_updated_at
    BEFORE UPDATE ON courses
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_leads_updated_at
    BEFORE UPDATE ON leads
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- Sample seed data (optional - comment out if not needed)
-- =====================================================================
-- INSERT INTO users (name, phone, role) VALUES
--     ('MD Name', '9000000000', 'MD'),
--     ('Kuberan', '9800000001', 'TELECALLER');

-- INSERT INTO courses (course_name, duration, fee, description) VALUES
--     ('Civil Engineering Certification', '6 months', 15000.00, 'Sample course');
