"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
"""
import os
import math
from datetime import datetime, timedelta, timezone, date as date_type
from typing import Optional, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()


MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB",  "attendance_db")

client = MongoClient(MONGO_URI)
db     = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True, name="emp_date_unique"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_desc_emp_asc"
    )
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], name="emp_date")
    db.attendance_logs.create_index([("date", ASCENDING)], name="date_asc")




def ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC-aware datetime, truncated to whole seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    """Convert a datetime to epoch milliseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is UTC-aware. PyMongo returns naive datetimes; attach UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def now_utc() -> datetime:
    """Return current UTC time truncated to whole seconds."""
    t = datetime.now(timezone.utc)
    return t.replace(microsecond=0)


def attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    ist_dt   = punch_in_utc.astimezone(IST)
    ist_date = ist_dt.date()

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    if (eh, em) <= (sh, sm):
        if (ist_dt.hour, ist_dt.minute) < (eh, em):
            return (ist_date - timedelta(days=1)).isoformat()

    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, att_date: str) -> int:
    sh, sm = map(int, shift_start.split(":"))
    att_d = datetime.fromisoformat(att_date)
    shift_start_ist = datetime(att_d.year, att_d.month, att_d.day, sh, sm, 0, tzinfo=IST)
    delta_secs = (punch_in_utc - shift_start_ist).total_seconds()
    if delta_secs <= 10 * 60:
        return 0
    return int(delta_secs // 60)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end: str, att_date: str, shift_start: str) -> int:
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    att_d  = datetime.fromisoformat(att_date)

    shift_end_date = att_d
    if (eh, em) <= (sh, sm):
        shift_end_date = att_d + timedelta(days=1)

    shift_end_ist = datetime(
        shift_end_date.year, shift_end_date.month, shift_end_date.day,
        eh, em, 0, tzinfo=IST
    )
    delta_secs = (punch_out_utc - shift_end_ist).total_seconds()
    minutes = int(delta_secs // 60)
    return minutes if minutes >= 30 else 0


def round_half_up(value: float, decimals: int) -> float:
    factor = 10 ** decimals
    return math.floor(value * factor + 0.5) / factor


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    secs = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(secs / 3600, 2)


def compute_half_day(work_hours: float) -> bool:
    return work_hours < 4.50


def serialize_record(doc: dict) -> dict:
    out = {
        "emp_code":          doc["emp_code"],
        "date":              doc["date"],
        "status":            doc["status"],
        "punch_in":          dt_to_ms(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out":         dt_to_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours":        doc.get("work_hours"),
        "late_minutes":      doc.get("late_minutes", 0),
        "overtime_minutes":  doc.get("overtime_minutes", 0),
        "half_day":          doc.get("half_day", False),
        "history":           [],
    }
    for entry in doc.get("history", []):
        h = {
            "at":      dt_to_ms(entry["at"]),
            "by":      entry["by"],
            "reason":  entry["reason"],
            "changes": {},
        }
        for field, chg in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                h["changes"][field] = {
                    "from": dt_to_ms(chg["from"]) if chg.get("from") else None,
                    "to":   dt_to_ms(chg["to"])   if chg.get("to")   else None,
                }
            else:
                h["changes"][field] = {"from": chg["from"], "to": chg["to"]}
        out["history"].append(h)
    return out


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code":    doc["emp_code"],
        "name":        doc["name"],
        "email":       doc["email"],
        "department":  doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end":   doc["shift_end"],
        "joined_on":   doc["joined_on"],
        "created_at":  dt_to_ms(doc["created_at"]),
    }



EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

def validate_epoch_ms(v: Any) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("punched_at must be an integer epoch milliseconds value")
    if v < EPOCH_MIN or v > EPOCH_MAX:
        raise ValueError(f"punched_at must be between {EPOCH_MIN} and {EPOCH_MAX} (epoch milliseconds)")
    return v


class EmployeeCreate(BaseModel):
    emp_code:    str
    name:        str
    email:       str
    department:  str
    shift_start: str = "09:30"
    shift_end:   str = "18:30"
    joined_on:   str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        import re
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or len(v) > 100:
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        import re
        if not re.match(r"^[^\@\s]+@[^\@\s]+\.[^\@\s]+$", v):
            raise ValueError("invalid email")
        if len(v) > 120:
            raise ValueError("email must be <= 120 characters")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v: str) -> str:
        if not v or len(v) > 50:
            raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v: str) -> str:
        import re
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def shift_start_ne_shift_end(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None
    status:     str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        if v is None:
            return v
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        if v is None:
            return v
        return validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    reason:         str
    regularized_by: str
    status:         Optional[str] = None
    punch_in:       Optional[int] = None
    punch_out:      Optional[int] = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v: str) -> str:
        if not v or len(v) > 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise ValueError("invalid status value")
        return v

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_epoch(cls, v):
        if v is None:
            return v
        return validate_epoch_ms(v)

    @model_validator(mode="after")
    def validate_absent_leave_no_punches(self) -> "RegularizeRequest":
        if self.status in ("ABSENT", "LEAVE"):
            if self.punch_in is not None or self.punch_out is not None:
                raise ValueError("Cannot supply punch_in or punch_out with ABSENT or LEAVE status")
        return self




