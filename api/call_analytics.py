from fastapi import APIRouter, Depends
from config.database import get_connection
from core.auth import get_current_user, get_accessible_sheet_ids

router = APIRouter()

@router.get("/analytics")
def get_call_analytics(user: dict = Depends(get_current_user)):
    accessible_ids = get_accessible_sheet_ids(user)
    scope_clause = "" if accessible_ids is None else "AND sheet_id = ANY(%s)"
    scope_where = "" if accessible_ids is None else "WHERE sheet_id = ANY(%s)"
    scope_params = () if accessible_ids is None else (accessible_ids,)

    with get_connection() as conn:

        #  SUMMARY
        total_calls = conn.execute(
            f"SELECT COUNT(*) FROM call_logs {scope_where}", scope_params
        ).fetchone()[0]

        answered = conn.execute(f"""
            SELECT COUNT(*) FROM call_logs
            WHERE call_disposition = 'Answered' {scope_clause}
        """, scope_params).fetchone()[0]

        unanswered = conn.execute(f"""
            SELECT COUNT(*) FROM call_logs
            WHERE call_disposition != 'Answered' {scope_clause}
        """, scope_params).fetchone()[0]

        transferred = conn.execute(f"""
            SELECT COUNT(*) FROM call_logs
            WHERE transfer_used = 'True' {scope_clause}
        """, scope_params).fetchone()[0]

        wrong_numbers = conn.execute(f"""
            SELECT COUNT(*) FROM call_logs
            WHERE wrong_call IS NOT NULL AND wrong_call NOT IN ('', 'no', 'No', 'false', 'False', 'None') {scope_clause}
        """, scope_params).fetchone()[0]

        avg_duration = conn.execute(
            f"SELECT AVG(duration_secs) FROM call_logs {scope_where}", scope_params
        ).fetchone()[0] or 0

        #  TREND (GROUP BY DATE)
        trend_rows = conn.execute(f"""
            SELECT
                called_at::date as date,
                COUNT(*) as made,
                SUM(CASE WHEN call_disposition = 'Answered' THEN 1 ELSE 0 END) as answered,
                SUM(CASE WHEN call_disposition != 'Answered' THEN 1 ELSE 0 END) as unanswered
            FROM call_logs
            {scope_where}
            GROUP BY called_at::date
            ORDER BY called_at::date DESC
            LIMIT 7
        """, scope_params).fetchall()

        trend_rows = list(reversed(trend_rows))

        trend_data = [
            {
                "date": row["date"].strftime("%Y-%m-%d") if hasattr(row["date"], "strftime") else row["date"],
                "made": row["made"],
                "answered": row["answered"],
                "unanswered": row["unanswered"],
            }
            for row in trend_rows
        ]

        #  CATEGORY
        category_data = [
            {"name": "Transferred", "value": transferred},
            {"name": "Wrong Number", "value": wrong_numbers},
        ]

        return {
            "summary": {
                "total_calls": total_calls,
                "answered": answered,
                "unanswered": unanswered,
                "transferred": transferred,
                "wrong_numbers": wrong_numbers,
                "avg_duration": round(avg_duration, 2),
            },
            "trend": trend_data,
            "category": category_data,
        }
