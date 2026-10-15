# REVIEW.md

| # | Where | What is wrong | How you'd notice it | Fix |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | No `tz_aware=True`; PyMongo returns naive datetimes, so every later comparison is ambiguous. | `punch_out - punch_in` with mixed naive/aware values raises `TypeError` (500). | `tz_aware=True`; all datetimes are aware UTC. |
| 2 | `GET /health` | Always returns ok, never pings MongoDB. | Stop Mongo: still 200. | `ping`; 503 on `PyMongoError`. |
| 3 | `compute_late_minutes` | Never converts to IST; `replace(hour=...)` runs on whatever zone the datetime has (UTC or server-local). | Punch 09:40 IST on a UTC server gives huge or negative lateness. | Shift start built as an IST datetime from the attendance date. |
| 4 | `compute_late_minutes` | Compares *floored minutes* `> 10`. | 09:40:01 gives floor 10, so the result is 0 instead of 10 (R2). | Compare seconds `> 600`, then floor to minutes. |
| 5 | `compute_late_minutes` | Overnight shifts ignored: a 02:00 punch-in is measured against 22:00 *today*. | Shift 22:00-06:00, punch-in 02:00 gives a negative result, 0. | Start is derived from the attendance date (R1), which is the previous day. |
| 6 | `compute_work_hours` | Float `round()` is banker's/binary rounding, not half-up. No seconds truncation. | 0.125 h gives 0.12 instead of 0.13. | `Decimal` with `ROUND_HALF_UP`; instants truncated to whole seconds. |
| 7 | `compute_overtime` | Attaches IST to the shift end but subtracts a naive `punch_out` (TypeError). No 30-minute minimum (R3). Overnight shift end is not moved to the next day. Uses `fromisoformat(date)` then `replace`. | Overtime of 5 min is reported; overnight punch-out gives 500 or a huge negative. | Aware datetimes, `>= 30` rule, `+1 day` when `shift_end <= shift_start`. |
| 8 | `EmployeeIn` | No validation: emp_code format, email, `HH:MM`, real `joined_on`, `shift_start != shift_end`, lengths. | `{"emp_code":"x","shift_start":"99:99"}` is accepted. | Field patterns/limits plus validators; 422. |
| 9 | `create_employee` | Check-then-insert race and no unique index: parallel creates give several 201s. | 20 parallel POSTs, duplicates stored. | Unique index on `emp_code`, catch `DuplicateKeyError` -> 409. |
| 10 | `create_employee` | `created_at = datetime.now()` is local-time naive, stored as if UTC; returned as an ISO string, not epoch ms. | Wrong instant on a non-UTC server; contract says ms. | UTC `now_utc()`, serialized to epoch ms. |
| 11 | `list_employees` | `skip = page * page_size` skips page 1 entirely. | `page=1` returns the second page. | `(page - 1) * page_size`. |
| 12 | `list_employees` | `total` counts the whole collection, ignoring `department`; no sort; page/page_size unvalidated (`page_size=0`, `-5`, `100000`). | Filter by department, `total` is wrong. | `count_documents(q)`, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 13 | `punch_in` | Unknown employee -> `emp[...]` on `None` -> 500. | `{"emp_code":"NOPE"}`. | 404. |
| 14 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses server-local time, float division, no range check; `if body.punched_at` treats `0` as "now". Seconds-style values are accepted. | `punched_at=1783312500` (seconds) is accepted. | `StrictInt` in `[1e11, 4102444800000]`; `ms // 1000` in UTC; `is not None`. |
| 15 | `punch_in` | `status` is any string (`ABSENT`, `"foo"`). | POST with `status: "ABSENT"` returns 201. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 16 | `punch_in` | `date` is the server-local date of the timestamp: no IST (R1), no overnight rule. | 23:30 IST punch (18:00Z) on a UTC server is dated correctly by luck, 02:00 IST overnight punch is dated to the wrong day. | `attendance_date()` implements R1. |
| 17 | `punch_in` | Check-then-insert race with no unique index. | Two simultaneous identical requests both get 201. | Unique `(emp_code, date)` index, `DuplicateKeyError` -> 409. |
| 18 | `punch_in` response | Returns `id` (the `_id`) and raw datetimes. | Contract: no `_id`, instants as epoch ms. | `serialize_attendance`. |
| 19 | `list_attendance` | Loads every matching document, sorts and paginates in Python: slow and memory-hungry at 100k. | Large collection. | DB-side `sort`/`skip`/`limit` and `count_documents`, backed by indexes. |
| 20 | `list_attendance` | Sort only by `date` (no `emp_code` tiebreak), so page boundaries are unstable. Returns `_id` as `id`, raw datetimes. | Same-date rows shuffle between pages. | Sort `(date desc, emp_code asc)`; serializer. |
| 21 | `list_attendance` | No validation: bad dates, `date_from > date_to`, unknown `status`, page/page_size bounds. | `date_from=banana`, `page_size=0`. | Strict date check, 422, `Literal` status, `Query` bounds. |
| 22 | whole file | No indexes at all. | `explain` shows COLLSCAN. | `ensure_indexes()` in the lifespan hook (idempotent). |

## Looked at and left alone
- `load_dotenv()` defaults to `override=False`, so real environment variables already win over `.env`. Correct as is.
- Extra body fields such as `work_hours` are ignored by Pydantic's default, which is what the contract asks for.
- Defaults `09:30` / `18:30` match the contract.