@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unreachable")




@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(1,  ge=1),
    page_size:  int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip  = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [serialize_employee(e) for e in items],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }




@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    punch_ts = ms_to_dt(body.punched_at) if body.punched_at else now_utc()
    att_date = attendance_date(punch_ts, emp["shift_start"], emp["shift_end"])
    late_min  = compute_late_minutes(punch_ts, emp["shift_start"], att_date)

    doc = {
        "emp_code":         body.emp_code,
        "date":             att_date,
        "status":           body.status,
        "punch_in":         punch_ts,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late_min,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")

    doc.pop("_id", None)
    return serialize_record(doc)




@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    punch_ts = ms_to_dt(body.punched_at) if body.punched_at else now_utc()

    # Find most recent open record with punch_in <= punch_ts
    record = db.attendance_logs.find_one(
        {
            "emp_code":  body.emp_code,
            "punch_in":  {"$lte": punch_ts},
            "punch_out": None,
            "status":    {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "No open punch-in found for this employee")

    pi = ensure_utc(record["punch_in"])
    if punch_ts <= pi:
        raise HTTPException(422, "punch_out must be after punch_in")
    if (punch_ts - pi).total_seconds() > 86400:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    wh = compute_work_hours(pi, punch_ts)
    ot = compute_overtime_minutes(punch_ts, emp["shift_end"], record["date"], emp["shift_start"])
    hd = compute_half_day(wh)

    result = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": record["date"], "punch_out": None},
        {"$set": {
            "punch_out":        punch_ts,
            "work_hours":       wh,
            "overtime_minutes": ot,
            "half_day":         hd,
        }},
        return_document=True,
    )
    if not result:
        raise HTTPException(409, "Record already punched out")

    result.pop("_id", None)
    return serialize_record(result)




@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(1,  ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

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
        if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(422, "Invalid status value")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip  = (page - 1) * page_size
    docs  = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [serialize_record(d) for d in docs],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }




@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "No attendance record found for this date")

    new_status    = body.status    or record["status"]
    new_punch_in  = ms_to_dt(body.punch_in)  if body.punch_in  is not None else ensure_utc(record.get("punch_in"))
    new_punch_out = ms_to_dt(body.punch_out) if body.punch_out is not None else ensure_utc(record.get("punch_out"))


    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in  = None
        new_punch_out = None


    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_punch_in is None:
        raise HTTPException(422, "Presence status requires a punch_in")


    if new_punch_in is not None:
        new_att_date = attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if new_att_date != date:
            raise HTTPException(422, "punch_in must remain on the record's attendance date")


    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(422, "Cannot have punch_out without punch_in")
        if new_punch_out <= new_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 86400:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")


    if new_punch_in is not None:
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"], date)
    else:
        new_late = 0

    if new_punch_in is not None and new_punch_out is not None:
        new_wh = compute_work_hours(new_punch_in, new_punch_out)
        new_ot = compute_overtime_minutes(new_punch_out, emp["shift_end"], date, emp["shift_start"])
        new_hd = compute_half_day(new_wh)
    else:
        new_wh = None
        new_ot = 0
        new_hd = False


    changes: dict = {}
    old_pi = ensure_utc(record.get("punch_in"))
    old_po = ensure_utc(record.get("punch_out"))

    if new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}
    if new_punch_in != old_pi:
        changes["punch_in"] = {"from": old_pi, "to": new_punch_in}
    if new_punch_out != old_po:
        changes["punch_out"] = {"from": old_po, "to": new_punch_out}
    if new_wh != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_wh}
    if new_late != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late}
    if new_ot != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_ot}
    if new_hd != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_hd}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    history_entry = {
        "at":      now_utc(),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": changes,
    }


    current_len = len(record.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date":     date,
            "$expr":    {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_len]},
        },
        {
            "$set": {
                "status":           new_status,
                "punch_in":         new_punch_in,
                "punch_out":        new_punch_out,
                "work_hours":       new_wh,
                "late_minutes":     new_late,
                "overtime_minutes": new_ot,
                "half_day":         new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if not result:
        raise HTTPException(409, "Concurrent edit detected; please retry")

    result.pop("_id", None)
    return serialize_record(result)




def month_range(month: str):
    import calendar
    y, m = int(month[:4]), int(month[5:])
    first = f"{y:04d}-{m:02d}-01"
    last_day = calendar.monthrange(y, m)[1]
    last = f"{y:04d}-{m:02d}-{last_day:02d}"
    return first, last


def working_days_in_range(start: str, end: str) -> int:
    sd = datetime.strptime(start, "%Y-%m-%d").date()
    ed = datetime.strptime(end,   "%Y-%m-%d").date()
    count = 0
    d = sd
    while d <= ed:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count




@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month:    str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    first, last = month_range(month)
    effective_start = max(first, emp["joined_on"])
    wd = working_days_in_range(effective_start, last)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": effective_start, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},

                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    ]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                    0
                ]
            }},
            "leave_days":             {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count":             {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes":     {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        r = rows[0]
        present_days = r["present_days"]
        leave_days   = r["leave_days"]
        late_count   = r["late_count"]
        total_late   = r["total_late_minutes"]
        total_ot     = r["total_overtime_minutes"]
    else:
        present_days = leave_days = late_count = total_late = total_ot = 0

    att_pct = round_half_up(present_days / wd * 100, 2) if wd > 0 else None

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           wd,
        "present_days":           present_days,
        "leave_days":             leave_days,
        "late_count":             late_count,
        "total_late_minutes":     total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct":         att_pct,
    }




@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first, last = month_range(month)

    emp_match: dict = {"joined_on": {"$lte": last}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let":  {"ec": "$emp_code", "joined": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$eq":  ["$emp_code", "$$ec"]},
                        {"$gte": ["$date", first]},
                        {"$lte": ["$date", last]},
                        {"$gte": ["$date", "$$joined"]},
                    ]
                }}}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "2000-01-01"]}}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "2000-01-01"]}}}}, 6]},
                    ]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1.0]},
                    0
                ]
            }},
            "work_hours_vals": {"$push": {
                "$cond": [
                    {"$and": [
                        {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": [{"$type": "$logs.work_hours"}, "null"]},
                        {"$ne": [{"$type": "$logs.work_hours"}, "missing"]},
                    ]},
                    "$logs.work_hours",
                    "$$REMOVE"
                ]
            }},
            "late_count":         {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count":        {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "LEAVE"]}, 1, 0]}},
            "on_duty_count":      {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department":         "$_id",
            "headcount":          {"$size": "$headcount"},
            "present_days":       1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": [{"$size": "$work_hours_vals"}, 0]},
                    {"$avg": "$work_hours_vals"},
                    None
                ]
            },
            "late_count":         1,
            "total_late_minutes": 1,
            "leave_count":        1,
            "on_duty_count":      1,
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = round_half_up(item["present_days"], 2)
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)

    return {"month": month, "items": items}




