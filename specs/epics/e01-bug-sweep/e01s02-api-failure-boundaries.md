# e01s02 — API and failure-boundary sweep

## 1. Identity

- **Story:** e01s02
- **Type:** test / security review
- **Risk:** P0
- **BCPs:** 5
- **Security:** high
- **Wave:** 1 — approximately 90 minutes

## 2. User value

Players receive predictable errors instead of authorization bypasses, dropped connections, stuck jobs, secret disclosure, lost settings, or damaged library state when requests or integrations fail.

## 3. Context

`web_app.py` is a shared boundary for the browser and most domain modules. Prior fixes addressed skipped premium-route authorization, update exceptions, Gameyfin worker terminal states, overlapping session polling, and partial settings resets. The sweep therefore targets both variants of those failures and less-covered dynamic route families.

## 4. Problem statement

Passing happy-path tests do not prove that every route authorizes consistently, rejects malformed data, preserves unrelated state, bounds background work, or translates domain exceptions into stable JSON responses.

## 5. Purpose / callers / contracts

- **Purpose:** systematically attack server-side trust and failure boundaries using isolated local fixtures.
- **Callers:** `index.html`, local API clients, deep-link flows, session polling, and integration adapters.
- **Contracts:** protected routes reject missing/invalid tokens; malformed requests return bounded 4xx JSON; server remains alive after faults; public settings omit secrets; partial updates preserve unrelated fields; background jobs reach terminal states; paths remain inside the temporary data root.

## 6. Preconditions

- e01s01 baseline and safety snapshot are complete.
- Run `web_app.py` only against temporary HOME/XDG directories and fixture data.
- Do not run `openbox.py` concurrently.
- Use local stubs/fixtures rather than live external services.

## 7. Dependencies

- `[OK]` Python standard library HTTP/client, tempfile, unittest/mock, and subprocess facilities.
- `[OK]` existing API/session/update/security test scripts.
- No application package is added.

## 8. Requirements

### ADDED: Authorization matrix covers protected route families

For every protected GET/POST/DELETE family exercised, verify missing, malformed, and valid authorization behavior. Public health/static behavior is separately classified so public endpoints are not falsely reported.

### ADDED: Request validation matrix covers malformed bodies

Probe empty bodies, invalid JSON, wrong top-level types, missing keys, wrong scalar/container types, duplicate actions, unknown IDs, and oversized-but-bounded fixture values where relevant.

### ADDED: Exception mapping preserves server availability

Force representative domain exceptions in updates, imports, storefront/Gameyfin, saves/backups, plugins, sessions, metadata, and integration status paths. Assert a bounded JSON error and a successful subsequent health request.

### ADDED: Persistence and asynchronous invariants are attacked

Verify partial settings merges, secret sanitization, background job terminal states, bounded polling, path containment, idempotent/repeated requests, and no real-user-data writes.

## 9. Invariants

- Never send destructive requests to a real data root.
- Never log raw credentials or bearer tokens in evidence.
- A 4xx response for bad input is expected only when the JSON body is stable and the server remains available.
- No production fix occurs until the candidate has a reproduction and root cause.
- No new security findings in affected paths may remain undispositioned at handoff.

## 10. Probe matrix

| Boundary | Success case | Failure variants | Evidence |
|---|---|---|---|
| Authorization | valid token succeeds | absent, malformed, stale token | status/body; no state change |
| JSON decoding | valid object accepted | empty, invalid JSON, array/string/null | bounded 4xx JSON; next health request succeeds |
| Settings | intended keys persist | omitted unrelated keys, wrong types, secret fields | preserved unrelated values; sanitized GET |
| Imports/storefront | fixture import succeeds | missing source, malformed catalog, adapter exception | no partial corruption; actionable JSON |
| Launch/session | fixture command/session lifecycle | unknown game, bad emulator, duplicate request, process failure | terminal state; no duplicate launch |
| Gameyfin/jobs | accepted background job | worker exception, invalid ID, timeout/stuck simulation | terminal error; bounded polling |
| Saves/backups | temp fixture backup/restore | traversal name, missing backup, I/O exception | contained paths; no source loss |
| Updates | valid fixture release | malformed JSON, missing asset/digest, timeout | bounded JSON; no dropped connection |
| Plugins | safe fixture hook | timeout, malformed stdout, nonzero exit | bounded error; server remains available |
| Integrations | configured fixture | missing credential/program, malformed response | sanitized actionable state |

## 11. Out of scope

- Penetration testing third-party services.
- Load/stress benchmarking beyond duplicate-request and overlap probes.
- Replacing the server or API design.
- Broad route-generation abstractions.

## 12. Detailed implementation steps

