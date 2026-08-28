# ora-okf

Export an Oracle schema as an [OKF](#what-is-an-okf-bundle) Markdown bundle, rewriting every schema name on the way out.

The problem it solves: schema documentation is useful to share, but the schema it was extracted from is usually named after an environment, a project code, or a person -- `APP_PROD_OWNER`, `SVC_MIGRATION_2019`, `JSMITH_DEV_COPY`. Those names should not travel with the documentation. `ora-okf` reads the physical schema and writes a bundle that consistently calls it something else, then proves the physical name is gone before it exits.

Renaming is not a search-and-replace over the output. It is applied to the extracted model -- owners, foreign key references, synonym targets, materialized view masters -- and to free text such as view definitions, PL/SQL source, column defaults, check predicates and comments, in a single simultaneous pass. A post-write audit then reads the produced bytes back and fails the run if any renamed name survived.

## Install

```bash
pip install -e .
```

Or, for development:

```bash
uv venv
uv pip install -e ".[dev]"
```

Requires Python 3.10+ and Oracle 19c or later. Connections use python-oracledb in thin mode, so no Oracle Instant Client installation is needed.

## Quick start

Two input files. Credentials, as a `KEY=value` file (see `.env.example`):

```env
DB_USER=APP_PROD_OWNER
DB_PASSWORD=...
DB_DSN=db.example.com:1521/ORCLPDB1
```

And a mapping, as YAML or JSON (see `examples/schema-map.yaml`):

```yaml
version: 1
unmapped: error
schemas:
  APP_PROD_OWNER: APP
  REF_DATA_PROD: REF
```

Then:

```bash
ora-okf --env-file oracle.env --mapping schema-map.yaml --okf-dir out/okf
```

```text
OKF bundle written
  schema     : APP_PROD_OWNER exported as APP
  bundle dir : out/okf
  files      : 214 (211 object concepts)
  renaming   : APP_PROD_OWNER -> APP, REF_DATA_PROD -> REF (1883 in-text occurrence(s))
  leak audit : clean
```

## Options

Every option is named; there are no positional arguments.

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--okf-dir PATH` | required | Directory to write the bundle into. Must be empty or a previously exported bundle. |
| `--env-file PATH` | — | `KEY=value` credentials file. Missing values fall back to the process environment. |
| `--schema NAME` | `SCHEMA`, then `DB_USER` | Schema to extract. |
| `--mapping PATH` | — | YAML or JSON schema mapping. Without it, nothing is renamed. |
| `--no-schema-qualifier` | off | Render `resource:` values as a bare object name instead of `<SCHEMA>.<OBJECT>`. |
| `--no-timestamp` | off | Omit the extraction timestamp from concept frontmatter and `log.md`, so an unchanged schema re-exports byte-identically. Use it for a bundle committed to a repository. |
| `--no-fail-on-leak` | off | Report, rather than fail, when a renamed name survives into the bundle. |
| `--include-data` | off | Add a row count and a bounded row sample to each table concept. Needs `SELECT` on the tables. |
| `--sample-rows N` | `5` | Maximum sample rows per table. `0` keeps row counts and omits the samples. |
| `--dry-run` | off | Render and audit in memory, report, write nothing. |
| `--validate-only` | off | Validate credentials and mapping, then exit. Does not connect. |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. Logs go to stderr. |
| `--log-file PATH` | — | Also write a `DEBUG`-level log to this file. |
| `--version` | — | Print the version and exit. |

Exit codes: `0` success, `1` configuration/connection/extraction/write error, `2` invalid command line, `3` a renamed physical schema name survived into the bundle.

## The mapping file

The mapping file drives renaming and nothing else. It is deliberately separate from the credentials file: credentials say which database to read, the mapping says what the documentation is allowed to call things.

| Key | Required | Meaning |
| --- | -------- | ------- |
| `schemas` | yes | Physical schema name to published name. Keys are matched case-insensitively. |
| `unmapped` | no | What to do with a referenced schema that has no entry: `keep` (default), `redact`, or `error`. |
| `redacted_name` | no | The placeholder used by `unmapped: redact`. Defaults to `EXTERNAL`. |
| `version` | no | Format version. Currently `1`. |

A published name is substituted into `COMMENT ON` statements, `resource:` identifiers and PL/SQL source, so it must be a legal unquoted Oracle identifier: a letter, then letters, digits, or `_ $ #`. Anything else is rejected when the file loads rather than corrupting the bundle.

Two validation rules are worth knowing about:

- **Unknown top-level keys are rejected.** Writing `schema:` for `schemas:` would otherwise produce a bundle that renames nothing, and you would only find out from the audit much later.
- **Chained renames are rejected.** Mapping `A -> B` alongside `B -> C` has no well-defined answer for what an `A` reference should become. Renaming runs as one simultaneous pass so ordering cannot matter, and the loader refuses the mapping that would make the question observable. An identity entry (`A -> A`) is allowed, and means "leave this one alone".

### Finding out which schemas need an entry

You usually do not know up front which other schemas a schema references. Run once with no mapping and `--dry-run`; the export reports every schema it saw, and writes nothing:

```bash
ora-okf --env-file oracle.env --okf-dir out/okf --dry-run
```

```text
Dry run complete (nothing written)
  schema     : APP_PROD_OWNER exported as APP_PROD_OWNER
  files      : 214 (211 object concepts)
  renaming   : no schema names were rewritten
  unmapped   : APP_PROD_OWNER, REF_DATA_PROD (still present in the bundle)
```

The `unmapped` line is the list to put in `schemas:`. Re-run with the mapping and `unmapped: error` to make any schema you missed a failure rather than a surprise.

### Choosing an `unmapped` policy

`keep` is the default because it is the least surprising: an unmapped schema comes out as-is, and the export prints which ones those were. For a bundle headed to a public repository, use `error` -- it turns "I did not know that schema was referenced" into a failure at the start rather than a discovery after publication.

## What is an OKF bundle

A directory of cross-linked Markdown files, one per database object, each with a YAML frontmatter block whose only required field is `type`:

```text
out/okf/
  .okf-bundle     marker: this directory is managed by ora-okf
  index.md        category listing and concept count
  log.md          extraction timestamp, database version, settings used
  schema.md       schema overview with per-category counts
  tables/         one file per table and global temporary table
  views/
  sequences/
  programs/       procedures, functions, packages, triggers
  types/
  synonyms/
  db_links/
  jobs/
  mviews/
  mview_logs/
```

A table concept looks like this:

````markdown
---
type: Oracle Table
title: ORDERS
description: Customer orders.
resource: APP.ORDERS
tags:
- table
timestamp: 2026-08-28 09:14:02 UTC
primary_key:
- ORDER_ID
---

## Schema

| Column | Type | Nullable | Default | Comment |
| --- | --- | --- | --- | --- |
| ORDER_ID | NUMBER(12) | NO | | Surrogate key |
| CUSTOMER_ID | NUMBER(12) | NO | | |
| PLACED_AT | TIMESTAMP(6) | YES | SYSTIMESTAMP | |

## Constraints

| Name | Type | Columns | Reference | Condition |
| --- | --- | --- | --- | --- |
| ORDERS_PK | P | ORDER_ID | | |
| ORDERS_FK_CUSTOMER | R | CUSTOMER_ID | [CUSTOMERS](/tables/customers.md) | |

- `CUSTOMER_ID` references [CUSTOMERS](/tables/customers.md) (`CUSTOMER_ID`) (CASCADE)

## Comments

```sql
COMMENT ON TABLE APP.ORDERS IS 'Customer orders.';
COMMENT ON COLUMN APP.ORDERS.ORDER_ID IS 'Surrogate key';
```
````

The bundle is `markdownlint`-clean, and rendering the same extracted model twice is byte-identical.

## Safety properties

**The bundle directory is owned, not shared.** Every bundle root gets a `.okf-bundle` marker, because a rerun deletes every `.md` file under it. A pre-existing non-empty directory without that marker is refused, so a mistyped `--okf-dir docs` is an error message rather than a deleted documentation tree.

**Passwords never reach output.** The credential object excludes its password from `repr`, connection errors name the user and DSN but never the secret, and database link passwords are not extracted at all.

**Sampled data is renamed too.** Under `--include-data`, a configuration table that stores its own schema name would otherwise put the physical name back into the bundle after every structural reference had been cleaned.

**The audit checks bytes, not intentions.** Renaming covers every reference the extractor understood. The audit reads the finished files, so it also catches a name that arrived by a route the model never described -- inside a string literal in PL/SQL, say. A name that was supposed to be renamed and is still present fails the run; a name the mapping deliberately kept is reported, not failed.

## Determinism

Rendering the same model twice produces identical bytes. Everything is sorted, nothing iterates a set, and the only timestamp in the output is `generated_at`, stamped once at extraction and carried through as data.

Two *pipeline* runs differ in exactly that timestamp. When comparing bundles from separate runs, filter the `timestamp:` lines; any other difference is a real change.

### Committing a bundle to a repository

`--no-timestamp` drops the timestamp from every concept's frontmatter and from `log.md`, so two runs against an unchanged schema write byte-identical files and no comparison filter is needed. Without it, a CI/CD job that regenerates a committed bundle sees a diff on every run, and a real schema change is indistinguishable from a clock tick.

```bash
ora-okf --env-file oracle.env --mapping schema-map.yaml --okf-dir docs/okf --no-timestamp
git diff --quiet docs/okf || echo "the schema changed"
```

One exception: `--include-data` samples rows with `FETCH FIRST n ROWS ONLY` and no `ORDER BY`, because there is no column guaranteed to exist to order by. SQL promises no row order without one, so an unchanged database can yield different sample rows between runs. Do not use an `--include-data` bundle as a byte-comparison baseline; use a structure-only one.

## Development

```bash
make install      # editable install with dev extras
make test         # full test suite
make lint         # ruff check
make typecheck    # mypy
make check        # lint + format check + typecheck + tests
```

Integration tests need a live Oracle and are marked `integration`; `make test-unit` skips them. Set `ORA_OKF_TEST_ENV_FILE` to a credentials file to enable them.

The project has no runtime dependency on any other project in this account, and no machine-specific path appears anywhere in the repository.

## License

MIT. See [LICENSE](LICENSE).