@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int           = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_range(month)

    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id":                "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count":         {"$sum": 1},
        }},
        {"$lookup": {
            "from":         "employees",
            "localField":   "_id",
            "foreignField": "emp_code",
            "as":           "emp",
        }},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        {"$project": {
            "_id":                0,
            "emp_code":           "$_id",
            "name":               "$emp.name",
            "department":         "$emp.department",
            "total_late_minutes": 1,
            "late_count":         1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))


    ranked = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0 and row["total_late_minutes"] < rows[i - 1]["total_late_minutes"]:
            rank = i + 1
        row["rank"] = rank
        ranked.append(row)


    result = [r for r in ranked if r["rank"] <= limit]

    return {"month": month, "items": result}




@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date:  str = Query(..., alias="from"),
    to_date:    str = Query(..., alias="to"),
):
    try:
        fd = datetime.strptime(from_date, "%Y-%m-%d").date()
        td = datetime.strptime(to_date,   "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(422, "from/to must be YYYY-MM-DD")

    if td < fd:
        raise HTTPException(422, "to must be >= from")
    if (td - fd).days > 91:
        raise HTTPException(422, "Date range must not exceed 92 days")

    dep_exists = db.employees.find_one({"department": department})
    if not dep_exists:
        raise HTTPException(404, "Department not found")


    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {
            "from":         "employees",
            "localField":   "emp_code",
            "foreignField": "emp_code",
            "as":           "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {
                "$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                    0
                ]
            }},
            "late_count": {"$sum": {
                "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
            }},
        }},
    ]

    logs_by_date = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}


    all_emps = list(db.employees.find(
        {"department": department},
        {"emp_code": 1, "joined_on": 1, "_id": 0}
    ))


    result_rows = []
    d = fd
    while d <= td:
        ds = d.isoformat()
        is_working = d.weekday() < 5
        hc = sum(1 for e in all_emps if e["joined_on"] <= ds)
        log = logs_by_date.get(ds, {})
        present_count = log.get("present_count", 0)
        late_count    = log.get("late_count", 0)

        if is_working and hc > 0:
            att_rate = round_half_up(present_count / hc, 4)
        else:
            att_rate = None

        result_rows.append({
            "date":            ds,
            "is_working_day":  is_working,
            "headcount":       hc,
            "present_count":   present_count,
            "late_count":      late_count,
            "attendance_rate": att_rate,
        })
        d += timedelta(days=1)


    rates = [row["attendance_rate"] for row in result_rows]


    for i, row in enumerate(result_rows):
        window_rates = rates[max(0, i - 6): i + 1]
        non_null = [r for r in window_rates if r is not None]
        if non_null:
            row["moving_avg_7d"] = round_half_up(sum(non_null) / len(non_null), 4)
        else:
            row["moving_avg_7d"] = None

    return {"department": department, "items": result_rows}




