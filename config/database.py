from datetime import datetime
import psycopg2
import psycopg2.extras
import logging, os, time
from contextlib import contextmanager
import pytz

logger = logging.getLogger("db")


los_angeles_tz = pytz.timezone("America/Los_Angeles")
los_angeles_time = datetime.now(los_angeles_tz)
timestamp_str = los_angeles_time.strftime("%Y-%m-%d %H:%M:%S PDT")


# ---------------------------------------------------------------------------
# Conversation history — how many recent messages to send to Claude as context
# ---------------------------------------------------------------------------
CONVERSATION_HISTORY_LIMIT = 50


class _PGConn:
    """Thin wrapper so callers can use conn.execute() like sqlite3."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql, params or ())
        return cur

    def cursor(self):
        return self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


@contextmanager
def get_connection():
    conn = None
    try:
        conn = psycopg2.connect(os.getenv("POSTGRES_URL"))
        yield _PGConn(conn)
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def get_active_prompt_text(prompt_type: str = "rubrics") -> str:
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT prompt_text FROM prompts WHERE active=TRUE AND type=%s ORDER BY id LIMIT 1",
                (prompt_type,),
            ).fetchone()
            if row and row["prompt_text"]:
                return row["prompt_text"]
    except Exception as exc:
        logger.warning("Failed to load active prompt from DB: %s", exc)

    return "insert prompt here"


def get_active_prompt_with_id(prompt_type: str = "rubrics") -> tuple[str, int | None]:
    """Return (prompt_text, prompt_id) for the active prompt of the given type.

    Returns ("insert prompt here", None) when no active prompt is found.
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id, prompt_text FROM prompts WHERE active=TRUE AND type=%s ORDER BY id LIMIT 1",
                (prompt_type,),
            ).fetchone()
            if row and row["prompt_text"]:
                return row["prompt_text"], row["id"]
    except Exception as exc:
        logger.warning("Failed to load active prompt from DB: %s", exc)

    return "insert prompt here", None


