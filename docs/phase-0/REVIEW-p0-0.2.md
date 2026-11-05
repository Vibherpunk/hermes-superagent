# REVIEW — Phase 0, card 0.2 (Config parsing for the superagent block)

- Reviewer: gemini-4-argon-high via Antigravity bridge (127.0.0.1:8011), independent third-party seat
- Commit reviewed: `6585b2ffd0d` — verified on disk: `git rev-parse 6585b2ffd0d` = `6585b2ffd0d9ed4354538a1de39e6a68bdfc823c` ✔ matches the sha in the verdict
- Date: 2026-11-03
- Supervisor evidence supplied to reviewer: card packet + full diff + supervisor-run tests/superagent pass (16 tests)

## Verbatim verdict

VERDICT: FAIL
SHA: 6585b2ffd0d
FINDINGS:
- **`human_reserve_slots` validation violates the card's `{0, 1}` constraint**: The card TODO explicitly specifies `human_reserve_slots not in {0, 1} -> error (v1 allows only 0 or 1)`. However, `superagent_settings` in gateway/kanban_watchers_dispatcher.py#L391-L397 and `_validate_superagent` in hermes_cli/config_value_checks.py#L104-L109 only reject values `< 0` (`human_reserve_slots must be an integer >= 0`), allowing invalid values such as `2` without raising an error (and tests/superagent/test_config.py#L34-L50 even asserts `human_reserve_slots: 2` succeeds).
- **Nested `github.receive_all` defaults are not applied on partial or `None` dicts**: The card requires that the returned dict contain every key of the E-033 block with defaults applied, including nested `github`. In `superagent_settings` (gateway/kanban_watchers_dispatcher.py#L408-L413), `dict(raw_github.get("receive_all", ...))` replaces rather than merges the default `receive_all` keys (`issues`, `pull_requests`, `comments`, `ci`) when a partial `receive_all` dict is passed, and raises `TypeError` if `github.receive_all` is `None`.
- **Boolean values are not rejected by `superagent_settings` for integer slot fields**: Unlike `_validate_superagent` (hermes_cli/config_value_checks.py#L99-L106), `superagent_settings` (gateway/kanban_watchers_dispatcher.py#L383-L397) calls `int(...)` without rejecting `isinstance(..., bool)`, allowing `worker_slots: True` or `human_reserve_slots: True/False` to silently coerce to `1` or `0`.

## Supervisor disposition

FAIL stands. Fix round 1 dispatched to a Tier F builder with these findings verbatim (fix packet p0-2-fix1): enforce {0,1} in both places, merge nested receive_all defaults and tolerate None, reject bools for slot fields in the resolver, and correct tests/superagent/test_config.py (the case asserting `human_reserve_slots: 2` succeeds must instead assert it raises; add partial/None receive_all cases and bool cases). The fix commit will be re-reviewed by Argon before card 0.2 is marked passed.