1. Start an isolated server fixture with temporary HOME/XDG paths, fixture library/settings, a captured token, and guaranteed cleanup; prove the health endpoint and data-root isolation → verify: `python3 -B test_bug_sweep_api.py --group fixture`
2. Enumerate public versus protected route families from `web_app.py`, then execute missing/invalid/valid token cases and record any inconsistent authorization as security candidates → verify: `python3 -B test_bug_sweep_api.py --group auth && python3 -B test_secrets.py && echo 'no new security findings in affected paths'`
3. Send invalid JSON, wrong top-level types, missing keys, wrong field types, unknown IDs, and repeated actions to mutation routes; after each fault, prove `/api/health` still responds → verify: `python3 -B test_bug_sweep_api.py --group validation`
4. Inject controlled domain exceptions into update, import, storefront/Gameyfin, saves, plugin, session, metadata, and integration paths; require bounded JSON and continued server availability → verify: `python3 -B test_bug_sweep_api.py --group exceptions`
5. Seed unrelated settings plus canary secrets, issue partial settings/storefront writes, and assert omitted values survive while public settings remain sanitized → verify: `python3 -B test_bug_sweep_api.py --group settings && python3 -B test_secrets.py && echo 'no new security findings in affected paths'`
6. Exercise background jobs, repeated launch/session calls, save/backup path containment, plugin failures, and missing-credential integration states using safe fixtures → verify: `python3 -B test_bug_sweep_api.py --group lifecycle && python3 -B test_sessions.py`
7. Add every observed anomaly to the candidate ledger with request, response, server-liveness check, state diff, severity, and reproduction count; reject only with evidence → verify: `test -f specs/verifications/e01s02-api-sweep.md && ! grep -Eq 'status: (new|untriaged)' specs/verifications/e01s02-api-sweep.md`

## 13. Verification commands

```bash
python3 -B test_bug_sweep_api.py --group fixture
python3 -B test_bug_sweep_api.py --group auth
python3 -B test_bug_sweep_api.py --group validation
python3 -B test_bug_sweep_api.py --group exceptions
python3 -B test_bug_sweep_api.py --group settings
python3 -B test_bug_sweep_api.py --group lifecycle
python3 -B test_parity_api.py
python3 -B test_sessions.py
python3 -B test_updates.py
python3 -B test_secrets.py
```

If a sweep-only probe proves durable missing coverage, move it into the closest existing test module before e01s05 rather than retaining duplicate infrastructure.

## 14. Evidence format

`specs/verifications/e01s02-api-sweep.md` records fixture root, route families, sanitized request shape, status/body, state before/after, server liveness, repetition count, candidate ID, and disposition. Secret values are replaced with `<redacted>`.

## 15. Failure handling

- First failure: rerun the narrow group unchanged.
- Intermittent failure: run ten times and record rate, timing, and shared state.
- Server drop: capture stderr and immediately verify whether the process or only the connection died.
- State mutation: preserve a copy of temporary before/after files, then stop that route family.
- Security candidate: classify P0/P1 and do not continue similar mutation paths until containment is understood.

## 16. Risks and mitigations

- **Real data damage:** temporary data roots and canary assertions.
- **Secret leakage:** fake values plus evidence redaction.
- **False route coverage:** derive matrix from source and UI references, but validate behavior rather than route strings alone.
- **Fixture overfitting:** use at least one malformed and one exception path per critical family.
- **Over-broad test harness:** keep sweep probes stdlib-only and migrate only durable assertions.

## 17. Acceptance criteria

- [ ] Isolated fixture proves no writes escape its data root.
- [ ] Authorization, validation, exception, settings, and lifecycle groups execute.
- [ ] Every forced fault is followed by a liveness check.
- [ ] Partial settings and public-secret contracts pass.
- [ ] Every API anomaly has evidence and a terminal current disposition.
- [ ] No new security findings in affected paths remain undispositioned.

## 18. Verification script (step-by-step)

1. Inspect the recorded temporary paths and verify they are not the real OpenBox data directory.
2. Run each `--group` command independently.
3. For one representative failure in each matrix row, inspect status, JSON body, server liveness, and before/after state.
4. Confirm evidence contains no raw token/credential.
5. Follow confirmed candidates into e01s04 root-cause entries.

## 19. Stop conditions

Stop the affected probe family if any write escapes the temporary root, credentials appear in output, the server cannot be restarted cleanly, or a security boundary appears exploitable. Preserve evidence and move directly to containment/root-cause work.

## 20. Handoff

Gate: all API candidates are dispositioned and confirmed defects have deterministic reproductions. Next: e01s03 browser sweep may continue; e01s04 consumes confirmed API defects in priority order.
