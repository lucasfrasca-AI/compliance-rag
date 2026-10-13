-- The corpus and mapping versions an assessment was computed against
-- (#153, ADR-0034; CONTEXT.md: Corpus version, Mapping version).
--
-- A version is a git tag resolved when the graph was loaded, plus the commit
-- the tag pointed at. A tag can move; a commit can't, so the SHA is what makes
-- the assessment reproducible and the tag is the name it was known by.
-- `corpus_version` and `mapping_version` keep holding the tag, and are NULL
-- when no tag existed at load time: the assessment is then untagged, named by
-- its SHA alone. A load is never refused for want of a tag.
--
-- Every new assessment names both commits. Rows written before this have
-- neither (tests only: no deployed ledger holds one), so the constraint is
-- NOT VALID: it is enforced on every insert and update from here on and does
-- not look back. Reading such a row back is refused by the adapter, naming it.

BEGIN;

ALTER TABLE assessment ADD COLUMN IF NOT EXISTS corpus_sha  TEXT;
ALTER TABLE assessment ADD COLUMN IF NOT EXISTS mapping_sha TEXT;

ALTER TABLE assessment ALTER COLUMN corpus_version  DROP NOT NULL;
ALTER TABLE assessment ALTER COLUMN mapping_version DROP NOT NULL;

-- A tag is a name, or absent: never an empty string standing in for untagged.
ALTER TABLE assessment DROP CONSTRAINT IF EXISTS assessment_corpus_tag;
ALTER TABLE assessment ADD CONSTRAINT assessment_corpus_tag
    CHECK (corpus_version IS NULL OR btrim(corpus_version) <> '');

ALTER TABLE assessment DROP CONSTRAINT IF EXISTS assessment_mapping_tag;
ALTER TABLE assessment ADD CONSTRAINT assessment_mapping_tag
    CHECK (mapping_version IS NULL OR btrim(mapping_version) <> '');

-- Full commit ids, lowercase: the form `git rev-parse` prints.
ALTER TABLE assessment DROP CONSTRAINT IF EXISTS assessment_corpus_sha;
ALTER TABLE assessment ADD CONSTRAINT assessment_corpus_sha
    CHECK (corpus_sha ~ '^[0-9a-f]{40}$');

ALTER TABLE assessment DROP CONSTRAINT IF EXISTS assessment_mapping_sha;
ALTER TABLE assessment ADD CONSTRAINT assessment_mapping_sha
    CHECK (mapping_sha ~ '^[0-9a-f]{40}$');

ALTER TABLE assessment DROP CONSTRAINT IF EXISTS assessment_names_both_commits;
ALTER TABLE assessment ADD CONSTRAINT assessment_names_both_commits
    CHECK (corpus_sha IS NOT NULL AND mapping_sha IS NOT NULL) NOT VALID;

COMMIT;
