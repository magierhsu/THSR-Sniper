# Short-term booking diagnostics

The scheduler emits allowlisted JSON events directly to stdout, independently
of captured interactive booking output. Each attempt has a task_id, run_id and
attempt number. Timestamps are UTC; durations use the monotonic clock.
Session creation marks a new session without revealing its cookie/token.
HTTP events describe explicit application requests; redirects followed inside
curl are included in their elapsed time, not separate request events.
Queue text classification is a heuristic, not proof of a queue position.

Inspect the API container on the deployment host:

```sh
docker logs --since 30m thsr-sniper-api 2>&1
```

Filter JSON booking events for one task (with jq installed):

```sh
docker logs --since 30m thsr-sniper-api 2>&1 | jq -R 'fromjson? | select(.task_id == "TASK-ID")'
```

The API uses Docker json-file rotation: 20 MB per file, 10 files maximum
(approximately 200 MB total). Retention is capacity-based, not a day count.
Old events can disappear on rotation or container recreation. There is no
archive, database history, external log service or automatic export.
Compose logging changes require container recreation to take effect.

No raw HTML, full URLs, request bodies, cookie values, captcha contents,
personal identifiers, membership data, emails or PNR are added to these events.
Exceptions record their class only. Existing application logs are separate
from this new diagnostic stream. Diagnostic write failures do not abort booking.

## Pre-entry Session observation

An opening-mode task may set `pre_entry_seconds` to `30` or `60` (the default
`0` disables it). The scheduler dispatches a dedicated worker at that offset,
which keeps one curl session alive, performs one GET before opening and one GET
at the configured opening time, then closes the session. It never runs OCR,
sends a search/selection/confirmation POST, or increments `attempts`.

The task ends in `observed` (or `paused` if it was paused while observing) and
exposes a value-free `observation_result` with both response classifications,
HTTP statuses, queue-token presence, and whether the same JSESSIONID was seen.
Queue detection checks cookie names and queue-related hidden/data field names,
but remains a heuristic and does not prove that a server-side queue position
was acquired. Cookie names are represented only by short hashes; cookie and
token values are never persisted. A worker exception is recorded as `failed`,
while an intentional service stop records `interrupted` and returns the task
to `waiting`. If all workers are occupied until opening, a waiting experiment
is recorded as `window-missed` without sending a late request, so it cannot
delay real booking tasks. A service restart also converts an interrupted
`observing` task back to `waiting` so the observation can be scheduled again.
