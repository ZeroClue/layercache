# Contributing to LayerCache

Thanks for your interest! This project is in early development and contributions are welcome.

## Setup

```bash
git clone https://github.com/ZeroClue/layercache.git
cd layercache
pip install -e ".[dev]"
```

## Running tests

```bash
pytest tests/ -v
```

## Code quality

Run both before committing:

```bash
ruff check layercache/ ruff format layercache/ --check
mypy layercache/
```

## Conventional commits

Use conventional commit messages:

- `feat:` — new feature
- `fix:` — bug fix
- `docs:` — documentation
- `chore:` — maintenance, dependencies
- `refactor:` — code restructuring
- `test:` — test additions/changes

## Pull request process

1. Create a feature branch from `main`
2. Make your changes
3. Run tests and lint locally
4. Open a PR with a clear description
5. Ensure CI passes

## Release process

Maintainers trigger a release by pushing a tag:

```bash
# Update version in layercache/__init__.py
git tag v<VERSION>    # e.g. v1.4.0
git push origin v<VERSION>
```

The release workflow builds the package, publishes to PyPI, and creates a GitHub release with auto-generated release notes.
