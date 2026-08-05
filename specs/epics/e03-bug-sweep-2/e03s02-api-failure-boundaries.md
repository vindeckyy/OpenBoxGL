# e03s02 — API and failure-boundary sweep

## 1. Identity

- **Story:** e03s02
- **Type:** adversarial test
- **Risk:** P0
- **BCPs:** 5
- **Wave:** 1

## 2. User value

Missing/wrong auth, malformed JSON, exception mapping, concurrent settings saves, and cross-process writes are exercised before any fix so each backlog item gets a mechanism, not a guess.

## 3. Context

The existing `test_bug_sweep_api.py` harness starts a real loopback `ThreadingHTTPServer` over a temp `OPENBOX_DATA_DIR` with a generated token. Backlog seeds: I18 (settings snapshot outside write lock), I17 (native vs web library overwrite), I16 (plugin backup ordering), I15 (.env startup abort).

## 4. Problem statement

The open issues carry no deterministic repro. The sweep must reproduce or refute each mechanism with real HTTP and real store instances.

## 5. Purpose / callers / contracts

Outputs `specs/verifications/e03s02-api-sweep.md` and scratch probes under `/tmp`. Never touches real `~/.local/share`; never runs `openbox.py` and `web_app.py` concurrently on one data root.

## 6. Assumptions

- Auth 403 contract: missing/wrong token on every GET route returns 403.
- Malformed/non-object JSON on POST returns 400 JSON and keeps the server alive.

## 7. Exit criteria

Every backlog seed has a disposition; every probe run reports server liveness afterward.

## 13. Verification commands

```bash
python3 -B test_bug_sweep_api.py
python3 -B test_parity_api.py
python3 /tmp/probe_i18.py   # concurrent partial settings saves
python3 /tmp/probe_i17.py   # native-style full-state save vs web update
python3 /tmp/probe_i17b.py  # current-pattern update vs update
```

## 17. Acceptance criteria

- [ ] Existing adversarial groups pass on the frozen baseline.
- [ ] I18 probe records the observed behavior over real HTTP.
- [ ] I17 probes record both the full-state clobber mechanism and the current-pattern outcome.
- [ ] I15/I16 mechanisms and dispositions are recorded.
- [ ] Server liveness is asserted after every probe.

## 18. Verification script (step-by-step)

1. Run the existing API groups.
2. Run the concurrent settings probe against a real server and record results.
3. Run the two-process store probes and record the final on-disk state.
4. Inspect `env_config.load_dotenv` and `plugins.install_plugin` ordering against the issue claims.
5. Assert liveness after each group.
