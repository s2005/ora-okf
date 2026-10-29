-- Export the optimizer statistics (USER_TABLES) of the current schema as JSON,
-- for a later merge into an ora-okf bundle at <okf-dir>/tables/.
-- Target: run connected as the schema owner; reads USER_TABLES only
-- Generated: 2026-10-29
--
-- Usage:
--   sqlplus user@db @table_stats.sql        (or: sql user@db @table_stats.sql)
--   python merge_stats.py --stats-json table_stats_on_<SCHEMA>_<YYYYMMDD_HH24MISS>.json --okf-dir <okf-dir>
--
-- Output, next to the script's working directory:
--   table_stats_on_<SCHEMA>_<YYYYMMDD_HH24MISS>.json  - the statistics document
--   table_stats_on_<SCHEMA>_<YYYYMMDD_HH24MISS>.log   - run log and summary
--
-- Values are optimizer statistics gathered by DBMS_STATS, not exact counts.
-- num_rows / avg_row_len / blocks are null for a table that was never analysed;
-- check last_analyzed before trusting a number.
-- JSON_OBJECT needs Oracle 12.2 or later.
--
-- JSON layout (one table object per line, so no line exceeds LINESIZE):
--   {"metadata":{...},"tables":[
--   {"table_name":"T_X","num_rows":123,...},
--   ...
--   ]}

SET DEFINE ON;
DEFINE report_name=table_stats

COLUMN log_name NEW_VALUE log_name;
SELECT '&report_name._on_' || SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') || '_' ||
       TO_CHAR(SYSDATE, 'YYYYMMDD_HH24MISS') AS log_name
  FROM dual;

SET ECHO OFF FEEDBACK ON LINESIZE 220 PAGESIZE 50000 TIMING ON HEAD ON SERVEROUTPUT OFF;

SPOOL &log_name..log
-- DEFINE stays ON: the spool file names below use &log_name
SET ECHO ON VERIFY OFF TIMING ON DEFINE ON;

-- Read-only script: nothing to roll back, but stop so a broken JSON is not mistaken for a good one
WHENEVER SQLERROR EXIT FAILURE;

-- Summary: how complete and how fresh the statistics are
SELECT COUNT(*)                                  AS tables_total,
       COUNT(last_analyzed)                      AS tables_analyzed,
       COUNT(*) - COUNT(last_analyzed)           AS tables_never_analyzed,
       TO_CHAR(MIN(last_analyzed), 'YYYY-MM-DD') AS oldest_analyzed,
       TO_CHAR(MAX(last_analyzed), 'YYYY-MM-DD') AS newest_analyzed,
       SUM(num_rows)                             AS num_rows_total
  FROM user_tables
 WHERE dropped = 'NO';

SPOOL OFF;

-- JSON document: settings that let nothing but the query rows reach the file
SET ECHO OFF FEEDBACK OFF TIMING OFF HEADING OFF PAGESIZE 0 LINESIZE 32767 VERIFY OFF;
SET TRIMSPOOL ON TRIMOUT ON TAB OFF TERMOUT OFF;
COLUMN line FORMAT A4000;

SPOOL &log_name..json
SELECT line
  FROM (SELECT 1 AS part,
               0 AS rn,
               '{"metadata":' ||
               JSON_OBJECT(
                   'schema'       VALUE SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'),
                   'session_user' VALUE SYS_CONTEXT('USERENV', 'SESSION_USER'),
                   'db_name'      VALUE SYS_CONTEXT('USERENV', 'DB_NAME'),
                   'source'       VALUE 'USER_TABLES',
                   'extracted_at' VALUE TO_CHAR(SYSTIMESTAMP, 'YYYY-MM-DD"T"HH24:MI:SSTZH:TZM'),
                   'table_count'  VALUE c.cnt
                   RETURNING VARCHAR2(4000)
               ) || ',"tables":[' AS line
          FROM (SELECT COUNT(*) AS cnt
                  FROM user_tables
                 WHERE dropped = 'NO') c
        UNION ALL
        SELECT 2 AS part,
               ROW_NUMBER() OVER (ORDER BY t.table_name) AS rn,
               JSON_OBJECT(
                   'table_name'      VALUE t.table_name,
                   'num_rows'        VALUE t.num_rows,
                   'avg_row_len'     VALUE t.avg_row_len,
                   'blocks'          VALUE t.blocks,
                   'sample_size'     VALUE t.sample_size,
                   'last_analyzed'   VALUE TO_CHAR(t.last_analyzed, 'YYYY-MM-DD"T"HH24:MI:SS'),
                   'global_stats'    VALUE t.global_stats,
                   'user_stats'      VALUE t.user_stats,
                   'partitioned'     VALUE t.partitioned,
                   'temporary'       VALUE t.temporary,
                   'iot_type'        VALUE t.iot_type,
                   'tablespace_name' VALUE t.tablespace_name
                   RETURNING VARCHAR2(4000)
               ) ||
               CASE
                   WHEN ROW_NUMBER() OVER (ORDER BY t.table_name) < COUNT(*) OVER () THEN ','
               END AS line
          FROM user_tables t
         WHERE t.dropped = 'NO'
        UNION ALL
        SELECT 3 AS part,
               0 AS rn,
               ']}' AS line
          FROM dual)
 ORDER BY part, rn;
SPOOL OFF;

SET TERMOUT ON HEADING ON PAGESIZE 50000 LINESIZE 220 FEEDBACK ON;

SPOOL &log_name..log APPEND
PROMPT JSON written: &log_name..json
SPOOL OFF;
SET ECHO OFF TIMING OFF FEEDBACK ON DEFINE ON;

PROMPT Table statistics export complete - Log file: &log_name..log
