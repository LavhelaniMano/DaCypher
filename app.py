# app.py — Agency Intel Ops (Streamlit UI)

import streamlit as st
import json, random, time, logging, traceback
import html
import os
import re
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
from google_auth_oauthlib.flow import Flow
from streamlit.errors import StreamlitSecretNotFoundError

# =========================
# CONFIG
# =========================
st.set_page_config(page_title="Agency Intel Ops", layout="centered")
logging.getLogger("streamlit").setLevel(logging.ERROR)

# =========================
# GLOBAL CSS (WHITE TEXT IN DARK BOXES)
# =========================
st.markdown("""
<style>
.dark-box {
    background:#021413;
    color:#ffffff !important;
    padding:14px;
    border-radius:8px;
    font-family: monospace;
    border:1px solid rgba(0,195,122,0.25);
}
.dark-box * {
    color:#ffffff !important;
}
.log-box {
    background:#021413;
    color:#ffffff !important;
    padding:12px;
    border-radius:6px;
    margin-bottom:8px;
    font-family: monospace;
}
</style>
""", unsafe_allow_html=True)

# =========================
# FILES
# =========================
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
WORD_BANK_FILE = APP_DIR / "wordbank.json"

# =========================
# LOAD WORDBANK
# =========================
def _load_wordbank():
    try:
        secrets = st.secrets.get("wordbank", {})
    except StreamlitSecretNotFoundError:
        secrets = {}
    stored = secrets.get("json") or os.getenv("WORDBANK_JSON")
    if stored:
        return json.loads(stored)
    if WORD_BANK_FILE.exists():
        return json.loads(WORD_BANK_FILE.read_text())
    return None

word_bank = _load_wordbank()
if word_bank is None:
    st.error(
        "Missing mission word bank. Add the wordbank JSON to Streamlit "
        "secrets ([wordbank] json) or place wordbank.json beside app.py."
    )
    st.stop()

