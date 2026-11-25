# Third-Party Notices

This package incorporates, references, and implements rules from the following open specifications:

## Conventional Branch 1.1.0

- Source: <https://conventionalbranch.org> (<https://github.com/conventional-branch/conventional-branch>)
- License: Creative Commons Attribution 4.0 International ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/))
- Scope: The branch type vocabulary (`feature`/`feat`, `bugfix`/`fix`, `hotfix`, `release`, `chore`, `ai`, `copilot`, `cursor`, `claude`, `codex`), trunk branch rules (`main`, `master`, `develop`), formal ABNF grammar, validation regex, and conformance test fixtures referenced in `skills/git-branching/SKILL.md`, `skills/repo-hygiene/scripts/repoguard.py`, and `tests/test_repoguard.py` are based on Conventional Branch 1.1.0.

## Conventional Commits 1.0.0

- Source: <https://www.conventionalcommits.org> (<https://github.com/conventional-commits/conventionalcommits.org>)
- License: Creative Commons Attribution 3.0 Unported ([CC BY 3.0](https://creativecommons.org/licenses/by/3.0/))
- Scope: Commit header structure (`<type>[optional scope][!]: <description>`), breaking-change footers, and SemVer mapping rules in `skills/git-committing/SKILL.md` and `skills/repo-hygiene/scripts/repoguard.py` follow Conventional Commits 1.0.0.

## Semantic Versioning 2.0.0

- Source: <https://semver.org>
- License: Creative Commons Attribution 3.0 Unported ([CC BY 3.0](https://creativecommons.org/licenses/by/3.0/))
- Scope: Version bump rules (`MAJOR.MINOR.PATCH`) in `skills/repo-hygiene/SKILL.md` and `repoguard suggest-version` follow SemVer 2.0.0.