def get_prompt_text_by_id(prompt_id: int) -> str | None:
    """Return the prompt_text for a specific prompt row, or None if not found."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT prompt_text FROM prompts WHERE id=%s",
                (prompt_id,),
            ).fetchone()
            if row and row["prompt_text"]:
                return row["prompt_text"]
    except Exception as exc:
        logger.warning("Failed to load prompt %s from DB: %s", prompt_id, exc)

    return None


def init_db():
    print('Initializing database...')
    with get_connection() as conn:

        #  CONFIG 
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                id INTEGER PRIMARY KEY,
                num_rows INTEGER NOT NULL CHECK(num_rows > 0)
            )
        """)

        conn.execute("""
            INSERT INTO config (id, num_rows)
            VALUES (1, 5)
            ON CONFLICT (id) DO NOTHING
        """)

        #  SHEETS 
        # type:  'google_sheet_job' (default) | 'salesforce_job'
        # query: NULL for sheet jobs, SOQL string for salesforce jobs
        conn.execute("""
        CREATE TABLE IF NOT EXISTS sheets (
            id SERIAL PRIMARY KEY,
            google_sheet_url TEXT,
            worksheet_name TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            status BOOLEAN DEFAULT TRUE,
            last_run TIMESTAMP,
            last_status TEXT,
            type TEXT NOT NULL DEFAULT 'google_sheet_job',
            query TEXT,
            query2 TEXT,
            postcall_sheet_url TEXT,
            postcall_worksheet_name TEXT,
            batch_size INTEGER,
            retries_on_voicemail INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # Drop NOT NULL on google_sheet_url for existing databases
        conn.execute("""
            ALTER TABLE sheets ALTER COLUMN google_sheet_url DROP NOT NULL
        """)

        # Migrate existing tables that don't have the new columns yet
        conn.execute("""
            ALTER TABLE sheets ADD COLUMN IF NOT EXISTS
            type TEXT NOT NULL DEFAULT 'google_sheet_job'
        """)

        conn.execute("""
            ALTER TABLE sheets ADD COLUMN IF NOT EXISTS
            query TEXT
        """)

        conn.execute("""
            ALTER TABLE sheets ADD COLUMN IF NOT EXISTS
            batch_size INTEGER
        """)

        conn.execute("""
            ALTER TABLE sheets ADD COLUMN IF NOT EXISTS
            retries_on_voicemail INTEGER DEFAULT 0
        """)

        # For salesforce_job: optional Google Sheet for post-call logging
        conn.execute("""
            ALTER TABLE sheets ADD COLUMN IF NOT EXISTS
            postcall_sheet_url TEXT
        """)

        conn.execute("""
            ALTER TABLE sheets ADD COLUMN IF NOT EXISTS
            postcall_worksheet_name TEXT
        """)

        conn.execute("""
            ALTER TABLE sheets ADD COLUMN IF NOT EXISTS
            query2 TEXT
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sheets_status
        ON sheets(status)
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sheets_type
        ON sheets(type)
        """)

        #  SCHEDULE TABLE 
        conn.execute("""
        CREATE TABLE IF NOT EXISTS sheet_schedules (
            id SERIAL PRIMARY KEY,
            sheet_id INTEGER NOT NULL,
            day_of_week TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            FOREIGN KEY(sheet_id) REFERENCES sheets(id) ON DELETE CASCADE
        )
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sheet_schedules_sheet_id
        ON sheet_schedules(sheet_id)
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sheet_schedules_day
        ON sheet_schedules(day_of_week)
        """)

        #  PROMPTS
        conn.execute("""
        CREATE TABLE IF NOT EXISTS prompts (
            id SERIAL PRIMARY KEY,
            prompt_text TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'rubrics',
            active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # Insert default prompt if table is empty
        conn.execute("""
        INSERT INTO prompts (prompt_text, type, active) 
        SELECT %s, 'rubrics', TRUE 
        WHERE NOT EXISTS (SELECT 1 FROM prompts)
        """, ("insert prompt here",))

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_prompts_active
        ON prompts(active)
        """)

        # For google_sheet_job: optional post-call variable extraction into an output sheet
        conn.execute("""
            ALTER TABLE sheets ADD COLUMN IF NOT EXISTS
            output_sheet_url TEXT
        """)

        conn.execute("""
            ALTER TABLE sheets ADD COLUMN IF NOT EXISTS
            output_worksheet_name TEXT
        """)

        conn.execute("""
            ALTER TABLE sheets ADD COLUMN IF NOT EXISTS
            variables_to_record TEXT
        """)

        conn.execute("""
            ALTER TABLE sheets ADD COLUMN IF NOT EXISTS
            extraction_prompt_id INTEGER REFERENCES prompts(id)
        """)

        # Per-variable descriptions for extraction, e.g. {"rating": "1-10 call quality score"}
        conn.execute("""
            ALTER TABLE sheets ADD COLUMN IF NOT EXISTS
            variable_descriptions JSONB
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_prompts_type
        ON prompts(type)
        """)

        #  CALL LOGS 
        conn.execute("""
        CREATE TABLE IF NOT EXISTS call_logs (
            id SERIAL PRIMARY KEY,
            conversation_id TEXT UNIQUE,
            to_number TEXT,
            from_number TEXT,
            lead_id TEXT,
            sheet_id INTEGER REFERENCES sheets(id) ON DELETE SET NULL,
            call_disposition TEXT DEFAULT 'Not Answered',
            called_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            duration_secs INTEGER,
            call_status TEXT,
            wrong_call TEXT,
            wants_to_sell TEXT,
            callback_time TEXT,
            transfer_used TEXT,
            transcript TEXT,
            lead_score TEXT,
            voicemail_retry_count INTEGER DEFAULT 0,
            updated_at TIMESTAMP
        )
        """)

        conn.execute("""
            ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS
            voicemail_retry_count INTEGER DEFAULT 0
        """)

        conn.execute("""
        ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS
        lead_score TEXT
        """)

        conn.execute("""
        ALTER TABLE prompts ADD COLUMN IF NOT EXISTS
        type TEXT NOT NULL DEFAULT 'rubrics'
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_call_logs_conversation_id
        ON call_logs(conversation_id)
        """)

        # ----------------------------------------------------------------
        #  CONVERSATION MESSAGES
        #  Stores the persistent Blake ↔ Claude conversation history.
        #  message_from: 'user' (Blake) | 'assistant' (Claude) | 'system'
        # ----------------------------------------------------------------
        conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id           SERIAL PRIMARY KEY,
            message_from TEXT      NOT NULL,
            message      TEXT      NOT NULL,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        conn.execute("""
        ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS
        prompt_id INTEGER
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_conversation_messages_id
        ON conversation_messages(id)
        """)

        conn.commit()
        logger.info("Database initialized")


# ---------------------------------------------------------------------------
# Conversation message helpers
# ---------------------------------------------------------------------------

def add_conversation_message(message_from: str, message: str, prompt_id: int | None = None) -> int:
    """
    Insert a new conversation message row.

    Args:
        message_from: 'user' | 'assistant' | 'system'
        message:      The message text.

    Returns:
        The newly created row id.
    """
    try:
        with get_connection() as conn:
            if prompt_id is None:
                cur = conn.execute(
                    """
                    INSERT INTO conversation_messages (message_from, message)
                    VALUES (%s, %s)
                    RETURNING id
                    """,
                    (message_from, message),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO conversation_messages (message_from, message, prompt_id)
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """,
                    (message_from, message, prompt_id),
                )
            row = cur.fetchone()
            conn.commit()
            new_id = row["id"]
            logger.info("Conversation message added: id=%s from=%s", new_id, message_from)
            return new_id
    except Exception as e:
        logger.error("Error adding conversation message: %s", e)
        raise


def get_all_conversation_messages(prompt_id: int | None = None) -> list[dict]:
    """
    Return every row in conversation_messages ordered oldest → newest.

    Returns:
        List of dicts with keys: id, message_from, message, created_at
    """
    try:
        with get_connection() as conn:
            if prompt_id is None:
                cur = conn.execute(
                    "SELECT id, message_from, message, created_at FROM conversation_messages ORDER BY id ASC"
                )
            else:
                cur = conn.execute(
                    """
                    SELECT id, message_from, message, created_at
                    FROM conversation_messages
                    WHERE prompt_id = %s
                    ORDER BY id ASC
                    """,
                    (prompt_id,),
                )
            rows = cur.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error("Error fetching all conversation messages: %s", e)
        raise


def get_recent_conversation_messages(limit: int = CONVERSATION_HISTORY_LIMIT, prompt_id: int | None = None, include_null: bool = False) -> list[dict]:
    """
    Return the most recent `limit` messages ordered oldest → newest
    (so they can be fed directly into the Claude messages array in order).

    Args:
        limit: Maximum number of messages to return. Defaults to
               CONVERSATION_HISTORY_LIMIT (50).

    Returns:
        List of dicts with keys: id, message_from, message, created_at
    """
    try:
        with get_connection() as conn:
            # Build query depending on whether a prompt_id filter is requested.
            if prompt_id is None:
                cur = conn.execute(
                    """
                    SELECT id, message_from, message, created_at
                    FROM (
                        SELECT id, message_from, message, created_at
                        FROM conversation_messages
                        ORDER BY id DESC
                        LIMIT %s
                    ) sub
                    ORDER BY id ASC
                    """,
                    (limit,),
                )
            else:
                if include_null:
                    cur = conn.execute(
                        """
                        SELECT id, message_from, message, created_at
                        FROM (
                            SELECT id, message_from, message, created_at
                            FROM conversation_messages
                            WHERE prompt_id = %s OR prompt_id IS NULL
                            ORDER BY id DESC
                            LIMIT %s
                        ) sub
                        ORDER BY id ASC
                        """,
                        (prompt_id, limit),
                    )
                else:
                    cur = conn.execute(
                        """
                        SELECT id, message_from, message, created_at
                        FROM (
                            SELECT id, message_from, message, created_at
                            FROM conversation_messages
                            WHERE prompt_id = %s
                            ORDER BY id DESC
                            LIMIT %s
                        ) sub
                        ORDER BY id ASC
                        """,
                        (prompt_id, limit),
                    )
            rows = cur.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error("Error fetching recent conversation messages: %s", e)
        raise


def clear_conversation_messages() -> int:
    """
    Delete all rows from conversation_messages.

    Returns:
        Number of rows deleted.
    """
    try:
        with get_connection() as conn:
            cur = conn.execute("DELETE FROM conversation_messages")
            count = cur.rowcount
            conn.commit()
            logger.info("Conversation history cleared: %d rows deleted", count)
            return count
    except Exception as e:
        logger.error("Error clearing conversation messages: %s", e)
        raise


# ---------------------------------------------------------------------------
# Existing helpers (unchanged)
# ---------------------------------------------------------------------------

_row_limit_cache: dict = {"value": None, "expires_at": 0.0}
_ROW_LIMIT_TTL = 60  # seconds


def get_row_limit() -> int:
    if _row_limit_cache["value"] is not None and time.monotonic() < _row_limit_cache["expires_at"]:
        return _row_limit_cache["value"]

    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT num_rows FROM config WHERE id=1"
            ).fetchone()

            if not row:
                raise RuntimeError("Config row missing in DB")

            _row_limit_cache["value"] = row["num_rows"]
            _row_limit_cache["expires_at"] = time.monotonic() + _ROW_LIMIT_TTL
            return _row_limit_cache["value"]

    except Exception as e:
        logger.error(f"Error fetching row limit: {e}")
        raise


