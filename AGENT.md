# Agent instructions

- Read `README.md` for project architecture, plugin contracts, domain rules, i18n requirements, and testing guidance before making related changes.
- Preserve existing user changes. Inspect `git status` and relevant diffs before editing; do not overwrite or revert unrelated work.
- Follow existing architecture and conventions. Keep changes focused; do not add comments unless requested.
- Internationalize all user-facing strings and keep backend settings and frontend types synchronized.
- Add or update focused tests. Run relevant plugin tests and the full Python suite for backend changes; build the Angular UI for frontend changes. Run `git diff --check` after changes.
- Report unsupported input and ambiguous ship identity explicitly; do not guess.
