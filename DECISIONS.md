# DECISIONS.md

## 1. Indexes

I created five indexes at startup via `create_indexes()` (idempotent because PyMongo's `create_index` uses `createIndexes` which is a no-op when the index already exists):

| Index | Collection | Fields | Purpose |
|---|---|---|---|
| `emp_code_unique` | `employees` | `emp_code` ASC, **unique** | Enforces uniqueness so concurrent duplicate `POST /employees` requests yield exactly one 201 and the rest 409 (from `DuplicateKeyError`) — no check-then-insert race. Also backs every `find_one({"emp_code": ...})` lookup. |
| `emp_date_unique` | `attendance_logs` | `(emp_code, date)` ASC, **unique** | Enforces the "at most one record per (emp_code, date)" invariant. Concurrent duplicate punch-ins for the same date will raise `DuplicateKeyError`, giving exactly one 201 and the rest 409 — no application-level locking needed. |
| `date_desc_emp_asc` | `attendance_logs` | `date` DESC, `emp_code` ASC | Covers the `GET /attendance` list query's sort and equality filters; with a covered scan MongoDB can serve the page without a COLLSCAN. |
| `emp_date` | `attendance_logs` | `(emp_code, date)` ASC | Covers the analytics `$match` that filters by emp_code and date range (employee monthly summary). Also speeds up regularize lookups. |
| `date_asc` | `attendance_logs` | `date` ASC | Covers date-range queries in department summary and late leaderboard aggregations. |

**Index I considered and rejected:** a compound index on `(department, emp_code)` for employees. Because the `GET /employees?department=...` query is a simple equality + sort and the collection is expected to be small (employees), the overhead of maintaining a second compound index is not justified. The `emp_code_unique` index already serves the sort.

## 2. Punch-in race condition

Two identical `POST /attendance/punch-in` requests for the same employee arrive simultaneously.

1. Both reach `db.employees.find_one(...)` and both find the employee — that is fine, reads are non-destructive.
2. Both compute the same `att_date` and `late_minutes`.
3. Both call `db.attendance_logs.insert_one(doc)` with the same `(emp_code, date)`.
4. MongoDB processes the two inserts serially at the storage level. The first succeeds and creates the document.
5. The second hits the **unique index** on `(emp_code, date)` and raises a `pymongo.errors.DuplicateKeyError`.
6. My code catches `DuplicateKeyError` and returns HTTP 409.

Result: exactly one 201 and one 409 — no check-then-insert race, no application-level lock needed.

## 3. Ties in the leaderboard

If two employees have the same total `late_minutes` at the cutoff of `limit`, I include **both** in the result.

Example with `limit=2` and employees A (90 min), B (80 min), C (80 min):
- A gets rank 1, B gets rank 2, C gets rank 2.
- Since `limit=2`, I return all rows where `rank <= 2`: that is A, B, and C — three rows.

This is standard competition ranking (1, 2, 2, 4, ...). The rank is assigned by comparing each employee's `total_late_minutes` to the previous one in the sorted list; if they differ the rank jumps to the current array index + 1, otherwise the rank stays the same. The spec says "return every employee whose rank ≤ limit", so a tie at the boundary intentionally returns more than `limit` rows.

## 4. Headcount in department summary

The department summary pipeline starts from the `employees` collection (not `attendance_logs`). For each employee with `joined_on <= last_day_of_month`, I use `$addToSet` on `emp_code` to collect unique employee codes per department. After the `$project` stage, `headcount = {$size: "$headcount_set"}`.

Because the pipeline starts with all qualifying employees (not just those who have logs), an employee with zero attendance logs is still counted in the `$addToSet`. The `$lookup` to `attendance_logs` uses `preserveNullAndEmpty: True` in `$unwind`, so employees with no matching logs produce one document with `logs = null`, which contributes 0 to all log-based counters but is still present in the `$addToSet`. This ensures headcount includes people with zero logs.

## 5. One change for 100× the data

At 100× scale (~10 M attendance records), the biggest bottleneck would be the **department trend** and **department summary** aggregations, which join `attendance_logs` with `employees` via a `$lookup` on every request. I would introduce a **denormalized `department` field directly on every `attendance_log` document** (written at punch-in time from the employee record). This turns the `$lookup` into a simple `$match` on `date` and `department`, eliminates the join entirely, and allows a compound index on `(department, date)` to serve all department-scoped analytics with an IXSCAN. The tradeoff is that if an employee changes department the historical logs keep the old department — acceptable for attendance analytics where the department at the time of attendance is the semantically correct value.
