## What and why

<!-- What does this change, and what problem does it solve? Link the issue if there is one. -->

## How it was tested

<!-- Tests added or changed, and anything you ran by hand. -->

## Checklist

- [ ] `pytest` passes, and `ruff check memdream tests scripts eval examples` is clean
- [ ] New behaviour has a test
- [ ] Docs updated (`README.md`, `docs/sdk.md`, …) if the public surface changed
- [ ] `CHANGELOG.md` has an entry under **Unreleased** for anything users will notice
- [ ] The [invariants in CONTRIBUTING.md](https://github.com/Mamlesh18/MemDream/blob/main/CONTRIBUTING.md#invariants--please-do-not-break-these) still hold — nothing a Dream operation does deletes, every repository call takes a `MemoryScope`, the model proposes and the code decides
