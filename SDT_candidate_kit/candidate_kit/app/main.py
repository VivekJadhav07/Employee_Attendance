"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)

Part A (fixes to the starter) is in this file. Parts B and C are added below the marker at the bottom.
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from bson import json_util

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False (default): real environment variables win over .env

# tz_aware=True: PyMongo hands back aware UTC datetimes instead of naive ones.
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
LATE_GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW = 4.50


log = logging.getLogger("attendance")

# Index names that aggregation queries are *hinted* to use. A hint makes the plan deterministic: the planner
# cannot fall back to a collection scan when a filter is not selective (e.g. "joined_on <= end of month").
HINT_DEPT_SUMMARY = "dept_joined"
HINT_LEADERBOARD = "date_late_emp"


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when an identical index already exists."""
    e, a = db.employees, db.attendance_logs
    # employees
    e.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")           # uniqueness + lookups/joins
    e.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name=HINT_DEPT_SUMMARY)  # headcount, trend
    e.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp")  # GET /employees?department=
    # attendance_logs
    a.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")  # race guard, monthly, $lookup
    a.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")  # GET /attendance order, date ranges
    a.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    a.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")  # punch-out lookup
    a.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
                   name=HINT_LEADERBOARD)  # leaderboard: range + late>0, and covers the $group fields


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:  # keep the process up so /health can answer 503 instead of crashing at import
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time / conversion helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (to_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as strings


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts that punch in before shift_end belong to the previous day."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < parse_hhmm(shift_end):
        d -= timedelta(days=1)
    return d.isoformat()


def ist_at(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    d = date.fromisoformat(date_str) + timedelta(days=plus_days)
    return datetime.combine(d, parse_hhmm(hhmm), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only if strictly > 10:00 after shift start; then floor minutes since shift start."""
    secs = (punch_in - ist_at(date_str, shift_start)).total_seconds()
    return int(secs // 60) if secs > LATE_GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 dp (Decimal, so no float/banker's rounding)."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: floor minutes after shift end, counted only when >= 30. Overnight shifts end on the next day."""
    end = ist_at(date_str, shift_end, plus_days=1 if is_overnight(shift_start, shift_end) else 0)
    secs = (punch_out - end).total_seconds()
    minutes = int(secs // 60) if secs > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < HALF_DAY_BELOW


# --------------------------------------------------------------------------- #
# Serialisation (Mongo -> API): no _id, instants as epoch ms
# --------------------------------------------------------------------------- #
def _ms_deep(v):
    if isinstance(v, datetime):
        return dt_to_ms(v)
    if isinstance(v, dict):
        return {k: _ms_deep(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_ms_deep(x) for x in v]
    return v


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"], "created_at": dt_to_ms(doc.get("created_at")),
    }


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": _ms_deep(doc.get("history") or []),
    }


def validate_date_param(name: str, value: Optional[str]) -> Optional[str]:
    """Strict YYYY-MM-DD; failures are real 422s with FastAPI's standard body."""
    if value is None:
        return None
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise RequestValidationError([{"loc": ("query", name), "msg": "must be a valid YYYY-MM-DD date",
                                       "type": "value_error", "input": value}])
    return value


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
EpochMs = StrictInt  # StrictInt rejects floats, strings and bools; range checked via Field below


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        date.fromisoformat(v)  # ValueError -> 422 (e.g. 2026-02-30)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_MS, le=MAX_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)  # filtered total
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index: exactly one winner
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    date_from = validate_date_param("date_from", date_from)
    date_to = validate_date_param("date_to", date_to)
    if date_from and date_to and date_from > date_to:
        raise RequestValidationError([{"loc": ("query", "date_from"), "msg": "date_from must be <= date_to",
                                       "type": "value_error"}])
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = (db.attendance_logs.find(q, {"_id": 0})
           .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
           .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_attendance(d) for d in cur], "total": total, "page": page, "page_size": page_size}


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    """Shared with the explain endpoint (Part C) so both build the query identically."""
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


# --------------------------------------------------------------------------- #
# Part B - attendance: punch-out and regularization
# --------------------------------------------------------------------------- #
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_MS, le=MAX_MS)


