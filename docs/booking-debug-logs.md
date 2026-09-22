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
