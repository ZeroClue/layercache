-- Migration: Add probation tracking columns to semantic_cache table
-- Phase 2.1: Multi-tier Caching Hierarchy
-- 
-- This migration is additive and safe to run on existing databases.
-- SQLite doesn't support DROP COLUMN, so rollback requires table recreation.
-- In practice, no rollback is needed as this is additive only.

-- Add probation_count column for tracking validation successes
-- Default 0 means entry starts in probation
ALTER TABLE semantic_cache ADD COLUMN probation_count INTEGER DEFAULT 0;

-- Add intent_hash for fast validation lookups
-- SHA-256 hex digest of normalized query
ALTER TABLE semantic_cache ADD COLUMN intent_hash TEXT;

-- Add provider_cache_valid flag for cache coherency
-- False means provider cache should be skipped for this entry
ALTER TABLE semantic_cache ADD COLUMN provider_cache_valid BOOLEAN DEFAULT 1;

-- Add created_at timestamp for auto-promotion timeout tracking
-- Uses SQLite's strftime for current timestamp
ALTER TABLE semantic_cache ADD COLUMN created_at REAL DEFAULT (strftime('%s', 'now'));

-- Index for probation queries (find entries ready for promotion)
CREATE INDEX IF NOT EXISTS idx_semantic_probation ON semantic_cache(probation_count);

-- Index for intent hash lookups (fast validation)
CREATE INDEX IF NOT EXISTS idx_semantic_intent ON semantic_cache(intent_hash);

-- Index for created_at (auto-promotion timeout queries)
CREATE INDEX IF NOT EXISTS idx_semantic_created ON semantic_cache(created_at);

-- Rollback procedure (if needed):
-- SQLite doesn't support DROP COLUMN, so recreate the table:
-- 
-- CREATE TABLE semantic_cache_new AS SELECT 
--     id, prefix_hash, query_text, query_embedding, response_payload,
--     model, ttl_expires_at
-- FROM semantic_cache;
-- DROP TABLE semantic_cache;
-- ALTER TABLE semantic_cache_new RENAME TO semantic_cache;