def unprocessable(loc: tuple, msg: str):
    return RequestValidationError([{"loc": loc, "msg": msg, "type": "value_error"}])


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent record whose punch_in <= punched_at (works across midnight: no date match needed).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = to_utc(rec["punch_in"])
    if ts <= p_in or ts > p_in + timedelta(hours=24):
        raise unprocessable(("body", "punched_at"), "punched_at must be after punch_in and within 24 hours of it")

    wh = compute_work_hours(p_in, ts)
    # The filter on punch_out: None makes the update atomic - of N simultaneous requests exactly one matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": wh,
            "overtime_minutes": compute_overtime(ts, emp["shift_start"], emp["shift_end"], rec["date"]),
            "half_day": compute_half_day(wh),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_MS, le=MAX_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_MS, le=MAX_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def _no_explicit_nulls(cls, data):
        # Omitted means "unchanged"; an explicit null is not a valid value for these fields.
        if isinstance(data, dict):
            for k in ("status", "punch_in", "punch_out"):
                if k in data and data[k] is None:
                    raise ValueError(f"{k} must not be null")
        return data


TRACKED = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    validate_date_param("date", date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no attendance record for that date")

    new_status = body.status or rec["status"]
    in_supplied, out_supplied = body.punch_in is not None, body.punch_out is not None

    if new_status in ("ABSENT", "LEAVE"):
        if in_supplied or out_supplied:
            raise unprocessable(("body", "status"), "ABSENT/LEAVE clears punch times; do not supply them")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if in_supplied else rec.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if out_supplied else rec.get("punch_out")
        if new_in is None:
            raise unprocessable(("body", "punch_in"), "a presence status requires a punch_in")
        new_in = to_utc(new_in)
        new_out = to_utc(new_out) if new_out is not None else None
        if in_supplied and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise unprocessable(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if new_out is not None and not (new_in < new_out <= new_in + timedelta(hours=24)):
            raise unprocessable(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")

    # Recompute every derived field from the final state (R2-R5).
    if new_status in ("ABSENT", "LEAVE"):
        new = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        new = {"late_minutes": compute_late_minutes(new_in, emp["shift_start"], date)}
        if new_out is not None:
            wh = compute_work_hours(new_in, new_out)
            new.update(work_hours=wh, half_day=compute_half_day(wh),
                       overtime_minutes=compute_overtime(new_out, emp["shift_start"], emp["shift_end"], date))
        else:
            new.update(work_hours=None, half_day=False, overtime_minutes=0)
    new.update(status=new_status, punch_in=new_in, punch_out=new_out)

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }

    def same(a, b):
        if isinstance(a, datetime) or isinstance(b, datetime):
            return a is not None and b is not None and to_utc(a) == to_utc(b)
        return a == b

    # "Changes nothing" is judged on what the client asked to change, not on derived fields.
    if all(same(old[k], new[k]) for k in ("status", "punch_in", "punch_out")):
        raise unprocessable(("body",), "request changes nothing")

    changes = {k: {"from": old[k], "to": new[k]} for k in TRACKED if not same(old[k], new[k])}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the write only applies if history has not grown since we read it,
    # so two concurrent corrections can never overwrite each other's audit entry (loser gets 409).
    hist_cond = ({"$size": len(rec["history"])} if isinstance(rec.get("history"), list) else {"$exists": False})
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": hist_cond},
        {"$set": {k: new[k] for k in TRACKED}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Part B - analytics (all MongoDB aggregation pipelines)
# Each *_pipeline() builder returns (collection, pipeline) so Part C's explain can reuse it unchanged.
# --------------------------------------------------------------------------- #
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
Month = Query(..., pattern=MONTH_RE)


def month_bounds(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:])
    return f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"


def count_working_days(start: str, end: str) -> int:
    """Mon-Fri days in [start, end] inclusive (R7). Pure calendar arithmetic, no data involved."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if e < s:
        return 0
    full, extra = divmod((e - s).days + 1, 7)
    return full * 5 + sum(1 for i in range(extra) if (s + timedelta(days=i)).weekday() < 5)


def round_half_up(x, places: int):
    if x is None:
        return None
    # round(..., 9) strips binary-float noise (25.349999999999998 -> 25.35) before the exact Decimal rounding.
    return float(Decimal(repr(round(float(x), 9))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# Reusable aggregation expressions over a log document
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
PRESENT_VALUE = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]},
                           {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}
LATE_FLAG = {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def employee_monthly_pipeline(emp_code: str, month: str):
    start, end = month_bounds(month)
    return "attendance_logs", [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_VALUE},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": LATE_FLAG},
            "total_late_minutes": {"$sum": LATE_MIN},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Month):
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if not emp:
        raise HTTPException(404, "employee not found")
    start, end = month_bounds(month)
    working_days = count_working_days(max(start, emp["joined_on"]), end)
    coll, pipe = employee_monthly_pipeline(emp_code, month)
    r = next(db[coll].aggregate(pipe), None) or {}
    present = r.get("present_days", 0)
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(present / working_days * 100, 2) if working_days else None,
    }


def department_summary_pipeline(month: str, department: Optional[str] = None):
    """employees -> $lookup(logs of the month, pre-aggregated per employee) -> $group by department.
    Starting from employees is what keeps people with zero logs in the headcount (R9)."""
    start, end = month_bounds(month)
    emp_match: dict = {"joined_on": {"$lte": end}}
    if department:
        emp_match["department"] = department
    return "employees", [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": PRESENT_VALUE},
                    "hours_sum": {"$sum": {"$cond": [HAS_HOURS, "$work_hours", 0]}},
                    "hours_n": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
                    "late_count": {"$sum": LATE_FLAG},
                    "late_minutes": {"$sum": LATE_MIN},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "s",
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},  # at most one element per employee
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
            "hours_sum": {"$sum": {"$ifNull": ["$s.hours_sum", 0]}},
            "hours_n": {"$sum": {"$ifNull": ["$s.hours_n", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$s.late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Month, department: Optional[str] = None):
    coll, pipe = department_summary_pipeline(month, department)
    items = []
    for r in db[coll].aggregate(pipe, hint=HINT_DEPT_SUMMARY):
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            # mean over records (sum of hours / number of records), not a mean of per-employee means
            "avg_work_hours": round_half_up(r["hours_sum"] / r["hours_n"], 2) if r["hours_n"] else None,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


def late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None):
    start, end = month_bounds(month)
    pipe: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped
    ]
    if department:
        pipe.append({"$match": {"e.department": department}})  # rank inside the department only
    pipe += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1,2,2,4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking, so ties at the cutoff all stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return "attendance_logs", pipe


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Month, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    coll, pipe = late_leaderboard_pipeline(month, limit, department)
    return {"month": month, "items": list(db[coll].aggregate(pipe, hint=HINT_LEADERBOARD))}


def round4_expr(x):
    """Half-up to 4 dp in the pipeline (Mongo's $round is banker's); the 1e-9 absorbs float noise."""
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [x, 10000]}, 0.5, 1e-9]}}, 10000]}


def department_trend_pipeline(department: str, d_from: str, d_to: str):
    f_dt = datetime.fromisoformat(d_from).replace(tzinfo=UTC)
    t_dt_excl = datetime.fromisoformat(d_to).replace(tzinfo=UTC) + timedelta(days=1)  # $densify upper bound is exclusive
    # Branch A (main): how many department employees joined on each day (clamped to the range start).
    # Branch B ($unionWith): present / late totals per day from the department's logs.
    # A running sum of "new heads" then gives the headcount of every day (R9).
    return "employees", [
        {"$match": {"department": department, "joined_on": {"$lte": d_to}}},
        {"$group": {"_id": {"$max": ["$joined_on", d_from]}, "new_heads": {"$sum": 1}}},
        {"$project": {"date": "$_id", "_id": 0, "new_heads": 1, "present": {"$literal": 0}, "late": {"$literal": 0}}},
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}},
            {"$lookup": {
                "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                             {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}}],
                "as": "l"}},
            {"$unwind": "$l"},
            {"$group": {
                "_id": "$l.date",
                "present": {"$sum": {"$cond": [{"$in": ["$l.status", list(PRESENCE)]},
                                               {"$cond": [{"$ifNull": ["$l.half_day", False]}, 0.5, 1]}, 0]}},
                "late": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$l.late_minutes", 0]}, 0]}, 1, 0]}},
            }},
            {"$project": {"date": "$_id", "_id": 0, "new_heads": {"$literal": 0}, "present": 1, "late": 1}},
        ]}},
        {"$group": {"_id": "$date", "new_heads": {"$sum": "$new_heads"},
                    "present": {"$sum": "$present"}, "late": {"$sum": "$late"}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # One row per calendar day, gaps filled inside the database.
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [f_dt, t_dt_excl]}}},
        {"$addFields": {k: {"$ifNull": ["$" + k, 0]} for k in ("new_heads", "present", "late")}},
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"headcount": {"$sum": "$new_heads",
                                                       "window": {"documents": ["unbounded", "current"]}}}}},
        {"$addFields": {
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
        }},
        {"$addFields": {"rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            round4_expr({"$divide": ["$present", "$headcount"]}), None]}}},
        # $avg skips nulls; window = current row + up to 6 previous rows inside the range.
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"avg7": {"$avg": "$rate", "window": {"documents": [-6, "current"]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": "$present", "late_count": "$late",
            "attendance_rate": "$rate",
            "moving_avg_7d": {"$cond": [{"$eq": ["$avg7", None]}, None, round4_expr("$avg7")]},
        }},
    ]


def validate_trend_range(from_: str, to: str) -> None:
    validate_date_param("from", from_)
    validate_date_param("to", to)
    span = (date.fromisoformat(to) - date.fromisoformat(from_)).days + 1
    if span < 1:
        raise unprocessable(("query", "to"), "to must be >= from")
    if span > 92:
        raise unprocessable(("query", "to"), "range must not exceed 92 days")


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    validate_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    coll, pipe = department_trend_pipeline(department, from_, to)
    items = list(db[coll].aggregate(pipe))
    for it in items:
        it["present_count"] = round_half_up(it["present_count"], 2)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Part C - GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
def explain_command(cmd: dict) -> dict:
    res = db.command({"explain": cmd, "verbosity": "executionStats"})
    # BSON -> plain JSON (dates, Int64, Timestamp ... ) so FastAPI can return the raw explain document.
    return json.loads(json_util.dumps(res, json_options=json_util.RELAXED_JSON_OPTIONS))


def explain_aggregate(coll: str, pipe: list, hint: Optional[str] = None) -> dict:
    cmd: dict = {"aggregate": coll, "pipeline": pipe, "cursor": {}}
    if hint:
        cmd["hint"] = hint
    return explain_command(cmd)


def require(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise RequestValidationError([{"loc": ("query", k), "msg": "field required", "type": "missing"} for k in missing])


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                      "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Explains the endpoint's main query, built by the very same builders the real endpoint uses."""
    if endpoint == "attendance_list":
        date_from = validate_date_param("date_from", date_from)
        date_to = validate_date_param("date_to", date_to)
        if date_from and date_to and date_from > date_to:
            raise unprocessable(("query", "date_from"), "date_from must be <= date_to")
        coll = "attendance_logs"
        explain = explain_command({
            "find": coll,
            "filter": build_attendance_query(emp_code, date_from, date_to, status),
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        })
    elif endpoint == "employee_monthly":
        require(emp_code=emp_code, month=month)
        coll, pipe = employee_monthly_pipeline(emp_code, month)
        explain = explain_aggregate(coll, pipe)
    elif endpoint == "department_summary":
        require(month=month)
        coll, pipe = department_summary_pipeline(month, department)
        explain = explain_aggregate(coll, pipe, HINT_DEPT_SUMMARY)
    elif endpoint == "late_leaderboard":
        require(month=month)
        coll, pipe = late_leaderboard_pipeline(month, limit, department)
        explain = explain_aggregate(coll, pipe, HINT_LEADERBOARD)
    else:  # department_trend
        require(department=department, **{"from": from_, "to": to})
        validate_trend_range(from_, to)
        coll, pipe = department_trend_pipeline(department, from_, to)
        explain = explain_aggregate(coll, pipe)
    return {"endpoint": endpoint, "collection": coll, "explain": explain}