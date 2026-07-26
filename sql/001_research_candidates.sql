-- NormanAI-research staging table.
--
-- This table belongs to the research repo. It is NOT the workspace D1 job
-- queue and does not pretend to be — promoting a row into the CRM still goes
-- through NormanAI-crm-core's crm_intake.py, which owns Notion writes and hard
-- dedup.
--
-- Apply with: supabase migration or psql -f sql/001_research_candidates.sql

create table if not exists research_candidates (
    identity_key        text primary key,
    company             text not null,
    website             text,
    linkedin            text,
    crunchbase          text,
    x_handle            text,
    one_liner           text,
    founders            text,
    founded             text,
    hq                  text,
    industries          text,

    last_funding_date   text,
    last_funding_usd    numeric,
    last_funding_type   text,
    total_funding_usd   numeric,
    num_rounds          integer,
    investors           text,

    -- Why research thinks this is a lead.
    nyc_proof           text,
    signals             jsonb not null default '[]'::jsonb,
    signal_notes        text,
    signal_strength     integer not null default 0,
    score_notes         jsonb not null default '[]'::jsonb,
    source_urls         jsonb not null default '[]'::jsonb,
    lane                text,

    -- Handoff bookkeeping.
    first_seen          timestamptz not null default now(),
    last_seen           timestamptz not null default now(),
    emitted_at          timestamptz,
    promoted_page_id    text,

    constraint research_candidates_strength_range
        check (signal_strength between 0 and 100)
);

create index if not exists research_candidates_strength_idx
    on research_candidates (signal_strength desc);

create index if not exists research_candidates_pending_idx
    on research_candidates (last_seen desc)
    where emitted_at is null;

comment on table research_candidates is
    'Discovery staging for NormanAI-research. Promotion into Norman CRM Core happens via NormanAI-crm-core/scripts/crm_intake.py — never write Notion from here.';

comment on column research_candidates.signal_strength is
    'Discovery confidence 0-100. NOT Fit Score — crm-core scoring lane owns that.';