def update_row_limit(new_val: int):
    if not isinstance(new_val, int) or new_val <= 0:
        raise ValueError("num_rows must be a positive integer")

    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "UPDATE config SET num_rows=%s WHERE id=1",
                (new_val,)
            )

            if cursor.rowcount == 0:
                raise RuntimeError("Failed to update config")

            conn.commit()
            _row_limit_cache["value"] = None  # invalidate cache
            logger.info(f"Row limit updated to {new_val}")

    except Exception as e:
        logger.error(f"Error updating row limit: {e}")
        raise


def create_call_log(conversation_id: str, to_number: str, from_number: str = None,
                    lead_id: str = None, sheet_id: int = None):
    try:
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO call_logs
                   (conversation_id, to_number, from_number, lead_id, sheet_id, call_disposition)
                   VALUES (%s, %s, %s, %s, %s, 'Not Answered')
                   ON CONFLICT (conversation_id) DO NOTHING""",
                (conversation_id, to_number, from_number, lead_id, sheet_id)
            )
            conn.commit()
            logger.info(f"Call log created: {conversation_id}")
    except Exception as e:
        logger.error(f"Error creating call log: {e}")
        raise


def update_call_log(conversation_id: str, call_disposition: str = None, duration_secs: int = None,
                    call_status: str = None, wrong_call: str = None, wants_to_sell: str = None,
                    callback_time: str = None, transfer_used: str = None, transcript: str = None,
                    timestamp_str: str = None, lead_score: str = None):
    try:
        if timestamp_str is None:
            karachi_tz = pytz.timezone("Asia/Karachi")
            timestamp_str = datetime.now(karachi_tz).strftime("%Y-%m-%d %H:%M:%S PKT")

        with get_connection() as conn:
            conn.execute(
                """UPDATE call_logs SET
                   call_disposition = COALESCE(%s, call_disposition),
                   duration_secs    = COALESCE(%s, duration_secs),
                   call_status      = COALESCE(%s, call_status),
                   wrong_call       = COALESCE(%s, wrong_call),
                   wants_to_sell    = COALESCE(%s, wants_to_sell),
                   callback_time    = COALESCE(%s, callback_time),
                   transfer_used    = COALESCE(%s, transfer_used),
                   transcript       = COALESCE(%s, transcript),
                   updated_at       = %s,
                   lead_score       = COALESCE(%s, lead_score)
                   WHERE conversation_id = %s""",
                (call_disposition, duration_secs, call_status, wrong_call,
                 wants_to_sell, callback_time, transfer_used, transcript,
                 timestamp_str, lead_score, conversation_id)
            )
            conn.commit()
            logger.info(f"Call log updated: {conversation_id}")
    except Exception as e:
        logger.error(f"Error updating call log: {e}")
        raise


def get_call_log(conversation_id: str):
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM call_logs WHERE conversation_id = %s",
                (conversation_id,)
            ).fetchone()
            if row:
                return dict(row)
            return None
    except Exception as e:
        logger.error(f"Error fetching call log: {e}")
        return None


def increment_voicemail_retry_count(conversation_id: str):
    """Increment the voicemail retry count for a call log."""
    try:
        with get_connection() as conn:
            conn.execute(
                "UPDATE call_logs SET voicemail_retry_count = voicemail_retry_count + 1 WHERE conversation_id = %s",
                (conversation_id,)
            )
            conn.commit()
            logger.info(f"Voicemail retry count incremented for {conversation_id}")
    except Exception as e:
        logger.error(f"Error incrementing voicemail retry count: {e}")
        raise


def can_retry_on_voicemail(conversation_id: str, retries_on_voicemail: int) -> bool:
    """Check if a call can be retried based on voicemail retry limits."""
    if retries_on_voicemail <= 0:
        return False
    
    try:
        log = get_call_log(conversation_id)
        if not log:
            return False
        
        retry_count = log.get("voicemail_retry_count", 0) or 0
        can_retry = retry_count < retries_on_voicemail
        logger.info(f"Call {conversation_id}: retry_count={retry_count}, limit={retries_on_voicemail}, can_retry={can_retry}")
        return can_retry
    except Exception as e:
        logger.error(f"Error checking retry eligibility: {e}")
        return False
