# Task 96: HEAD on missing file emits 404 without CORS headers (R16 violation)

**Milestone**: unassigned
**Spec**: [`local.engine-rest-api-dispatcher`](../../specs/local.engine-rest-api-dispatcher.md) — R16, R41, behavior row 43
**Estimated Time**: 10 minutes
**Status**: Filed (M18-87 regression discovery)
**Repository**: `scenecraft-engine`

---

## Bug

`do_HEAD` in `src/scenecraft/api_server.py` short-circuits with `send_response(404); end_headers()` when the requested file does not exist, **without** calling `self._cors_headers()` first.

```python
def do_HEAD(self):
    parsed = urlparse(self.path)
    path = unquote(parsed.path)
    m = re.match(r"^/api/projects/([^/]+)/files/(.+)$", path)
    if m:
        ...
        if not str(full_path).startswith(...) or not full_path.exists():
            self.send_response(404)
            self.end_headers()       # ← no CORS headers here
            return
        ...
        self._cors_headers()         # ← CORS only emitted on 200 path
```

## Spec contract violated

- **R16** (engine-rest-api-dispatcher): "On every response (including 401, 404, 500), the dispatcher emits ... `Access-Control-Allow-Origin`, `Access-Control-Allow-Methods`, `Access-Control-Allow-Headers`."
- **Migration Contract item 6**: "CORS headers on EVERY response, including errors."
- **Behavior row 43** locks the empty-body status but assumes CORS too (R16 cross-cuts every row).

## Repro

```bash
curl -I -H "Origin: https://app.example.com" \
    http://localhost:5174/api/projects/test-proj/files/missing.mp4
# Response: HTTP/1.0 404
# (no Access-Control-Allow-Origin, no Vary, no Allow-Methods)
```

## Impact

A browser that issues a HEAD preload (common for `<video>` elements) against
a missing file gets a 404 without CORS, which the browser treats as a CORS
failure. The user-visible symptom is a confusing console error instead of a
clean "file not found" handled in JS.

## Fix

Add `self._cors_headers()` before `self.end_headers()` in the early-exit
404 branch. Also add to the 405 fallthrough at the end of `do_HEAD`.

## Test

`test_e2e_head_missing_file_emits_cors` is xfailed in
`tests/specs/test_engine_rest_api_dispatcher.py` and starts passing when
this fix lands.
