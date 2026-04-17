# gitcalver

A Python implementation of [GitCalVer](https://gitcalver.org), which derives
calendar-based version numbers from git history.

Each commit on the default branch gets a unique, strictly increasing version of
the form `YYYYMMDD.N`, where `N` is the number of commits on that UTC date.

See the [GitCalVer specification](https://gitcalver.org) for full details.

## Installation

```sh
uv add gitcalver
# or
pip install gitcalver
```

See [Requirements](#requirements) below.

## CLI usage

```
gitcalver [OPTIONS] [REVISION | VERSION]
```

With no arguments, prints the version for `HEAD`:

```sh
$ gitcalver
20260411.3
```

Pass a revision to compute its version:

```sh
$ gitcalver HEAD~1
20260411.2
```

### Version prefix

Use `--prefix` to prepend a literal string:

| Use case | Command                      | Example output   |
|----------|------------------------------|------------------|
| Default  | `gitcalver`                  | `20260411.3`     |
| SemVer   | `gitcalver --prefix "0."`    | `0.20260411.3`   |
| Go       | `gitcalver --prefix "v0."`   | `v0.20260411.3`  |

### Dirty workspace

By default, `gitcalver` exits with status 2 if the workspace has uncommitted
changes. Use `--dirty STRING` to produce a version instead; the output will
include the given string and a short commit hash
(e.g. `--dirty "-dirty"` produces `20260411.3-dirty.abc1234`).

Use `--no-dirty-hash` with `--dirty` to suppress the hash suffix.
Use `--no-dirty` to explicitly refuse dirty versions (overrides `--dirty`).

Dirty versions are a convenience and are not necessarily unique.

### Reverse lookup

Pass a version number to get the corresponding commit hash:

```sh
$ gitcalver 20260411.3
a1b2c3d4e5f6...

$ gitcalver --short --prefix "0." 0.20260411.3
a1b2c3d
```

If the version was generated with `--prefix`, pass the same `--prefix` for
reverse lookup. Dirty versions cannot be reversed.

### Options

| Option              | Description                                    |
|---------------------|------------------------------------------------|
| `--prefix PREFIX`   | Literal string prepended to version            |
| `--dirty STRING`    | Enable dirty versions; append `STRING.HASH`    |
| `--no-dirty`        | Refuse dirty versions (overrides `--dirty`)    |
| `--no-dirty-hash`   | Suppress `.HASH` suffix (requires `--dirty`)   |
| `--branch BRANCH`   | Base branch name; overrides auto-detection. This is the branch versions are minted on, not the branch you are working on. |
| `--short`           | Output short commit hash (reverse lookup mode) |
| `--help`            | Show help                                      |

### Exit codes

| Code | Meaning                                |
|------|----------------------------------------|
| 0    | Success                                |
| 1    | Error (not a git repo, no commits, non-monotonic dates, shallow clone) |
| 2    | Dirty workspace or off default branch (without `--dirty`) |
| 3    | Cannot trace to default branch         |

## Python API

```python
import gitcalver

# Forward: compute a version for HEAD (or a specific revision).
version = gitcalver.get_version(repo="/path/to/repo")
# e.g. "20260411.3"

version = gitcalver.get_version(
    repo="/path/to/repo",
    revision="HEAD~1",
    prefix="v0.",
    dirty="-dirty",
)

# Reverse: resolve a version back to a commit hash.
commit = gitcalver.find_commit("20260411.3", repo="/path/to/repo")

# If the version was generated with --prefix, pass the same prefix:
commit = gitcalver.find_commit(
    "v0.20260411.3", prefix="v0.", repo="/path/to/repo"
)
```

Errors are raised as `gitcalver.ExitError`, which carries a `code` attribute
matching the CLI exit codes above.

## Hatch plugin

`gitcalver` ships a [Hatch](https://hatch.pypa.io/) version source plugin. To
use it in `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling", "gitcalver"]
build-backend = "hatchling.build"

[tool.hatch.version]
source = "gitcalver"
# Optional:
# prefix = "0."
# dirty = "-dirty"
# no-dirty-hash = true
# branch = "main"
```

## Requirements

- Python 3.10+
- `git` on `$PATH`
- Full commit history (shallow clones made with `--depth` are rejected; partial
  clones made with `--filter=blob:none` are fine)

## License

MIT
