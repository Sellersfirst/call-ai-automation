from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from typing import Dict, Optional
from config.database import get_connection

router = APIRouter()


#  INNER MODEL 
class DaySchedule(BaseModel):
    start: str
    end: str


#  CREATE (Google Sheet Job) 
class SheetCreate(BaseModel):
    google_sheet_url: str
    worksheet_name: str
    agent_id: str
    status: bool = True
    batch_size: Optional[int] = None
    retries_on_voicemail: Optional[int] = 0
    schedule: Dict[str, DaySchedule]
    output_sheet_url: Optional[str] = None
    output_worksheet_name: Optional[str] = None
    variables_to_record: Optional[str] = None      # comma-separated variable names
    extraction_prompt_id: Optional[int] = None      # FK to prompts.id


#  CREATE (Salesforce Job) 
class SalesforceJobCreate(BaseModel):
    name: str                      # display name, stored as worksheet_name
    agent_id: str
    query: str                     # SOQL query to fetch leads
    query2: Optional[str] = None                  # Fallback SOQL query if primary returns no results
    status: bool = True
    batch_size: Optional[int] = None
    retries_on_voicemail: Optional[int] = 0
    schedule: Dict[str, DaySchedule]
    postcall_sheet_url: Optional[str] = None      # Google Sheet URL for post-call logging
    postcall_worksheet_name: Optional[str] = None # Worksheet name for post-call logging


#  UPDATE 
class SheetUpdate(BaseModel):
    google_sheet_url: Optional[str] = None
    worksheet_name: Optional[str] = None
    agent_id: Optional[str] = None
    status: Optional[bool] = None
    batch_size: Optional[int] = None
    retries_on_voicemail: Optional[int] = None
    query: Optional[str] = None
    query2: Optional[str] = None
    schedule: Optional[Dict[str, DaySchedule]] = None
    postcall_sheet_url: Optional[str] = None
    postcall_worksheet_name: Optional[str] = None
    output_sheet_url: Optional[str] = None
    output_worksheet_name: Optional[str] = None
    variables_to_record: Optional[str] = None
    extraction_prompt_id: Optional[int] = None


class SheetStatusUpdate(BaseModel):
    status: bool


#  HELPERS

def _insert_schedules(conn, sheet_id: int, schedule: Dict[str, DaySchedule]):
    for day, times in schedule.items():
        start = times.start
        end = times.end
        if start == end == "00:00":
            continue
        conn.execute("""
            INSERT INTO sheet_schedules (sheet_id, day_of_week, start_time, end_time)
            VALUES (%s, %s, %s, %s)
        """, (sheet_id, day.lower(), start, end))


#  CREATE — Google Sheet Job

@router.post("/sheets")
def create_sheet(data: SheetCreate):
    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO sheets (
                google_sheet_url, worksheet_name, agent_id, status, type, query, batch_size, retries_on_voicemail,
                output_sheet_url, output_worksheet_name, variables_to_record, extraction_prompt_id
            )
            VALUES (%s, %s, %s, %s, 'google_sheet_job', NULL, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            data.google_sheet_url,
            data.worksheet_name,
            data.agent_id,
            data.status,
            data.batch_size,
            data.retries_on_voicemail,
            data.output_sheet_url,
            data.output_worksheet_name,
            data.variables_to_record,
            data.extraction_prompt_id,
        ))
        sheet_id = cursor.fetchone()[0]
        _insert_schedules(conn, sheet_id, data.schedule)
        conn.commit()

    return {"id": sheet_id, "message": "Sheet job added successfully"}


#  CREATE — Salesforce Job

@router.post("/sheets/salesforce")
def create_salesforce_job(data: SalesforceJobCreate):
    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO sheets (google_sheet_url, worksheet_name, agent_id, status, type, query, query2, postcall_sheet_url, postcall_worksheet_name, batch_size, retries_on_voicemail)
            VALUES (NULL, %s, %s, %s, 'salesforce_job', %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            data.name,
            data.agent_id,
            data.status,
            data.query,
            data.query2,
            data.postcall_sheet_url,
            data.postcall_worksheet_name,
            data.batch_size,
            data.retries_on_voicemail,
        ))
        sheet_id = cursor.fetchone()[0]
        _insert_schedules(conn, sheet_id, data.schedule)
        conn.commit()

    return {"id": sheet_id, "message": "Salesforce job added successfully"}



#  GET — All Jobs (both types)

@router.get("/sheets")
def get_sheets(
    status: Optional[bool] = Query(None),
    type: Optional[str] = Query(None),   # 'google_sheet_job' or 'salesforce_job'
    limit: int = 10,
    offset: int = 0
):
    with get_connection() as conn:
        conditions = []
        params = []

        if status is not None:
            conditions.append("status=%s")
            params.append(status)

        if type is not None:
            conditions.append("type=%s")
            params.append(type)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM sheets {where_clause} ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        sheets = [dict(r) for r in rows]

        # Attach schedules
        for sheet in sheets:
            schedule_rows = conn.execute(
                "SELECT day_of_week, start_time, end_time FROM sheet_schedules WHERE sheet_id=%s",
                (sheet["id"],)
            ).fetchall()
            sheet["schedule"] = {
                r["day_of_week"]: {"start": r["start_time"], "end": r["end_time"]}
                for r in schedule_rows
            }

    return sheets


#  UPDATE


@router.put("/sheets/{sheet_id}")
def update_sheet(sheet_id: int, data: SheetUpdate):
    with get_connection() as conn:
        fields = []
        values = []

        update_data = data.dict(exclude={"schedule"}, exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                fields.append(f"{key}=%s")
                values.append(value)

        if fields:
            values.append(sheet_id)
            conn.execute(
                f"UPDATE sheets SET {', '.join(fields)} WHERE id=%s",
                values
            )

        if data.schedule is not None:
            conn.execute("DELETE FROM sheet_schedules WHERE sheet_id=%s", (sheet_id,))
            _insert_schedules(conn, sheet_id, data.schedule)

        conn.commit()

    return {"message": "Sheet updated"}


#  TOGGLE STATUS

@router.patch("/sheets/{sheet_id}/status")
def toggle_status(sheet_id: int, data: SheetStatusUpdate):
    with get_connection() as conn:
        conn.execute(
            "UPDATE sheets SET status=%s WHERE id=%s",
            (data.status, sheet_id)
        )
        conn.commit()

    return {"message": "Status updated"}



#  DELETE


@router.delete("/sheets/{sheet_id}")
def delete_sheet(sheet_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM sheet_schedules WHERE sheet_id=%s", (sheet_id,))
        conn.execute("DELETE FROM sheets WHERE id=%s", (sheet_id,))
        conn.commit()

    return {"message": "Sheet deleted"}