@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int           = Query(10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    from_date:  Optional[str] = Query(None, alias="from"),
    to_date:    Optional[str] = Query(None, alias="to"),
    page:       int           = Query(1,  ge=1),
    page_size:  int           = Query(20, ge=1, le=100),
):
    valid_eps = {"attendance_list", "employee_monthly", "department_summary",
                 "late_leaderboard", "department_trend"}
    if endpoint not in valid_eps:
        raise HTTPException(422, f"endpoint must be one of {sorted(valid_eps)}")

    if endpoint == "attendance_list":
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
        skip = (page - 1) * page_size
        cmd = {
            "explain": {
                "find":       "attendance_logs",
                "filter":     q,
                "sort":       {"date": -1, "emp_code": 1},
                "skip":       skip,
                "limit":      page_size,
            },
            "verbosity": "executionStats",
        }
        result = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        first, last = month_range(month)
        emp = db.employees.find_one({"emp_code": emp_code})
        if not emp:
            raise HTTPException(404, "Employee not found")
        effective_start = max(first, emp["joined_on"])
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": effective_start, "$lte": last}}},
            {"$group": {"_id": None, "count": {"$sum": 1}}},
        ]
        result = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        first, last = month_range(month)
        emp_match: dict = {"joined_on": {"$lte": last}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let":  {"ec": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq":  ["$emp_code", "$$ec"]},
                        {"$gte": ["$date", first]},
                        {"$lte": ["$date", last]},
                    ]}}}
                ],
                "as": "logs"
            }},
            {"$limit": 1},
        ]
        result = db.command("aggregate", "employees", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": result}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        first, last = month_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}},
            {"$limit": limit},
        ]
        result = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$group": {"_id": "$date", "present_count": {"$sum": 1}}},
        ]
        result = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": result}
