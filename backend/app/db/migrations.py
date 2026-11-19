"""Versioned database migrations, applied automatically at server start.

Each migration runs once, in order, inside a transaction. A Postgres advisory lock makes concurrent
server instances wait for each other instead of migrating twice. To change the schema, append a new
(version, sql) entry; never edit an applied one.
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

log = logging.getLogger(__name__)

EMBEDDING_DIM = 384  # BAAI/bge-small-en-v1.5; a different model needs a new migration

MIGRATIONS: list[tuple[int, str]] = [
    (1, f"""
        CREATE EXTENSION IF NOT EXISTS vector;

        CREATE TABLE conversations (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            channel             TEXT NOT NULL CHECK (channel IN ('web', 'voice', 'whatsapp')),
            token_hash          TEXT NOT NULL,              -- the visitor's session token, hashed
            status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'escalated', 'closed')),
            page_url            TEXT NOT NULL DEFAULT '',
            turn_count          INT NOT NULL DEFAULT 0,
            escalation_reason   TEXT,
            escalation_summary  TEXT,
            escalated_at        TIMESTAMPTZ,
            started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_message_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX conversations_recent ON conversations (last_message_at DESC);

        CREATE TABLE messages (
            id                 BIGSERIAL PRIMARY KEY,
            conversation_id    UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role               TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
            text               TEXT NOT NULL DEFAULT '',
            tool_calls         JSONB NOT NULL DEFAULT '[]',
            tool_call_id       TEXT NOT NULL DEFAULT '',
            tool_name          TEXT NOT NULL DEFAULT '',
            client_message_id  TEXT,                        -- retry key sent by the widget
            reply_to           BIGINT,                      -- assistant reply -> the user message id
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX messages_conversation ON messages (conversation_id, id);
        -- A retried message (same client id) can never be stored or answered twice.
        CREATE UNIQUE INDEX messages_client_id ON messages (conversation_id, client_message_id)
            WHERE client_message_id IS NOT NULL;

        CREATE TABLE leads (
            conversation_id  UUID PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
            name             TEXT NOT NULL DEFAULT '',
            email            TEXT NOT NULL DEFAULT '',
            phone            TEXT NOT NULL DEFAULT '',
            company          TEXT NOT NULL DEFAULT '',
            job_title        TEXT NOT NULL DEFAULT '',
            need             TEXT NOT NULL DEFAULT '',
            company_size     TEXT NOT NULL DEFAULT '',
            current_systems  TEXT NOT NULL DEFAULT '',
            budget           TEXT NOT NULL DEFAULT '',
            timeline         TEXT NOT NULL DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'qualified', 'not_a_fit')),
            missing          TEXT[] NOT NULL DEFAULT '{{}}',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX leads_recent ON leads (updated_at DESC);

        CREATE TABLE call_requests (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            conversation_id  UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            preferred_times  TEXT NOT NULL,
            timezone         TEXT NOT NULL DEFAULT '',
            notes            TEXT NOT NULL DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'requested',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- Knowledge the assistant may use. audience separates public answers from internal material
        -- (Phase 3); the public chat only ever searches audience = 'public'.
        CREATE TABLE kb_entries (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug          TEXT NOT NULL UNIQUE,
            kind          TEXT NOT NULL CHECK (kind IN ('company', 'service', 'faq', 'policy')),
            audience      TEXT NOT NULL DEFAULT 'public' CHECK (audience IN ('public', 'internal')),
            title         TEXT NOT NULL,
            body          TEXT NOT NULL,
            active        BOOLEAN NOT NULL DEFAULT true,
            content_hash  TEXT NOT NULL,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE kb_chunks (
            id         BIGSERIAL PRIMARY KEY,
            entry_id   UUID NOT NULL REFERENCES kb_entries(id) ON DELETE CASCADE,
            ord        INT NOT NULL,
            heading    TEXT NOT NULL DEFAULT '',
            content    TEXT NOT NULL,
            -- Words in the heading (the entry's title) count more than words in the text.
            tsv        TSVECTOR GENERATED ALWAYS AS (
                           setweight(to_tsvector('english', heading), 'A') ||
                           setweight(to_tsvector('english', content), 'D')) STORED,
            embedding  VECTOR({EMBEDDING_DIM})
        );
        CREATE INDEX kb_chunks_tsv ON kb_chunks USING GIN (tsv);
        CREATE INDEX kb_chunks_embedding ON kb_chunks USING hnsw (embedding vector_cosine_ops);

        -- Audit trail: guard actions, tool calls, escalations, errors.
        CREATE TABLE events (
            id               BIGSERIAL PRIMARY KEY,
            conversation_id  UUID REFERENCES conversations(id) ON DELETE CASCADE,
            kind             TEXT NOT NULL,
            data             JSONB NOT NULL DEFAULT '{{}}',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX events_conversation ON events (conversation_id, id);
        CREATE INDEX events_kind ON events (kind, created_at DESC);
    """),
    (2, """
        -- Phase 2: bookings, background jobs, CRM links, conversation summaries.
        CREATE EXTENSION IF NOT EXISTS btree_gist;

        ALTER TABLE conversations
            ADD COLUMN visitor_timezone TEXT NOT NULL DEFAULT '',
            ADD COLUMN summary          TEXT NOT NULL DEFAULT '',
            ADD COLUMN summary_at       TIMESTAMPTZ;

        ALTER TABLE leads
            ADD COLUMN crm_contact_id TEXT NOT NULL DEFAULT '',
            ADD COLUMN crm_deal_id    TEXT NOT NULL DEFAULT '',
            ADD COLUMN crm_note_id    TEXT NOT NULL DEFAULT '',
            ADD COLUMN crm_synced_at  TIMESTAMPTZ;

        CREATE TABLE bookings (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            reference          TEXT NOT NULL UNIQUE,          -- shown to the visitor, e.g. DC-7K3M9Q
            conversation_id    UUID REFERENCES conversations(id) ON DELETE SET NULL,
            consultant_id      TEXT NOT NULL,
            calendar_id        TEXT NOT NULL,
            start_at           TIMESTAMPTZ NOT NULL,
            end_at             TIMESTAMPTZ NOT NULL CHECK (end_at > start_at),
            status             TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'cancelled')),
            name               TEXT NOT NULL,
            email              TEXT NOT NULL,
            phone              TEXT NOT NULL DEFAULT '',
            visitor_timezone   TEXT NOT NULL DEFAULT '',
            notes              TEXT NOT NULL DEFAULT '',
            calendar_event_id  TEXT NOT NULL,                 -- fixed id, so a retried create cannot duplicate
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            cancelled_at       TIMESTAMPTZ,
            -- No two confirmed calls can overlap on one calendar, whatever the application does.
            CONSTRAINT bookings_no_overlap EXCLUDE USING gist
                (calendar_id WITH =, tstzrange(start_at, end_at) WITH &&) WHERE (status = 'confirmed')
        );
        CREATE UNIQUE INDEX bookings_one_per_conversation ON bookings (conversation_id) WHERE status = 'confirmed';
        CREATE INDEX bookings_upcoming ON bookings (calendar_id, start_at) WHERE status = 'confirmed';

        CREATE TABLE jobs (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            kind           TEXT NOT NULL,
            key            TEXT NOT NULL UNIQUE,              -- idempotency key: one job per key, ever
            ref            TEXT NOT NULL DEFAULT '',          -- what it is about (booking or conversation id)
            payload        JSONB NOT NULL DEFAULT '{}',
            status         TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
            attempts       INT NOT NULL DEFAULT 0,
            max_attempts   INT NOT NULL DEFAULT 6,
            run_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            locked_until   TIMESTAMPTZ,
            last_error     TEXT NOT NULL DEFAULT '',
            dispatched_at  TIMESTAMPTZ,                       -- handed to QStash
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at    TIMESTAMPTZ
        );
        CREATE INDEX jobs_due ON jobs (run_at) WHERE status IN ('pending', 'running');
        CREATE INDEX jobs_ref ON jobs (ref, kind) WHERE status = 'pending';
    """),
    (3, """
        -- Video meeting link of a discovery call (Google Meet), filled in when the calendar event is made.
        ALTER TABLE bookings ADD COLUMN meeting_url TEXT NOT NULL DEFAULT '';
    """),
    (4, """
        -- Phase 3: existing clients and their projects and tickets; staff accounts and who may see which
        -- client; internal documents scoped to a client.
        CREATE TABLE clients (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug             TEXT NOT NULL UNIQUE,
            name             TEXT NOT NULL,
            tier             TEXT NOT NULL DEFAULT 'sme' CHECK (tier IN ('sme', 'enterprise')),
            account_manager  TEXT NOT NULL DEFAULT '',          -- consultant id from content/team.yaml
            active           BOOLEAN NOT NULL DEFAULT true,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE client_contacts (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            client_id   UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            email       TEXT NOT NULL UNIQUE CHECK (email = lower(email)),
            name        TEXT NOT NULL DEFAULT '',
            role        TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE projects (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            client_id        UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            name             TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'in_progress'
                             CHECK (status IN ('planning', 'in_progress', 'on_hold', 'completed')),
            phase            TEXT NOT NULL DEFAULT '',
            next_milestone   TEXT NOT NULL DEFAULT '',
            milestone_date   DATE,
            client_update    TEXT NOT NULL DEFAULT '',          -- written for the client; nothing internal
            owner            TEXT NOT NULL DEFAULT '',          -- consultant id
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX projects_client ON projects (client_id);
        CREATE TABLE tickets (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            reference        TEXT NOT NULL UNIQUE,              -- shown to the client, e.g. TK-7K3M9Q
            client_id        UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            conversation_id  UUID REFERENCES conversations(id) ON DELETE SET NULL,
            contact_email    TEXT NOT NULL,
            summary          TEXT NOT NULL,
            details          TEXT NOT NULL DEFAULT '',
            priority         TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
            category         TEXT NOT NULL DEFAULT 'other',
            status           TEXT NOT NULL DEFAULT 'open'
                             CHECK (status IN ('open', 'in_progress', 'waiting', 'resolved', 'closed')),
            external_key     TEXT NOT NULL DEFAULT '',          -- e.g. SUP-12 in Jira
            external_url     TEXT NOT NULL DEFAULT '',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX tickets_client ON tickets (client_id, created_at DESC);

        ALTER TABLE conversations DROP CONSTRAINT conversations_channel_check;
        ALTER TABLE conversations ADD CONSTRAINT conversations_channel_check
            CHECK (channel IN ('web', 'voice', 'whatsapp', 'team'));
        ALTER TABLE conversations
            ADD COLUMN client_id      UUID REFERENCES clients(id) ON DELETE SET NULL,   -- set after email check
            ADD COLUMN verified_email TEXT NOT NULL DEFAULT '',
            ADD COLUMN verified_at    TIMESTAMPTZ,
            ADD COLUMN staff_user_id  UUID;                                            -- team conversations

        CREATE TABLE staff_users (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email          TEXT NOT NULL UNIQUE CHECK (email = lower(email)),
            name           TEXT NOT NULL,
            role           TEXT NOT NULL CHECK (role IN ('admin', 'consultant')),
            consultant_id  TEXT NOT NULL DEFAULT '',            -- links to content/team.yaml
            password_hash  TEXT NOT NULL,
            active         BOOLEAN NOT NULL DEFAULT true,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_login_at  TIMESTAMPTZ
        );
        -- Which clients a consultant may see. Admins see all clients.
        CREATE TABLE staff_client_access (
            user_id    UUID NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
            client_id  UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, client_id)
        );
        CREATE TABLE staff_sessions (
            token_hash    TEXT PRIMARY KEY,
            user_id       UUID NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at    TIMESTAMPTZ NOT NULL,
            last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        ALTER TABLE conversations ADD CONSTRAINT conversations_staff_user_fk
            FOREIGN KEY (staff_user_id) REFERENCES staff_users(id) ON DELETE CASCADE;

        ALTER TABLE kb_entries DROP CONSTRAINT kb_entries_kind_check;
        ALTER TABLE kb_entries ADD CONSTRAINT kb_entries_kind_check CHECK (kind IN (
            'company', 'service', 'faq', 'policy',
            'proposal', 'case_study', 'sop', 'template', 'technical', 'notes'));
        -- An internal document about one client is only searchable by staff allowed to see that client.
        ALTER TABLE kb_entries ADD COLUMN client_id UUID REFERENCES clients(id) ON DELETE CASCADE;
        ALTER TABLE kb_entries ADD CONSTRAINT kb_public_has_no_client
            CHECK (audience = 'internal' OR client_id IS NULL);
    """),
    # Phase 4: phone and browser voice calls. One row per call; the transcript is the conversation's messages.
    (5, """
        CREATE TABLE calls (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            call_sid         TEXT UNIQUE,                       -- Twilio's id; NULL for browser calls
            request_key      TEXT UNIQUE,                       -- outbound: idempotency key of the request
            direction        TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound', 'web')),
            from_number      TEXT NOT NULL DEFAULT '',
            to_number        TEXT NOT NULL DEFAULT '',
            conversation_id  UUID REFERENCES conversations(id) ON DELETE SET NULL,
            purpose          TEXT NOT NULL DEFAULT '',          -- outbound: why we call (said in the greeting)
            contact_name     TEXT NOT NULL DEFAULT '',
            requested_by     UUID REFERENCES staff_users(id) ON DELETE SET NULL,
            status           TEXT NOT NULL DEFAULT 'queued',    -- queued, ringing, in_progress, completed, ...
            outcome          TEXT NOT NULL DEFAULT '',          -- transferred, transfer_missed, time_limit, ...
            transfer_to      TEXT NOT NULL DEFAULT '',          -- consultant id
            error            TEXT NOT NULL DEFAULT '',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at       TIMESTAMPTZ,
            ended_at         TIMESTAMPTZ,
            duration_s       INT
        );
        CREATE INDEX calls_created ON calls (created_at DESC);
        CREATE INDEX calls_conversation ON calls (conversation_id);
    """),
    # Phase 5: content edited in the admin dashboard. The files in content/ are the defaults; a row here
    # replaces one of them (or adds an internal document). Every saved version is kept.
    (6, """
        CREATE TABLE content_files (
            path        TEXT PRIMARY KEY,                    -- e.g. faqs.yaml, internal/sop-x.md
            body        TEXT NOT NULL,
            version     INT NOT NULL,
            deleted     BOOLEAN NOT NULL DEFAULT false,      -- an internal document removed in the dashboard
            updated_by  UUID REFERENCES staff_users(id) ON DELETE SET NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE content_history (
            id          BIGSERIAL PRIMARY KEY,
            path        TEXT NOT NULL,
            version     INT NOT NULL,
            body        TEXT NOT NULL,
            deleted     BOOLEAN NOT NULL DEFAULT false,
            note        TEXT NOT NULL DEFAULT '',
            updated_by  UUID REFERENCES staff_users(id) ON DELETE SET NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (path, version)
        );
    """),
]

_LOCK_ID = 8_402_117  # arbitrary constant for pg_advisory_lock


async def migrate(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_ID)
        try:
            await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                               "version INT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for version, sql in MIGRATIONS:
                if version in done:
                    continue
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
                log.info("applied database migration %d", version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_ID)


def normalize_dsn(dsn: str) -> str:
    """Accept connection strings exactly as hosted Postgres providers (e.g. Neon) show them:
    drop `channel_binding` (unknown to asyncpg) and use Neon's direct host instead of the pooler
    (PgBouncer in transaction mode breaks the migration lock and prepared statements)."""
    parts = urlsplit(dsn)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "channel_binding"]
    netloc = parts.netloc
    if "-pooler." in netloc and "neon.tech" in netloc:
        netloc = netloc.replace("-pooler.", ".", 1)
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), parts.fragment))


async def connect(dsn: str) -> asyncpg.Pool:
    # Idle connections are recycled after a minute: Neon suspends idle databases and drops them.
    pool = await asyncpg.create_pool(normalize_dsn(dsn), min_size=1, max_size=10, timeout=10,
                                     max_inactive_connection_lifetime=60)
    await migrate(pool)
    return pool
