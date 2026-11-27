-- SPDX-License-Identifier: BUSL-1.1
-- Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
-- 2026.09-ga.5 re-domain fixup (#2932): LLM Manager (llm_manager_db).
-- workers.address: a worker registered under the old domain is unreachable
-- after the move (whole-host rule). In-stack workers use Docker names and are
-- never touched; REMOTE workers must still be re-joined from the LLM console.
BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';
INSERT INTO rz_out (line) SELECT 'before llm-manager.workers ' || count(*) FROM workers WHERE pg_temp.rz_left(address);
WITH c AS (
  UPDATE workers SET address = pg_temp.rz_text(address) WHERE pg_temp.rz_has(address) RETURNING 1)
INSERT INTO rz_out (line) SELECT 'changed llm-manager.workers ' || count(*) FROM c;
INSERT INTO rz_out (line) SELECT 'after llm-manager.workers ' || count(*) FROM workers WHERE pg_temp.rz_left(address);
SELECT line FROM rz_out ORDER BY ord;
\if :apply
COMMIT;
\else
ROLLBACK;
\endif
