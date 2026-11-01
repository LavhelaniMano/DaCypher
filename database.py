import os
import logging
import secrets
import uuid as uuidlib
from contextlib import contextmanager

import bcrypt
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

DATABASE_URL_ENV = "DATABASE_URL"


def _get_database_url():
    try:
        postgres_secrets = st.secrets.get("postgres", {})
    except StreamlitSecretNotFoundError:
        postgres_secrets = {}

    database_url = (
        postgres_secrets.get("database_url")
        or postgres_secrets.get("url")
        or os.getenv(DATABASE_URL_ENV)
    )
    if not database_url:
        raise RuntimeError(
            "PostgreSQL connection string is missing. Set [postgres] database_url "
            "in Streamlit secrets or the DATABASE_URL environment variable."
        )
    return database_url


def _ensure_schema(pool_):
    conn = pool_.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    uid TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0,
                    level INTEGER NOT NULL DEFAULT 1,
                    uses_hint INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    email TEXT PRIMARY KEY,
                    password_hash TEXT,
                    google_sub TEXT,
                    uid TEXT NOT NULL,
                    codename TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    code_verifier TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                ALTER TABLE oauth_states
                ADD COLUMN IF NOT EXISTS code_verifier TEXT;
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS leaderboard (
                    username TEXT PRIMARY KEY,
                    score INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            # Classroom groups. A group is joined with a short shareable key;
            # each membership keeps its own points so one agent can take part
            # in several groups without their progress leaking between them.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS study_groups (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    join_code TEXT NOT NULL UNIQUE,
                    owner_uid TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS group_members (
                    group_id TEXT NOT NULL
                        REFERENCES study_groups(id) ON DELETE CASCADE,
                    uid TEXT NOT NULL,
                    codename TEXT NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0,
                    missions INTEGER NOT NULL DEFAULT 0,
                    total_seconds INTEGER NOT NULL DEFAULT 0,
                    joined_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (group_id, uid)
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS group_members_uid_idx
                ON group_members (uid);
                """
            )
            # Supabase exposes the `public` schema through PostgREST, so any
            # table without RLS is reachable with the public anon key. This app
            # never uses PostgREST (psycopg2 connects as the table owner, which
            # bypasses RLS), so enabling RLS with no policies closes that
            # exposure without affecting the app. Do NOT use FORCE ROW LEVEL
            # SECURITY: that would apply RLS to the owner too and break writes.
            for table in (
                "users",
                "accounts",
                "leaderboard",
                "oauth_states",
                "study_groups",
                "group_members",
            ):
                cur.execute(
                    f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;"
                )
        conn.commit()
    finally:
        pool_.putconn(conn)


@st.cache_resource
def _get_pool():
    connection_pool = pool.SimpleConnectionPool(1, 10, dsn=_get_database_url())
    _ensure_schema(connection_pool)
    return connection_pool


@contextmanager
def _cursor(commit=False):
    connection_pool = _get_pool()
    conn = connection_pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        connection_pool.putconn(conn)


def _upsert_leaderboard(cur, username, score):
    cur.execute(
        """
        INSERT INTO leaderboard (username, score)
        VALUES (%s, %s)
        ON CONFLICT (username) DO UPDATE SET score = EXCLUDED.score;
        """,
        (username, score),
    )


# ---------- DATABASE FUNCTIONS ----------

def create_user(uid, username):
    """Create user account if not exists"""
    with _cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO users (uid, username, score, level, uses_hint)
            VALUES (%s, %s, 0, 1, 0)
            ON CONFLICT (uid) DO NOTHING;
            """,
            (uid, username),
        )


def get_user(uid):
    with _cursor() as cur:
        cur.execute(
            "SELECT username, score, level, uses_hint FROM users WHERE uid = %s;",
            (uid,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def update_score(uid, new_score):
    with _cursor(commit=True) as cur:
        cur.execute(
            "UPDATE users SET score = %s WHERE uid = %s RETURNING username;",
            (new_score, uid),
        )
        row = cur.fetchone()
        if row:
            _upsert_leaderboard(cur, row["username"], new_score)


def update_level(uid, new_level):
    with _cursor(commit=True) as cur:
        cur.execute("UPDATE users SET level = %s WHERE uid = %s;", (new_level, uid))


def increment_hint_usage(uid):
    with _cursor(commit=True) as cur:
        cur.execute(
            "UPDATE users SET uses_hint = uses_hint + 1 WHERE uid = %s;",
            (uid,),
        )


def update_leaderboard(username, score):
    """Leaderboard sorted by score"""
    with _cursor(commit=True) as cur:
        _upsert_leaderboard(cur, username, score)


def save_progress(uid, username, level, score):
    with _cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO users (uid, username, level, score)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (uid) DO UPDATE SET
                username = EXCLUDED.username,
                level = EXCLUDED.level,
                score = EXCLUDED.score;
            """,
            (uid, username, level, score),
        )


def get_progress(uid):
    return get_user(uid)


# ---------- GROUP / CLASSROOM FUNCTIONS ----------

# No 0/O/1/I so keys are easy to read out loud in a lecture room.
GROUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
GROUP_CODE_LENGTH = 6


def _normalise_group_code(code):
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def _new_group_code():
    return "".join(
        secrets.choice(GROUP_CODE_ALPHABET) for _ in range(GROUP_CODE_LENGTH)
    )


def _free_group_code(cur):
    for _ in range(12):
        candidate = _new_group_code()
        cur.execute(
            "SELECT 1 FROM study_groups WHERE join_code = %s;", (candidate,)
        )
        if not cur.fetchone():
            return candidate
    return None


def create_group(name, owner_uid, owner_codename, owner_score=0):
    """Create a group, add the owner as first member, and return the group row."""
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Enter a group name.")

    group_id = str(uuidlib.uuid4())
    with _cursor(commit=True) as cur:
        join_code = _free_group_code(cur)
        if not join_code:
            raise RuntimeError("Could not allocate a group key. Please try again.")
        cur.execute(
            """
            INSERT INTO study_groups (id, name, join_code, owner_uid)
            VALUES (%s, %s, %s, %s);
            """,
            (group_id, name, join_code, owner_uid),
        )
        cur.execute(
            """
            INSERT INTO group_members (group_id, uid, codename, score)
            VALUES (%s, %s, %s, %s);
            """,
            (group_id, owner_uid, owner_codename, int(owner_score or 0)),
        )
        cur.execute("SELECT * FROM study_groups WHERE id = %s;", (group_id,))
        return dict(cur.fetchone())


def find_group_by_code(code):
    code = _normalise_group_code(code)
    if not code:
        return None
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM study_groups WHERE join_code = %s;", (code,)
        )
        row = cur.fetchone()
    return dict(row) if row else None


def join_group(code, uid, codename, score=0):
    """Add uid to the group with this key. Returns the group, or None if invalid."""
    group = find_group_by_code(code)
    if not group:
        return None
    with _cursor(commit=True) as cur:
        cur.execute(
            "SELECT 1 FROM group_members WHERE group_id = %s AND uid = %s;",
            (group["id"], uid),
        )
        if not cur.fetchone():
            cur.execute(
                """
                INSERT INTO group_members (group_id, uid, codename, score)
                VALUES (%s, %s, %s, %s);
                """,
                (group["id"], uid, codename, int(score or 0)),
            )
    return group


def get_groups_for_uid(uid):
    """Groups this agent belongs to, with their member counts."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT g.id, g.name, g.join_code, g.owner_uid, g.created_at,
                   COUNT(m.uid) AS member_count
            FROM study_groups g
            JOIN group_members me ON me.group_id = g.id AND me.uid = %s
            LEFT JOIN group_members m ON m.group_id = g.id
            GROUP BY g.id, g.name, g.join_code, g.owner_uid, g.created_at
            ORDER BY g.created_at;
            """,
            (uid,),
        )
        return [dict(row) for row in cur.fetchall()]


def get_group_standings(group_id):
    """Ranked members: most group points, then most missions, then least time."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT uid, codename, score, missions, total_seconds, joined_at
            FROM group_members
            WHERE group_id = %s
            ORDER BY score DESC, missions DESC, total_seconds ASC, joined_at ASC;
            """,
            (group_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def record_group_progress(uid, points, elapsed_seconds=0):
    """Credit a solved mission to every group this agent currently belongs to."""
    points = int(points)
    elapsed = max(0, int(elapsed_seconds or 0))
    with _cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE group_members
            SET score = score + %s,
                missions = missions + 1,
                total_seconds = total_seconds + %s
            WHERE uid = %s;
            """,
            (points, elapsed, uid),
        )


def rotate_group_code(group_id, owner_uid):
    """Give the group a fresh join key. Only the owner may do this."""
    with _cursor(commit=True) as cur:
        cur.execute(
            "SELECT 1 FROM study_groups WHERE id = %s AND owner_uid = %s;",
            (group_id, owner_uid),
        )
        if not cur.fetchone():
            return None
        new_code = _free_group_code(cur)
        if not new_code:
            return None
        cur.execute(
            "UPDATE study_groups SET join_code = %s WHERE id = %s;",
            (new_code, group_id),
        )
        return new_code


def leave_group(group_id, uid):
    with _cursor(commit=True) as cur:
        cur.execute(
            "DELETE FROM group_members WHERE group_id = %s AND uid = %s;",
            (group_id, uid),
        )


def get_leaderboard(limit=50):
    with _cursor() as cur:
        cur.execute(
            """
            SELECT username, score FROM leaderboard
            ORDER BY score DESC, username ASC
            LIMIT %s;
            """,
            (int(limit),),
        )
        return [dict(row) for row in cur.fetchall()]


# ---------- ACCOUNT / AUTH FUNCTIONS ----------

def uid_for_email(email):
    """Stable user id derived from the account email."""
    return str(uuidlib.uuid5(uuidlib.NAMESPACE_DNS, email.lower().strip()))


def _hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _check_password(password, password_hash):
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except ValueError:
        return False


def create_account(email, password):
    """Register a new account. Returns True if created, False if email exists."""
    uid = uid_for_email(email)
    with _cursor(commit=True) as cur:
        cur.execute("SELECT 1 FROM accounts WHERE email = %s;", (email,))
        if cur.fetchone():
            return False
        cur.execute(
            """
            INSERT INTO accounts (email, password_hash, uid, codename)
            VALUES (%s, %s, %s, NULL);
            """,
            (email, _hash_password(password), uid),
        )
        return True


def get_account(email):
    with _cursor() as cur:
        cur.execute(
            "SELECT email, password_hash, google_sub, uid, codename "
            "FROM accounts WHERE email = %s;",
            (email,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def verify_account(email, password):
    """Return the account dict if credentials are valid, else None."""
    account = get_account(email)
    if not account or not account.get("password_hash"):
        return None
    if not _check_password(password, account["password_hash"]):
        return None
    return account


def set_codename(email, codename):
    with _cursor(commit=True) as cur:
        cur.execute(
            "UPDATE accounts SET codename = %s WHERE email = %s;",
            (codename, email),
        )


def upsert_google_account(email, google_sub):
    """Create or link an account for a Google sign-in."""
    uid = uid_for_email(email)
    with _cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO accounts (email, google_sub, uid, codename)
            VALUES (%s, %s, %s, NULL)
            ON CONFLICT (email) DO UPDATE SET google_sub = EXCLUDED.google_sub;
            """,
            (email, google_sub, uid),
        )
    return get_account(email)


# ---------- OAUTH STATE HELPERS ----------

def _ensure_oauth_state_schema(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY,
            code_verifier TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """
    )
    cur.execute(
        """
        ALTER TABLE oauth_states
        ADD COLUMN IF NOT EXISTS code_verifier TEXT;
        """
    )


def store_oauth_state(state, code_verifier):
    """Persist OAuth state and its PKCE verifier through the browser redirect."""
    if not state:
        return
    try:
        with _cursor(commit=True) as cur:
            _ensure_oauth_state_schema(cur)
            cur.execute(
                "DELETE FROM oauth_states WHERE created_at < NOW() - INTERVAL '15 minutes';"
            )
            cur.execute(
                """
                INSERT INTO oauth_states (state, code_verifier)
                VALUES (%s, %s)
                ON CONFLICT (state) DO UPDATE
                SET code_verifier = EXCLUDED.code_verifier;
                """,
                (state, code_verifier),
            )
    except Exception:
        # Best-effort: if the DB is briefly unavailable, fall back to the
        # session-only state check rather than crashing the login page.
        logging.exception("Could not persist Google OAuth state.")


def consume_oauth_state(state):
    """Consume OAuth state and return its PKCE verifier, if it was persisted."""
    with _cursor(commit=True) as cur:
        _ensure_oauth_state_schema(cur)
        cur.execute(
            "DELETE FROM oauth_states WHERE state = %s RETURNING state, code_verifier;",
            (state,),
        )
        return cur.fetchone()
