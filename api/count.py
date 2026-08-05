from fastapi import APIRouter, Depends
from config.database import get_connection
from core.auth import get_current_user, get_accessible_sheet_ids

router = APIRouter()

@router.get("/sheets/stats")
def get_sheet_stats(user: dict = Depends(get_current_user)):
    accessible_ids = get_accessible_sheet_ids(user)
    with get_connection() as conn:
        cursor = conn.cursor()

        if accessible_ids is None:
            cursor.execute("""
                SELECT
                    SUM(CASE WHEN status = TRUE THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN status = FALSE THEN 1 ELSE 0 END) AS inactive,
                    COUNT(*) AS total
                FROM sheets
            """)
        else:
            cursor.execute("""
                SELECT
                    SUM(CASE WHEN status = TRUE THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN status = FALSE THEN 1 ELSE 0 END) AS inactive,
                    COUNT(*) AS total
                FROM sheets
                WHERE id = ANY(%s)
            """, (accessible_ids,))

        row = cursor.fetchone()

        return {
            "active": row[0] or 0,
            "inactive": row[1] or 0,
            "total": row[2] or 0
        }