# =========================
# SESSION STATE DEFAULTS
# =========================
defaults = {
    "auth_email": None,
    "codename": None,
    "oauth_state": None,
    "oauth_code_verifier": None,
    "player": "",
    "uid": None,
    "uid_loaded": False,
    "level": 1,
    "score": 0,
    "mission_started": False,
    "start_time": None,
    "current_entry": None,
    "current_cipher": "",
    "show_clues": False,
    "history": [],
    "hint_uses": 0,
    "nav_page": "🎯 Mission",
    "active_group_id": None,
    "pending_group_id": None,
    "mission_flash": None,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# =========================
# SESSION HELPERS
# =========================
def logout():
    """Forget the current agent and return to the login screen."""
    for key in list(defaults) + [
        "login_email",
        "login_password",
        "reg_email",
        "reg_password",
    ]:
        st.session_state.pop(key, None)
    st.rerun()


# =========================
# IMPORT GAME + DATABASE
# =========================
from Cipher import encode_for_level
from database import (
    create_user,
    get_progress,
    update_score,
    update_level,
    increment_hint_usage,
    update_leaderboard,
    save_progress,
    create_account,
    get_account,
    verify_account,
    set_codename,
    upsert_google_account,
    uid_for_email,
    store_oauth_state,
    consume_oauth_state,
    create_group,
    join_group,
    get_groups_for_uid,
    get_group_standings,
    record_group_progress,
    rotate_group_code,
    leave_group,
    get_leaderboard,
)

# =========================
# MISSION HELPERS
# =========================
def pick_entry(level):
    entries = word_bank.get(str(level), [])
    return random.choice(entries) if entries else None


def start_new_mission():
    entry = pick_entry(st.session_state.level)
    if not entry:
        st.session_state.mission_started = False
        return

    words = [w for w in entry["answer"].upper().split() if w.isalpha()]
    cipher = encode_for_level(words, st.session_state.level)

    st.session_state.current_entry = entry
    st.session_state.current_cipher = cipher
    st.session_state.start_time = time.time()
    st.session_state.mission_started = True
    st.session_state.show_clues = False


# =========================
# GROUP ARENA
# =========================

def format_duration(seconds):
    seconds = max(0, int(round(float(seconds or 0))))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def par_seconds(level):
    """Target time for a mission. Faster than this earns a speed bonus."""
    return 90 + 20 * int(level)


def speed_bonus(level, elapsed_seconds):
    """Group points awarded for speed: up to the mission's base value."""
    if elapsed_seconds is None:
        return 0
    base = 5 + int(level) // 2
    par = par_seconds(level)
    remaining = max(0.0, par - float(elapsed_seconds))
    return int(round(base * remaining / par))


def _set_active_group(group_id):
    st.session_state.active_group_id = group_id
    # Applied at the top of the next run, before the picker is created.
    st.session_state.pending_group_id = group_id


def render_group_arena():
    uid = st.session_state.uid
    codename = st.session_state.player or st.session_state.codename or "Agent"

    st.markdown("## 🏫 Group Arena")
    st.caption(
        "Create a group, share the key with your class, and race to the top of "
        "the standings. Group points are earned by missions you solve after "
        "joining, and quick solves earn a speed bonus."
    )

    # Widget state may only be written before the widget exists in this run.
    pending_group_id = st.session_state.pop("pending_group_id", None)
    if pending_group_id:
        st.session_state.active_group_id = pending_group_id
        st.session_state["group_picker"] = pending_group_id

    flash = st.session_state.pop("group_flash", None)
    if flash:
        st.success(flash)

    try:
        my_groups = get_groups_for_uid(uid)
    except Exception:
        logging.exception("Could not load groups.")
        st.error("The group board is temporarily unavailable. Please try again.")
        my_groups = []

    if my_groups:
        by_id = {group["id"]: group for group in my_groups}
        ids = list(by_id)
        if st.session_state.active_group_id not in by_id:
            st.session_state.active_group_id = ids[0]
            st.session_state["group_picker"] = ids[0]

        active_id = st.selectbox(
            "Group",
            options=ids,
            index=ids.index(st.session_state.active_group_id),
            format_func=lambda gid: by_id[gid]["name"],
            key="group_picker",
        )
        st.session_state.active_group_id = active_id
        group = by_id[active_id]
        is_owner = group["owner_uid"] == uid

        st.markdown(
            f"""
            <div class="dark-box" style="font-size:15px;">
                <b>{html.escape(group['name'])}</b><br>
                {group['member_count']} member(s) &middot; you are
                {'the group owner' if is_owner else 'a member'}<br><br>
                Join key:
                <b style="letter-spacing:5px;font-size:20px;">{group['join_code']}</b><br>
                <span style="color:#8fc8b8;">Share this key &mdash; anyone with it can join.</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown("### 🏆 Group Standings")
        try:
            standings = get_group_standings(active_id)
        except Exception:
            logging.exception("Could not load group standings.")
            standings = []

        if standings:
            rows = []
            my_rank = None
            for rank, row in enumerate(standings, start=1):
                if row["uid"] == uid:
                    my_rank = rank
                rows.append(
                    {
                        "Rank": rank,
                        "Agent": row["codename"],
                        "Points": row["score"],
                        "Solved": row["missions"],
                        "Total time": format_duration(row["total_seconds"]),
                        "Avg time": (
                            format_duration(row["total_seconds"] / row["missions"])
                            if row["missions"]
                            else "—"
                        ),
                    }
                )
            st.dataframe(
                pd.DataFrame(rows), hide_index=True, use_container_width=True
            )
            if my_rank:
                st.caption(f"Your position: #{my_rank} of {len(rows)}")
        else:
            st.info("No one has solved a mission in this group yet.")

        st.caption(
            f"Speed bonus: each solve pays its base points, plus up to the same "
            f"again for speed. Par time is {par_seconds(1)}s at level 1 and grows "
            f"by 20s per level; the bonus shrinks as you take longer and reaches "
            f"zero at par."
        )

        top_left, top_right = st.columns(2)
        with top_left:
            if is_owner:
                if st.button("🔄 Generate new join key", use_container_width=True):
                    try:
                        new_code = rotate_group_code(active_id, uid)
                    except Exception:
                        logging.exception("Could not rotate group key.")
                        new_code = None
                    if new_code:
                        st.session_state.group_flash = (
                            f"New join key: {new_code} — share the new key."
                        )
                        st.rerun()
                    else:
                        st.error("Could not create a new key. Please try again.")
            else:
                st.caption("Only the group owner can change the join key.")
        with top_right:
            if st.button("🚪 Leave this group", use_container_width=True):
                try:
                    leave_group(active_id, uid)
                except Exception:
                    logging.exception("Could not leave group.")
                    st.error("Could not leave the group. Please try again.")
                else:
                    st.session_state.active_group_id = None
                    st.session_state.group_flash = f"You left {group['name']}."
                    st.rerun()
    else:
        st.session_state.active_group_id = None
        st.info(
            "You are not in a group yet. Create one below, or join with the key "
            "your instructor gave you."
        )

    st.divider()
    create_col, join_col = st.columns(2)

    with create_col:
        st.markdown("### ➕ Create a group")
        st.caption("You become the owner and get a key to share.")
        with st.form("create_group_form", clear_on_submit=True):
            group_name = st.text_input("Group name", max_chars=60)
            create_clicked = st.form_submit_button("CREATE GROUP")

        if create_clicked:
            if not (group_name or "").strip():
                st.error("Enter a group name.")
            else:
                try:
                    created = create_group(group_name, uid, codename)
                except ValueError as exc:
                    created = False
                    st.error(str(exc))
                except Exception:
                    logging.exception("Could not create group.")
                    created = False
                    st.error("The database is temporarily unavailable. Please try again.")
                if created:
                    _set_active_group(created["id"])
                    st.session_state.group_flash = (
                        f"Group '{created['name']}' created. "
                        f"Share join key {created['join_code']} with your class."
                    )
                    st.rerun()

    with join_col:
        st.markdown("### 🔑 Join a group")
        st.caption("Ask your instructor for the group key.")
        with st.form("join_group_form", clear_on_submit=True):
            entered_code = st.text_input("Join key", max_chars=12)
            join_clicked = st.form_submit_button("JOIN GROUP")

        if join_clicked:
            if not (entered_code or "").strip():
                st.error("Enter the join key you were given.")
            else:
                try:
                    joined = join_group(entered_code, uid, codename)
                except Exception:
                    logging.exception("Could not join group.")
                    joined = False
                    st.error("The database is temporarily unavailable. Please try again.")
                if joined:
                    _set_active_group(joined["id"])
                    st.session_state.group_flash = (
                        f"Joined '{joined['name']}'. Missions you solve from now "
                        f"on count for this group."
                    )
                    st.rerun()
                elif joined is None:
                    st.error(
                        "That key did not match a group. Check it with your instructor."
                    )


def render_global_leaderboard():
    st.markdown("## 🏆 Global Standings")
    st.caption("Every agent, ranked by total mission points.")
    try:
        rows = get_leaderboard(50)
    except Exception:
        logging.exception("Could not load leaderboard.")
        st.error("The leaderboard is temporarily unavailable. Please try again.")
        return

    if not rows:
        st.info("No scores yet — solve a mission to appear here.")
        return

    table = pd.DataFrame(
        [
            {"Rank": rank, "Agent": row["username"], "Score": row["score"]}
            for rank, row in enumerate(rows, start=1)
        ]
    )
    st.dataframe(table, hide_index=True, use_container_width=True)
    my_name = st.session_state.player
    my_row = next((r for r in rows if r["username"] == my_name), None)
    if my_row:
        st.caption(
            f"You are on the global board at {my_row['score']} points. "
            f"Use the Group Arena to compete with your class."
        )


# =========================
# USER LOADING
# =========================
def load_player(uid, codename):
    """Load (or create) the player's persisted progress for the account uid."""
    create_user(uid, codename)
    data = get_progress(uid)

    if not data:
        save_progress(uid, codename, 1, 0)
        data = {"level": 1, "score": 0, "uses_hint": 0}

    st.session_state.player = codename
    st.session_state.codename = codename
    st.session_state.uid = uid
    st.session_state.level = int(data.get("level", 1))
    st.session_state.score = int(data.get("score", 0))
    st.session_state.hint_uses = int(data.get("uses_hint", 0))
    st.session_state.uid_loaded = True

# =========================
# GOOGLE OAUTH HELPERS
# =========================
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

def _google_secrets():
    try:
        return st.secrets.get("google", {})
    except StreamlitSecretNotFoundError:
        return {}

def _google_client_config(secret):
    return {
        "web": {
            "client_id": secret["client_id"],
            "client_secret": secret["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

def _google_redirect_base():
    secret = _google_secrets()
    configured = secret.get("redirect_uri") or os.getenv("OAUTH_REDIRECT_URI")
    if configured:
        return configured.rstrip("/")
    try:
        host = st.context.headers.get("Host")
    except Exception:
        host = None
    if host:
        scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
        return f"{scheme}://{host}"
    return "http://localhost:8501"

def google_auth_url():
    flow = Flow.from_client_config(
        _google_client_config(_google_secrets()),
        scopes=GOOGLE_SCOPES,
    )
    flow.redirect_uri = _google_redirect_base()
    auth_url, state = flow.authorization_url(prompt="select_account")
    st.session_state.oauth_state = state
    st.session_state.oauth_code_verifier = flow.code_verifier
    store_oauth_state(state, flow.code_verifier)
    return auth_url

def handle_google_callback():
    code = st.query_params.get("code")
    if not code:
        return

    try:
        state = st.query_params.get("state")
        session_state_valid = (
            state is not None and state == st.session_state.get("oauth_state")
        )
        code_verifier = st.session_state.get("oauth_code_verifier")
        persisted_oauth_state = None
        if state is not None and (not session_state_valid or not code_verifier):
            persisted_oauth_state = consume_oauth_state(state)
        state_valid = session_state_valid or persisted_oauth_state is not None
        if not state_valid:
            st.error("Google sign-in failed (state mismatch). Please try again.")
            _clear_oauth_params()
            return

        secret = _google_secrets()
        if persisted_oauth_state is not None:
            code_verifier = (
                persisted_oauth_state.get("code_verifier") or code_verifier
            )
        if not code_verifier:
            st.error("Google sign-in session expired. Please try again.")
            _clear_oauth_params()
            return
        flow = Flow.from_client_config(
            _google_client_config(secret),
            scopes=GOOGLE_SCOPES,
            code_verifier=code_verifier,
        )
        flow.redirect_uri = _google_redirect_base()
        flow.fetch_token(code=code)
        session = flow.authorized_session()
        info = session.get("https://www.googleapis.com/oauth2/v2/userinfo").json()

        _clear_oauth_params()

        email = (info.get("email") or "").strip().lower()
        if not email:
            st.error("Could not retrieve your Google email.")
            return

        upsert_google_account(email, str(info.get("id") or email))
        st.session_state.auth_email = email
        st.rerun()
    except Exception:
        _clear_oauth_params()
        st.error("Google sign-in could not be completed. Error details:")
        st.code(traceback.format_exc())

def _clear_oauth_params():
    for key in ("code", "state", "scope", "authuser", "prompt", "hd"):
        if key in st.query_params:
            del st.query_params[key]
    st.session_state.oauth_state = None
    st.session_state.oauth_code_verifier = None

def validate_email(email):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))

# =========================
# LOGIN GATE
# =========================
handle_google_callback()

if not st.session_state.auth_email:
    st.markdown("""
    <style>
    .login-title {
        background: linear-gradient(135deg, #021413, #04302b);
        color: #eafff7 !important;
        border: 1px solid rgba(0,195,122,0.35);
        border-radius: 10px;
        padding: 18px 20px;
        margin-bottom: 18px;
        font-family: monospace;
        box-shadow: 0 4px 18px rgba(0,0,0,0.35);
    }
    .login-title .topbar { letter-spacing: 3px; font-size: 12px; color: #00c37a; }
    .login-title h1 { color: #ffffff !important; font-size: 22px; margin: 6px 0 4px; }
    .login-title .sub { font-size: 12px; color: #8fc8b8; }
    .login-card {
        background: #021413;
        color: #eafff7 !important;
        border: 1px solid rgba(0,195,122,0.25);
        border-radius: 10px;
        padding: 16px 18px;
        margin-bottom: 14px;
    }
    .login-card h3 { color: #00c37a !important; font-family: monospace; letter-spacing: 2px; font-size: 13px; margin: 0 0 12px; }
    .login-hint { font-size: 12px; color: #8fc8b8; margin-top: 10px; font-family: monospace; }
    .login-sect h3 {
        color: #00c37a !important; font-family: monospace; letter-spacing: 2px;
        font-size: 13px; border-bottom: 1px solid rgba(0,195,122,0.3);
        padding-bottom: 6px; margin: 16px 0 10px;
    }
    .g-btn {
        display: flex; align-items: center; justify-content: center; gap: 10px;
        background: #ffffff; color: #333333 !important; font-weight: 600;
        font-family: Roboto, Arial, sans-serif; font-size: 15px;
        border: 1px solid #dadce0; border-radius: 6px; padding: 10px 16px;
        text-decoration: none; width: 100%; box-sizing: border-box;
    }
    .g-btn svg { width: 18px; height: 18px; flex: 0 0 auto; }
    .g-btn:hover { background: #f7f9fc; box-shadow: 0 1px 3px rgba(0,0,0,0.25); }
    .divider { display: flex; align-items: center; color: #6b7c77; margin: 16px 0; font-family: monospace; font-size: 12px; letter-spacing: 1px; }
    .divider::before, .divider::after { content: ""; flex: 1; border-top: 1px solid rgba(0,195,122,0.25); }
    .divider span { padding: 0 10px; }
    .stButton > button, .stFormSubmitButton > button {
        width: 100%; background: #00c37a; color: #021413; font-weight: 700;
        border: none; border-radius: 6px; padding: 0.6rem 1rem; letter-spacing: 1px;
    }
    .stButton > button:hover, .stFormSubmitButton > button:hover { background: #00d68a; color: #021413; }
    .stTextInput input {
        background: #03201d; color: #ffffff;
        border: 1px solid rgba(0,195,122,0.35); border-radius: 6px;
    }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="login-title">
        <div class="topbar">// AGENCY INTELLIGENCE NETWORK</div>
        <h1>🕵️ Agency Intel Ops — Decryption Terminal</h1>
        <div class="sub">Agent identity verification required to access the terminal.</div>
    </div>
    """, unsafe_allow_html=True)

    goog_svg = """<svg viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>"""

    try:
        google_configured = bool(
            _google_secrets().get("client_id")
            and _google_secrets().get("client_secret")
        )
    except Exception:
        google_configured = False

    if google_configured:
        st.markdown(
            f"""
            <div class="login-card">
                <h3>⚡ SIGN IN WITH GOOGLE — INSTANT ACCESS</h3>
                <a class="g-btn" href="{google_auth_url()}">{goog_svg}Continue with Google</a>
                <div class="login-hint">Authenticate with your Gmail account. No password needed.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.info("Google sign-in is not configured yet.")

    st.markdown('<div class="divider"><span>OR CONTINUE WITH EMAIL</span></div>', unsafe_allow_html=True)

    auth_mode = st.segmented_control(
        "Account action",
        options=["Log In", "Create Account"],
        default="Log In",
        label_visibility="collapsed",
    )

    if auth_mode == "Create Account":
        st.markdown('<div class="login-sect"><h3>✍️ NEW AGENT — CREATE ACCOUNT</h3></div>', unsafe_allow_html=True)
        with st.form("register_form", clear_on_submit=False):
            reg_email = st.text_input("Email", key="reg_email")
            reg_password = st.text_input("Password", type="password", key="reg_password")
            st.caption("Minimum 6 characters. You will choose a codename after signing up.")
            register_submitted = st.form_submit_button("CREATE ACCOUNT")

        if register_submitted:
            reg_email = (reg_email or "").strip().lower()
            if not validate_email(reg_email):
                st.error("Enter a valid email address.")
            elif not reg_password or len(reg_password) < 6:
                st.error("Password must be at least 6 characters.")
            else:
                try:
                    created = create_account(reg_email, reg_password)
                except Exception:
                    created = False
                    st.error("The database is temporarily unavailable. Check your Postgres connection and try again.")
                if created:
                    st.session_state.auth_email = reg_email
                    st.success("Account created. Now choose your codename.")
                    st.rerun()
                elif not created:
                    st.error("That email is already registered. Log in instead.")

        st.markdown(
            '<div class="login-card" style="margin-top:14px;"><div class="login-hint">'
            '💡 Already have an account? Switch to <b>Log In</b> above.</div></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<div class="login-sect"><h3>🔐 AGENT LOGIN</h3></div>', unsafe_allow_html=True)
        with st.form("email_login_form", clear_on_submit=False):
            login_email = st.text_input("Email", key="login_email")
            login_password = st.text_input("Password", type="password", key="login_password")
            login_submitted = st.form_submit_button("LOG IN")

        if login_submitted:
            login_email = (login_email or "").strip().lower()
            if not validate_email(login_email):
                st.error("Enter a valid email address.")
            else:
                try:
                    account = verify_account(login_email, login_password)
                except Exception:
                    account = None
                    st.error("The database is temporarily unavailable. Check your Postgres connection and try again.")
                if account:
                    st.session_state.auth_email = login_email
                    st.rerun()
                elif not login_password:
                    st.error("Enter your password.")
                else:
                    st.error("Invalid email or password.")

        st.markdown(
            '<div class="login-card" style="margin-top:14px;"><div class="login-hint">'
            '💡 New agent? Switch to <b>Create Account</b> above to register.</div></div>',
            unsafe_allow_html=True,
        )

    st.stop()

if st.session_state.auth_email:
    # Load returning players that already have a codename.
    existing_account = get_account(st.session_state.auth_email) or {}
    if (
        existing_account.get("codename")
        and not st.session_state.uid_loaded
    ):
        load_player(existing_account["uid"], existing_account["codename"])
        start_new_mission()
        st.rerun()

# First-time players choose their codename.
if st.session_state.auth_email and not st.session_state.uid_loaded:
    st.session_state.uid = existing_account.get("uid") or uid_for_email(
        st.session_state.auth_email
    )
    st.markdown("### 🎭 Choose Your Codename")
    st.markdown("Your codename is the callsign shown on the leaderboard.")
    codename = st.text_input("Enter Code Name:")

    if st.button("Confirm Codename"):
        codename = (codename or "").strip()
        if not codename:
            st.error("Codename cannot be empty.")
        else:
            set_codename(st.session_state.auth_email, codename)
            load_player(st.session_state.uid, codename)
            start_new_mission()
            st.rerun()

    st.stop()

# =========================
# HEADER
# =========================
header_info, header_action = st.columns([5, 1])
with header_info:
    st.markdown("## 🕵️ Agency Intel Ops — Decryption Terminal")
    st.markdown(
        f"**Agent:** {st.session_state.player or '—'} | "
        f"**Level:** {st.session_state.level} | "
        f"**Score:** {st.session_state.score}"
    )
with header_action:
    if st.button("🚪 Log Out", use_container_width=True):
        logout()

# =========================
# NAVIGATION
# =========================
page = st.segmented_control(
    "Navigation",
    options=["🎯 Mission", "🏫 Group Arena", "🏆 Leaderboard"],
    default="🎯 Mission",
    label_visibility="collapsed",
    key="nav_page",
)

if page == "🏫 Group Arena":
    render_group_arena()
    st.stop()

if page == "🏆 Leaderboard":
    render_global_leaderboard()
    st.stop()

flash = st.session_state.pop("mission_flash", None)
if flash:
    st.success(flash)

# =========================
# INTERCEPTED TRANSMISSION
# =========================
st.markdown("### 📡 Intercepted Transmission")
st.markdown(
    f"<div class='dark-box'>{st.session_state.current_cipher or '— awaiting signal —'}</div>",
    unsafe_allow_html=True
)

# =========================
# STOPWATCH
# =========================
if st.session_state.mission_started:
    elapsed = int(time.time() - st.session_state.start_time)
    st.markdown(f"⏱ **Elapsed Time:** {elapsed} seconds")

# =========================
# HINTS (TWO-STAGE LOGIC)
# =========================
if st.button("💡 Get Hint") and st.session_state.player:

    if not st.session_state.mission_started:
        st.markdown("### Clue meanings")
        st.markdown("""
        **M:** Mission context  
        **I:** Intercepted detail  
        **P:** Physical proof  
        **B:** Backstory  
        **E:** Evidence
        """)
    else:
        e = st.session_state.current_entry
        st.markdown("### Mission Clues")
        st.markdown(
            f"""
            <div class="dark-box">
            <strong>M:</strong> {e['M']}<br>
            <strong>I:</strong> {e['I']}<br>
            <strong>P:</strong> {e['P']}<br>
            <strong>B:</strong> {e['B']}<br>
            <strong>E:</strong> {e['E']}
            </div>
            """,
            unsafe_allow_html=True
        )

        st.session_state.hint_uses += 1
        increment_hint_usage(st.session_state.uid)

# =========================
# ANSWER INPUT (SAFE)
# =========================
if st.session_state.mission_started:
    guess = st.text_input("Enter decoded phrase (exact match):", key="guess_input")

    if st.button("Transmit Conclusion"):
        correct_answer = st.session_state.current_entry["answer"].lower().strip()
        is_correct = guess.lower().strip() == correct_answer

        st.session_state.history.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "level": st.session_state.level,
            "cipher": st.session_state.current_cipher,
            "answer": guess,
            "correct": is_correct
        })

        if is_correct:
            level = st.session_state.level
            points = 5 + level // 2
            elapsed = (
                int(time.time() - st.session_state.start_time)
                if st.session_state.start_time
                else None
            )
            bonus = speed_bonus(level, elapsed)

            st.session_state.score += points

            update_level(st.session_state.uid, level + 1)
            update_score(st.session_state.uid, st.session_state.score)
            update_leaderboard(st.session_state.player, st.session_state.score)
            try:
                record_group_progress(
                    st.session_state.uid, points + bonus, elapsed or 0
                )
            except Exception:
                logging.exception("Could not update group standings.")

            message = f"✅ Correct! +{points} points."
            if bonus:
                message += (
                    f" ⚡ Speed bonus +{bonus} "
                    f"(solved in {format_duration(elapsed)})."
                )
            st.session_state.mission_flash = message

            st.session_state.level = level + 1
            st.session_state.mission_started = False
            start_new_mission()
            st.rerun()
        else:
            st.session_state.score = max(0, st.session_state.score - 2)
            st.error("❌ Incorrect. Mission compromised.")
            update_score(st.session_state.uid, st.session_state.score)

# =========================
# RESTART MISSION (NO SKIP)
# =========================
if st.button("♻️ Restart Mission"):
    st.session_state.mission_started = False
    start_new_mission()
    st.rerun()

# =========================
# MISSION LOG (SESSION ONLY)
# =========================
st.markdown("## 🗂 Mission Debrief (Session)")

if st.session_state.history:
    for h in reversed(st.session_state.history):
        status = "SUCCESS" if h["correct"] else "FAILED"
        color = "#00c37a" if h["correct"] else "#ff4b4b"
        st.markdown(
            f"""
            <div class="log-box" style="border-left:5px solid {color};">
            Level {h['level']} — {status}<br>
            Cipher: {h['cipher']}<br>
            Answer: {h['answer']}
            </div>
            """,
            unsafe_allow_html=True
        )
else:
    st.info("No completed missions yet.")