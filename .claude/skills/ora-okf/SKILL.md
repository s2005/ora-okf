---
name: ora-okf
description: Run the ora-okf Oracle schema exporter straight from GitHub with uvx - no clone, no venv, no pip install. Use when asked to export an Oracle schema as OKF/Markdown documentation, to produce or refresh a schema bundle with schema names renamed, to find which schemas a schema references, or when someone mentions ora-okf, an OKF bundle, or a .okf-bundle directory.
---

# ora-okf (run with uvx, no clone)

Exports one Oracle schema as a directory of cross-linked Markdown concepts (an OKF bundle), rewriting every
schema name through a mapping file, then reading the written bytes back and failing the run if a renamed
physical name survived.

Source: `https://github.com/s2005/ora-okf` (public, MIT). Not published on PyPI, so it is run from the git URL.

**Pinned release: `v0.0.1`.** Every command here pins that tag, so the behaviour described below is the
behaviour that runs. See [Keeping this skill current](#keeping-this-skill-current) before changing the pin.

## Prerequisite: uv

`uv` is the only thing this needs. It is often already installed but missing from the current shell's PATH,
so check both:

```bash
uv --version || ls "$HOME/.local/bin/uv" "$HOME/.local/bin/uv.exe" 2>/dev/null
```

If it is genuinely absent, install it - user-local, no admin rights, no system Python required. Ask before
installing anything on someone's machine.

| Platform | Command |
| --- | --- |
| macOS, Linux, WSL, Git Bash | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Windows PowerShell | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| Homebrew | `brew install uv` |
| winget | `winget install --id=astral-sh.uv -e` |
| A working pip | `pip install --user uv` |

The installer writes to `~/.local/bin` and edits shell profiles - it does not change the shell that is already
running. Put it on PATH for the current session rather than telling the user to open a new terminal:

```bash
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

### If uv cannot be installed

The package is a normal Python distribution, so any installer that reads a git URL works. With pipx:

```bash
pipx run --spec "git+https://github.com/s2005/ora-okf.git@v0.0.1" ora-okf --help
```

Or a throwaway virtualenv on stock Python 3.10+:

```bash
python -m venv .venv
./.venv/bin/pip install "git+https://github.com/s2005/ora-okf.git@v0.0.1"   # Windows: .venv/Scripts/pip
./.venv/bin/ora-okf --help                                          # Windows: .venv/Scripts/ora-okf
```

Both leave artifacts in the working directory, which is what uvx avoids - prefer uv when it is available.
Every route needs network access to GitHub and PyPI; on an air-gapped machine none of them work, and the
package has to arrive as a pre-built wheel.

## Run it

```bash
uvx --from "git+https://github.com/s2005/ora-okf.git@v0.0.1" ora-okf --help
```

Prefix any invocation below with `uvx --from "git+https://github.com/s2005/ora-okf.git@v0.0.1"`. With `uv`
present (see above), nothing else has to be installed: uvx fetches a suitable Python (3.10+), builds the
package into a cached ephemeral environment, runs it, and leaves nothing in the working directory. Connections use
python-oracledb in thin mode, so no Oracle Instant Client is needed either.

Quote the `--from` value: it contains `@`, which some shells and most YAML CI files handle badly unquoted.

Dropping `@v0.0.1` runs the head of `main` instead. That is the right choice only when chasing an unreleased
fix: what it runs can change under you between two invocations.

```bash
uvx --from git+https://github.com/s2005/ora-okf.git ora-okf --version   # unpinned: latest main
```

A tag pin still contacts the remote on every run to resolve the ref - a tag can be moved, so uv re-checks it.
That costs about two seconds. Only a full commit SHA is resolved locally from the cache and skips the network
entirely, which is worth knowing for a CI job that runs the command repeatedly:

```bash
uvx --from "git+https://github.com/s2005/ora-okf.git@157d5cbb44f159053657e12db89732912755fc6d" ora-okf --version
```

Releases are listed at `https://github.com/s2005/ora-okf/releases`; that SHA is the commit `v0.0.1` points at.
Force a rebuild of an already-cached ref with `uvx --refresh-package ora-okf --from ...`.

Optional, for repeated use on one machine - a persistent `ora-okf` command instead of the `--from` prefix:

```bash
uv tool install "git+https://github.com/s2005/ora-okf.git@v0.0.1"
uv tool upgrade ora-okf        # only meaningful for an unpinned install
uv tool uninstall ora-okf
```

An install pinned to a tag stays on that tag; move it forward by installing the next tag over it.

## Two input files

Both live in the working directory; all paths on the command line are resolved relative to it.

**Credentials**, a `KEY=value` file. Copy `assets/oracle.env.template` from this skill directory.
Any key left blank falls back to the process environment, so keep the password out of the file:

```bash
export DB_PASSWORD='...'      # never echo it, never commit it
```

| Key | Required | Meaning |
| --- | --- | --- |
| `DB_USER` | yes | Connecting account. |
| `DB_PASSWORD` | yes | May come from the process environment instead. |
| `DB_DSN` | one of | Full DSN, `host:port/service`. Wins over the parts below. |
| `DB_HOST` / `DB_PORT` / `DB_SERVICE` | one of | Parts to assemble a DSN. `DB_PORT` defaults to 1521. |
| `SCHEMA` | no | Schema to export. Defaults to `DB_USER`; `--schema` overrides both. |

**Mapping**, YAML or JSON. Copy `assets/schema-map.yaml`.

| Key | Required | Meaning |
| --- | --- | --- |
| `schemas` | yes | Physical name to published name, matched case-insensitively. |
| `unmapped` | no | Referenced schema with no entry: `keep` (default), `redact`, or `error`. |
| `redacted_name` | no | Placeholder for `unmapped: redact`. Default `EXTERNAL`. |
| `version` | no | Format version, currently `1`. |

A published name must be a legal unquoted Oracle identifier (letter, then letters, digits, `_ $ #`), because it
is substituted into `COMMENT ON` statements, `resource:` identifiers and PL/SQL source. Unknown top-level keys
and chained renames (`A -> B` alongside `B -> C`) are rejected when the file loads.

## Workflow

```bash
X='uvx --from git+https://github.com/s2005/ora-okf.git@v0.0.1 ora-okf'

# 1. Check credentials and mapping without connecting to anything.
$X --env-file oracle.env --mapping schema-map.yaml --okf-dir out/okf --validate-only

# 2. Find out which schemas the objects actually reference. Writes nothing.
#    The "unmapped" line of the report is the list to put under schemas:.
$X --env-file oracle.env --okf-dir out/okf --dry-run

# 3. Export for real, with unmapped: error so a missed schema is a failure.
$X --env-file oracle.env --mapping schema-map.yaml --okf-dir out/okf
```

For a bundle committed to a repository, add `--no-timestamp` so an unchanged schema re-exports
byte-identically and CI sees a diff only on a real schema change:

```bash
$X --env-file oracle.env --mapping schema-map.yaml --okf-dir docs/okf --no-timestamp
git diff --quiet docs/okf || echo "the schema changed"
```

## Options

Every option is named; there are no positional arguments.

| Option | Default | Description |
| --- | --- | --- |
| `--okf-dir PATH` | required | Directory to write into. Must be empty or a previously exported bundle. Required even with `--validate-only`. |
| `--env-file PATH` | - | `KEY=value` credentials file. Missing values fall back to the process environment. |
| `--schema NAME` | `SCHEMA`, then `DB_USER` | Schema to extract. |
| `--mapping PATH` | - | YAML or JSON schema mapping. Without it, nothing is renamed. |
| `--no-schema-qualifier` | off | Render `resource:` as a bare object name instead of `<SCHEMA>.<OBJECT>`. |
| `--no-timestamp` | off | Omit the extraction timestamp from frontmatter and `log.md`. |
| `--no-fail-on-leak` | off | Report, rather than fail, when a renamed name survives into the bundle. |
| `--include-data` | off | Add a row count and bounded row sample to each table concept. Needs `SELECT` on the tables. |
| `--sample-rows N` | `5` | Maximum sample rows per table. `0` keeps counts, omits samples. Negative is a usage error. |
| `--dry-run` | off | Render and audit in memory, report, write nothing. Mutually exclusive with `--validate-only`. |
| `--validate-only` | off | Validate credentials and mapping, then exit. Does not connect. |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Logs go to stderr. |
| `--log-file PATH` | - | Also write a `DEBUG`-level log here. |
| `--version` | - | Print the version and exit. |

Exit codes: `0` success, `1` configuration/connection/extraction/write error, `2` invalid command line,
`3` a renamed physical schema name survived into the bundle.

## Output

```text
out/okf/
  .okf-bundle     marker: this directory is managed by ora-okf
  index.md        category listing and concept count
  log.md          extraction timestamp, database version, settings used
  schema.md       schema overview with per-category counts
  tables/ views/ sequences/ programs/ types/ synonyms/ db_links/ jobs/ mviews/ mview_logs/
```

## Gotchas

- **The bundle directory is owned, not shared.** A rerun deletes every `.md` under `--okf-dir`. A non-empty
  directory without the `.okf-bundle` marker is refused, so a mistyped `--okf-dir docs` is an error, not a
  deleted docs tree.
- **Exit 3 is a leak, not a crash.** A name the mapping said to rename is still in the written bytes. Fix the
  mapping; `--no-fail-on-leak` only downgrades it to a report.
- **`--include-data` bundles are not byte-stable.** Samples use `FETCH FIRST n ROWS ONLY` with no `ORDER BY`.
  Use a structure-only bundle as a comparison baseline.
- **Without `--no-timestamp`, two runs differ in the `timestamp:` lines.** Filter those before diffing; any
  other difference is real.
- Sampled row data is renamed too, and database link passwords are never extracted.

## Keeping this skill current

The pin is a claim about what runs; the options table is a claim about what that version accepts. Both go
stale when a release changes the CLI. When moving to a newer release:

1. Read what changed - `gh release view <tag> --repo s2005/ora-okf`, or run
   `uvx --from "git+https://github.com/s2005/ora-okf.git@<tag>" ora-okf --help`.
2. Replace every pinned tag in this file: `grep -c 'ora-okf.git@' SKILL.md` counts the lines to touch.
3. Re-check the options table and exit codes against that `--help` output before trusting them.

Staying on an old tag is safe - it keeps working. Bumping the pin without re-reading `--help` is what
produces a skill documenting flags the pinned version does not have.
