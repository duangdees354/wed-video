
import os
import sqlite3
import json
from datetime import datetime
import re
from functools import wraps
from uuid import uuid4
import requests

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_file, abort, Response
)

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, static_folder=STATIC_DIR, template_folder=TEMPLATE_DIR)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


IS_RAILWAY = any(
    os.getenv(name)
    for name in (
        "RAILWAY_PROJECT_ID",
        "RAILWAY_SERVICE_ID",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_PUBLIC_DOMAIN",
    )
)
IS_PRODUCTION = env_flag("IS_PRODUCTION", default=False) or (
    os.getenv("FLASK_ENV", "").strip().lower() == "production"
) or IS_RAILWAY
DATA_DIR = os.path.abspath(
    os.getenv("DATA_DIR")
    or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    or BASE_DIR
)

TURNSTILE_SITE_KEY = os.getenv("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "")
TURNSTILE_ALLOWED_HOSTNAMES = {
    item.strip().lower()
    for item in os.getenv("TURNSTILE_ALLOWED_HOSTNAMES", "").split(",")
    if item.strip()
}
TURNSTILE_REQUIRED = env_flag("TURNSTILE_REQUIRED", default=IS_PRODUCTION)
TURNSTILE_ENABLED = bool(TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY)
VIDEO_STREAM_CHUNK_SIZE = 1024 * 512
VIDEO_PROXY_TIMEOUT = (10, 60)
DEFAULT_VIDEO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    )
}

if bool(TURNSTILE_SITE_KEY) ^ bool(TURNSTILE_SECRET_KEY):
    raise RuntimeError(
        "TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY must be set together."
    )

if TURNSTILE_REQUIRED and not TURNSTILE_ENABLED:
    raise RuntimeError(
        "Turnstile is required in this environment. Set TURNSTILE_SITE_KEY and "
        "TURNSTILE_SECRET_KEY before starting the app."
    )


def get_cover_root() -> str:
    if DATA_DIR == BASE_DIR:
        return os.path.join(STATIC_DIR, "covers")
    return os.path.join(DATA_DIR, "covers")


DB_PATH = os.path.join(DATA_DIR, "videos.db")
VIDEO_ROOT = os.path.join(DATA_DIR, "video_files")
COVER_ROOT = get_cover_root()
EPISODE_COVER_ROOT = os.path.join(COVER_ROOT, "episodes")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(VIDEO_ROOT, exist_ok=True)
os.makedirs(COVER_ROOT, exist_ok=True)
os.makedirs(EPISODE_COVER_ROOT, exist_ok=True)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION
app.config["PREFERRED_URL_SCHEME"] = "https" if IS_PRODUCTION else "http"


def get_client_ip() -> str | None:
    candidates = [
        request.headers.get("CF-Connecting-IP"),
        request.headers.get("X-Forwarded-For"),
        request.remote_addr,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        first_ip = candidate.split(",")[0].strip()
        if first_ip:
            return first_ip
    return None


def normalize_public_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def is_safe_public_path(path: str) -> bool:
    normalized = normalize_public_path(path)
    return bool(normalized) and not normalized.startswith("../") and "/../" not in normalized


def resolve_video_path(file_path: str | None) -> str | None:
    if not file_path:
        return None
    if os.path.isabs(file_path):
        return file_path

    normalized = file_path.replace("/", os.sep)
    candidates = [
        os.path.join(DATA_DIR, normalized),
        os.path.join(BASE_DIR, normalized),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def resolve_cover_path(file_path: str | None) -> str | None:
    if not file_path:
        return None
    if os.path.isabs(file_path):
        return file_path
    if not is_safe_public_path(file_path):
        return None

    normalized = file_path.replace("/", os.sep)
    candidates = []
    if DATA_DIR != BASE_DIR:
        candidates.append(os.path.join(DATA_DIR, normalized))
    candidates.append(os.path.join(STATIC_DIR, normalized))
    candidates.append(os.path.join(BASE_DIR, normalized))

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0] if candidates else None


def video_storage_path(abs_path: str) -> str:
    root = DATA_DIR if DATA_DIR != BASE_DIR else BASE_DIR
    return os.path.relpath(abs_path, root).replace("\\", "/")


def cover_storage_path(abs_path: str) -> str:
    root = DATA_DIR if DATA_DIR != BASE_DIR else STATIC_DIR
    return os.path.relpath(abs_path, root).replace("\\", "/")


def media_url(path: str | None) -> str | None:
    if not path:
        return None
    if str(path).startswith(("http://", "https://")):
        return path
    return url_for("media_file", filename=normalize_public_path(path))


def delete_cover_file(file_path: str | None):
    if not file_path or str(file_path).startswith(("http://", "https://")):
        return

    absolute_path = resolve_cover_path(file_path)
    if not absolute_path:
        return

    try:
        if os.path.exists(absolute_path):
            os.remove(absolute_path)
    except Exception:
        return

    stop_dirs = {os.path.abspath(COVER_ROOT), os.path.abspath(STATIC_DIR)}
    current_dir = os.path.dirname(absolute_path)
    while os.path.isdir(current_dir) and os.path.abspath(current_dir) not in stop_dirs:
        try:
            if os.listdir(current_dir):
                break
            os.rmdir(current_dir)
        except Exception:
            break
        current_dir = os.path.dirname(current_dir)


def verify_turnstile(token, remote_ip=None):
    """Validate a Turnstile token with Cloudflare Siteverify."""
    if not TURNSTILE_ENABLED:
        return not TURNSTILE_REQUIRED

    if not token:
        return False

    try:
        resp = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret": TURNSTILE_SECRET_KEY,
                "response": token,
                "remoteip": remote_ip or "",
                "idempotency_key": str(uuid4()),
            },
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            app.logger.warning(
                "Turnstile validation failed: %s",
                ", ".join(data.get("error-codes", [])) or "unknown-error",
            )
            return False

        hostname = str(data.get("hostname") or "").strip().lower()
        if TURNSTILE_ALLOWED_HOSTNAMES and hostname not in TURNSTILE_ALLOWED_HOSTNAMES:
            app.logger.warning("Turnstile hostname mismatch: %s", hostname or "missing")
            return False

        return True
    except Exception:
        return False


def parse_range_header(range_header: str | None, total_size: int | None = None):
    """Parse a simple single-range header and return (start, end)."""
    if not range_header:
        return None

    match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip(), re.IGNORECASE)
    if not match:
        return "invalid"

    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return "invalid"

    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else None
        else:
            if total_size is None:
                return None
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return "invalid"
            start = max(total_size - suffix_length, 0)
            end = total_size - 1
    except ValueError:
        return "invalid"

    if start < 0:
        return "invalid"

    if total_size is not None:
        if start >= total_size:
            return "invalid"
        if end is None or end >= total_size:
            end = total_size - 1

    if end is not None and end < start:
        return "invalid"

    return start, end


def iter_remote_video_chunks(upstream_response, start=0, end=None):
    """Yield video bytes while optionally trimming to the requested byte range."""
    position = 0
    remaining = None if end is None else (end - start + 1)

    try:
        for chunk in upstream_response.iter_content(chunk_size=VIDEO_STREAM_CHUNK_SIZE):
            if not chunk:
                continue

            chunk_start = position
            chunk_end = position + len(chunk) - 1
            position = chunk_end + 1

            if chunk_end < start:
                continue

            if chunk_start < start:
                chunk = chunk[start - chunk_start :]
                chunk_start = start

            if remaining is not None and len(chunk) > remaining:
                chunk = chunk[:remaining]

            if chunk:
                yield chunk

            if remaining is not None:
                remaining -= len(chunk)
                if remaining <= 0:
                    break
    finally:
        upstream_response.close()


def stream_remote_video(video_url: str):
    """Proxy remote MP4 URLs through this app so every client gets consistent headers."""
    range_header = request.headers.get("Range")
    upstream_headers = dict(DEFAULT_VIDEO_HEADERS)
    if range_header:
        upstream_headers["Range"] = range_header

    try:
        upstream = requests.get(
            video_url,
            headers=upstream_headers,
            stream=True,
            allow_redirects=True,
            timeout=VIDEO_PROXY_TIMEOUT,
        )
    except requests.RequestException:
        abort(502)

    total_size = None
    content_length = upstream.headers.get("Content-Length")
    if content_length:
        try:
            total_size = int(content_length)
        except (TypeError, ValueError):
            total_size = None

    passthrough_headers = {"Accept-Ranges": upstream.headers.get("Accept-Ranges", "bytes")}
    for header_name in ("Cache-Control", "Content-Range", "Content-Type", "ETag", "Last-Modified"):
        value = upstream.headers.get(header_name)
        if value:
            passthrough_headers[header_name] = value

    if "Content-Type" not in passthrough_headers:
        passthrough_headers["Content-Type"] = "video/mp4"

    if upstream.status_code == 416:
        if total_size is not None and "Content-Range" not in passthrough_headers:
            passthrough_headers["Content-Range"] = f"bytes */{total_size}"
        upstream.close()
        return Response(status=416, headers=passthrough_headers)

    # Preferred case: the upstream already honors range requests.
    if upstream.status_code in (200, 206) and (not range_header or upstream.status_code == 206):
        if total_size is not None:
            passthrough_headers["Content-Length"] = str(total_size)
        return Response(
            iter_remote_video_chunks(upstream),
            status=upstream.status_code,
            headers=passthrough_headers,
            direct_passthrough=True,
        )

    # Fallback for hosts that ignore Range but still send the whole file.
    if upstream.status_code == 200 and range_header and total_size is not None:
        parsed_range = parse_range_header(range_header, total_size)
        if parsed_range == "invalid":
            upstream.close()
            return Response(
                status=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{total_size}",
                    "Content-Type": passthrough_headers["Content-Type"],
                },
            )

        if parsed_range:
            start, end = parsed_range
            end = total_size - 1 if end is None else end
            length = end - start + 1
            passthrough_headers["Accept-Ranges"] = "bytes"
            passthrough_headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
            passthrough_headers["Content-Length"] = str(length)
            return Response(
                iter_remote_video_chunks(upstream, start=start, end=end),
                status=206,
                headers=passthrough_headers,
                direct_passthrough=True,
            )

    if upstream.status_code >= 400:
        upstream.close()
        abort(502)

    if total_size is not None:
        passthrough_headers["Content-Length"] = str(total_size)

    return Response(
        iter_remote_video_chunks(upstream),
        status=upstream.status_code,
        headers=passthrough_headers,
        direct_passthrough=True,
    )


@app.route("/media/<path:filename>")
def media_file(filename):
    if not is_safe_public_path(filename):
        abort(404)

    absolute_path = resolve_cover_path(filename)
    if not absolute_path or not os.path.exists(absolute_path):
        abort(404)

    return send_file(absolute_path, conditional=True, max_age=3600)


@app.context_processor
def inject_globals():
    return {
        "TURNSTILE_SITE_KEY": TURNSTILE_SITE_KEY,
        "TURNSTILE_ENABLED": TURNSTILE_ENABLED,
        "media_url": media_url,
    }



# Thai datetime filter
from datetime import datetime, timedelta

def thdt(value):
    try:
        dt=datetime.fromisoformat(value)
    except Exception:
        return value
    dt=dt+timedelta(hours=7)
    return dt.strftime("%Y-%m-%d : %H:%M")

app.jinja_env.filters['thdt']=thdt


# ---------- Admin login defaults (reset every restart) ----------
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "1234"

# ค่า login ปัจจุบันในหน่วยความจำ (รีเซ็ตเมื่อรีสตาร์ท)
current_admin_username = DEFAULT_ADMIN_USERNAME
current_admin_password = DEFAULT_ADMIN_PASSWORD


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def ensure_episode_thumbnail_column(conn: sqlite3.Connection):
    """เพิ่มคอลัมน์ thumbnail_url ให้ตาราง episodes ถ้ายังไม่มี (ใช้ตอนอัปเดตจากเวอร์ชันเก่า)."""
    cur = conn.execute("PRAGMA table_info(episodes)")
    cols = [row[1] for row in cur.fetchall()]
    if "thumbnail_url" not in cols:
        conn.execute("ALTER TABLE episodes ADD COLUMN thumbnail_url TEXT")
        conn.commit()




def ensure_visibility_columns(conn: sqlite3.Connection):
    """เพิ่มคอลัมน์ is_active ให้ตาราง series และ episodes ถ้ายังไม่มี (ใช้เปิด/ปิดการดู)."""
    # ตาราง series
    cur = conn.execute("PRAGMA table_info(series)")
    cols = [row[1] for row in cur.fetchall()]
    if "is_active" not in cols:
        conn.execute("ALTER TABLE series ADD COLUMN is_active INTEGER DEFAULT 1")
    # ตาราง episodes
    cur = conn.execute("PRAGMA table_info(episodes)")
    cols = [row[1] for row in cur.fetchall()]
    if "is_active" not in cols:
        conn.execute("ALTER TABLE episodes ADD COLUMN is_active INTEGER DEFAULT 1")
    conn.commit()



def generate_user_key() -> str:
    """สร้าง key สำหรับผู้ใช้ ใช้ตัวอักษร hex แบบง่าย ๆ"""
    return "U" + os.urandom(8).hex().upper()


def ensure_user_extra_columns(conn: sqlite3.Connection):
    """เพิ่มคอลัมน์ user_key และ plain_password ให้ตาราง users ถ้ายังไม่มี
    และเติมค่า user_key อัตโนมัติถ้ายังเป็นค่าว่าง"""
    cur = conn.execute("PRAGMA table_info(users)")
    cols = [row[1] for row in cur.fetchall()]

    if "user_key" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN user_key TEXT")
    if "plain_password" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN plain_password TEXT")
    conn.commit()

    # เติม key ให้ผู้ใช้ที่ยังไม่มี
    cur = conn.execute("SELECT id, user_key FROM users")
    rows = cur.fetchall()
    for r in rows:
        try:
            current_key = r["user_key"]
        except Exception:
            current_key = r[1]
        if not current_key:
            new_key = generate_user_key()
            conn.execute("UPDATE users SET user_key = ? WHERE id = ?", (new_key, r[0]))
    conn.commit()


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    # ตารางเรื่อง
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            thumbnail_url TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    # ตารางตอน (เวอร์ชันใหม่มี thumbnail_url)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            episode_number INTEGER,
            source_type TEXT NOT NULL,
            video_url TEXT,
            drive_id TEXT,
            file_path TEXT,
            thumbnail_url TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(series_id) REFERENCES series(id) ON DELETE CASCADE
        )
        """
    )

    # กรณีอัปเกรดจากเวอร์ชันเก่าที่ไม่มีคอลัมน์ thumbnail_url
    ensure_episode_thumbnail_column(conn)
    ensure_visibility_columns(conn)

    # ตารางผู้ใช้ทั่วไป
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    # ตารางประวัติการดู
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS watch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            series_id INTEGER NOT NULL,
            episode_id INTEGER NOT NULL,
            watched_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(series_id) REFERENCES series(id) ON DELETE CASCADE,
            FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
        )
        """
    )

    ensure_user_extra_columns(conn)

    conn.commit()
    conn.close()


init_db()


def extract_drive_id(text: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None

    if "drive.google.com" not in text:
        return text

    if "/file/d/" in text:
        try:
            part = text.split("/file/d/")[1]
            file_id = part.split("/")[0]
            return file_id
        except Exception:
            pass

    if "id=" in text:
        try:
            part = text.split("id=")[1]
            file_id = part.split("&")[0]
            return file_id
        except Exception:
            pass

    return None


def download_drive_file(file_id: str, series_id: int) -> str:
    import gdown

    series_dir = os.path.join(VIDEO_ROOT, f"series_{series_id}")
    os.makedirs(series_dir, exist_ok=True)

    output = os.path.join(series_dir, f"{file_id}.mp4")

    if os.path.exists(output):
        return output

    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    try:
        gdown.download(url, output, quiet=False)
    except Exception as e:
        raise RuntimeError(f"โหลดไฟล์จาก Google Drive ไม่สำเร็จ: {e}")

    if not os.path.exists(output):
        raise RuntimeError("ไม่พบไฟล์ที่ดาวน์โหลดจาก Google Drive")

    return output


def is_admin() -> bool:
    return bool(session.get("is_admin"))


def admin_required():
    if not is_admin():
        flash("ต้องเข้าสู่ระบบแอดมินก่อน", "error")
        return False
    return True



# ฟังก์ชันจัดการสถานะผู้ใช้ทั่วไป
def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user


def login_user(user_row):
    session["user_id"] = user_row["id"]
    session["username"] = user_row["username"]


def logout_user():
    session.pop("user_id", None)
    session.pop("username", None)


def user_login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get("user_id"):
            # ถ้ายังไม่ได้ล็อกอิน ให้ไปหน้าเข้าสู่ระบบผู้ใช้
            flash("กรุณาเข้าสู่ระบบก่อนดูวิดีโอ", "error")
            return redirect(url_for("user_login", next=request.path))
        return view_func(*args, **kwargs)

    return wrapped_view

@app.route("/")
def index():
    conn = get_db_connection()
    series_list = conn.execute(
        "SELECT * FROM series ORDER BY datetime(created_at) DESC"
    ).fetchall()
    conn.close()
    return render_template("index.html", series_list=series_list)




@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return redirect(url_for("index"))

    # ตัดคำอย่างง่าย: เอาคำหลัก เช่น "มหาเวทย์ผนึกมาร" จาก "มหาเวทย์ผนึกมาร S2"
    tokens = query.split()
    keywords = [t for t in tokens if not re.fullmatch(r"[sS]\d+", t)]
    main_keyword = max(keywords, key=len) if keywords else query

    conn = get_db_connection()
    series_rows = conn.execute("SELECT * FROM series").fetchall()
    conn.close()

    def score(row):
        title = (row["title"] or "").lower()
        desc = (row["description"] or "").lower()
        q = query.lower()
        mk = main_keyword.lower()

        if title == q:
            base = 4
        elif q in title:
            base = 3
        elif mk in title:
            base = 2
        elif mk in desc:
            base = 1
        else:
            base = 0

        return base

    sorted_rows = sorted(series_rows, key=score, reverse=True)
    results = sorted_rows  # แสดงทุกเรื่อง แต่จัดอันดับให้เรื่องที่ตรงสุดอยู่ด้านบน

    return render_template(
        "search_results.html",
        query=query,
        main_keyword=main_keyword,
        series_list=results,
    )

@app.route("/series/<int:series_id>")
def series_detail(series_id):
    conn = get_db_connection()
    series = conn.execute(
        "SELECT * FROM series WHERE id = ?", (series_id,)
    ).fetchone()
    if series is None:
        conn.close()
        flash("ไม่พบเรื่องนี้", "error")
        return redirect(url_for("index"))

    episodes = conn.execute(
        """
        SELECT * FROM episodes
        WHERE series_id = ?
        ORDER BY episode_number IS NULL, episode_number, datetime(created_at)
        """,
        (series_id,),
    ).fetchall()
    conn.close()
    return render_template("series_detail.html", series=series, episodes=episodes)


@app.route("/series/<int:series_id>/episode/<int:episode_id>")
@user_login_required
def watch_episode(series_id, episode_id):
    conn = get_db_connection()
    series = conn.execute(
        "SELECT * FROM series WHERE id = ?", (series_id,)
    ).fetchone()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id = ? AND series_id = ?",
        (episode_id, series_id),
    ).fetchone()
    conn.close()

    if series is None or episode is None:
        flash("ไม่พบตอนนี้", "error")
        return redirect(url_for("index"))

    # ตรวจสอบสถานะเปิด/ปิด
    series_active = 1
    try:
        if "is_active" in series.keys() and series["is_active"] is not None:
            series_active = int(series["is_active"])
    except Exception:
        series_active = 1

    episode_active = 1
    try:
        if "is_active" in episode.keys() and episode["is_active"] is not None:
            episode_active = int(episode["is_active"])
    except Exception:
        episode_active = 1

    blocked = (series_active == 0) or (episode_active == 0)

    # บันทึกประวัติการดู (เฉพาะเมื่อผู้ใช้ล็อกอินแล้ว)
    user_id = session.get("user_id")
    if user_id and not blocked:
        try:
            conn = get_db_connection()
            conn.execute(
                "INSERT INTO watch_history (user_id, series_id, episode_id, watched_at) VALUES (?, ?, ?, ?)",
                (user_id, series_id, episode_id, datetime.utcnow().isoformat()),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    # ผู้ใช้ยังเข้าได้ปกติ แต่ถ้า blocked == True จะขึ้นข้อความในหน้า watch.html แทนวิดีโอ
    return render_template("watch.html", series=series, episode=episode, blocked=blocked)


@app.route("/stream/<int:episode_id>")
@user_login_required
def stream_episode(episode_id):
    conn = get_db_connection()
    episode = conn.execute(
        "SELECT * FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()

    series = None
    if episode is not None:
        series = conn.execute(
            "SELECT * FROM series WHERE id = ?",
            (episode["series_id"],),
        ).fetchone()

    conn.close()

    if episode is None or series is None:
        abort(404)

    # ถ้าเรื่องหรืออตอนถูกปิด จะไม่ให้สตรีมวิดีโอ
    series_active = 1
    try:
        if "is_active" in series.keys() and series["is_active"] is not None:
            series_active = int(series["is_active"])
    except Exception:
        series_active = 1

    episode_active = 1
    try:
        if "is_active" in episode.keys() and episode["is_active"] is not None:
            episode_active = int(episode["is_active"])
    except Exception:
        episode_active = 1

    if series_active == 0 or episode_active == 0:
        abort(403)

    source_type = None
    drive_id = None
    file_path = None
    video_url = None
    try:
        if "source_type" in episode.keys():
            source_type = episode["source_type"]
        if "drive_id" in episode.keys():
            drive_id = episode["drive_id"]
        if "file_path" in episode.keys():
            file_path = episode["file_path"]
        if "video_url" in episode.keys():
            video_url = episode["video_url"]
    except Exception:
        pass

    # ---------------------------
    # เตรียม path ของไฟล์วิดีโอ
    # ถ้าไฟล์หายไป (เช่น ย้ายเซิร์ฟเวอร์/รีดีพลอยใหม่)
    # และเป็นตอนแบบ Google Drive ให้ลองโหลดใหม่อัตโนมัติ
    # ---------------------------
    abs_path = resolve_video_path(file_path)

    if abs_path and os.path.exists(abs_path):
        response = send_file(
            abs_path,
            mimetype="video/mp4",
            as_attachment=False,
            conditional=True,
            etag=True,
            last_modified=os.path.getmtime(abs_path),
            max_age=3600,
        )
        response.headers["Accept-Ranges"] = "bytes"
        return response

    if source_type == "gdrive" and drive_id:
        try:
            new_file = download_drive_file(drive_id, episode["series_id"])
            rel_path = video_storage_path(new_file)
            conn2 = get_db_connection()
            conn2.execute(
                "UPDATE episodes SET file_path = ? WHERE id = ?",
                (rel_path, episode["id"]),
            )
            conn2.commit()
            conn2.close()

            response = send_file(
                new_file,
                mimetype="video/mp4",
                as_attachment=False,
                conditional=True,
                etag=True,
                last_modified=os.path.getmtime(new_file),
                max_age=3600,
            )
            response.headers["Accept-Ranges"] = "bytes"
            return response
        except Exception:
            abort(404)

    if video_url:
        return stream_remote_video(video_url)

    abort(404)




# ------------- ระบบผู้ใช้ทั่วไป: สมัคร, ล็อกอิน, เปลี่ยนรหัส, ประวัติการดู -------------


@app.route("/register", methods=["GET", "POST"])
def user_register():
    if session.get("user_id"):
        return redirect(url_for("my_page"))

    if request.method == "POST":
        token = request.form.get("cf-turnstile-response")
        if not verify_turnstile(token, get_client_ip()):
            flash("กรุณายืนยันว่าท่านไม่ใช่บอทก่อนสมัครสมาชิก", "error")
            return render_template("user_register.html")

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")

        if not username or not password or not password_confirm:
            flash("กรุณากรอกข้อมูลให้ครบ", "error")
        elif password != password_confirm:
            flash("รหัสผ่านใหม่และยืนยันรหัสผ่านไม่ตรงกัน", "error")
        else:
            conn = get_db_connection()
            try:
                hashed = generate_password_hash(password)
                user_key = generate_user_key()
                conn.execute(
                    "INSERT INTO users (username, password, plain_password, user_key, created_at) VALUES (?, ?, ?, ?, ?)",
                    (username, hashed, password, user_key, datetime.utcnow().isoformat()),
                )
                conn.commit()
                conn.close()
                flash("สมัครบัญชีสำเร็จ กรุณาเข้าสู่ระบบ", "success")
                return redirect(url_for("user_login"))
            except sqlite3.IntegrityError:
                conn.close()
                flash("ชื่อผู้ใช้นี้มีอยู่ในระบบแล้ว", "error")

    return render_template("user_register.html")



@app.route("/login", methods=["GET", "POST"])
def user_login():
    if session.get("user_id"):
        return redirect(url_for("my_page"))

    next_url = request.args.get("next") or url_for("index")

    if request.method == "POST":
        token = request.form.get("cf-turnstile-response")
        if not verify_turnstile(token, get_client_ip()):
            flash("กรุณายืนยันว่าท่านไม่ใช่บอทก่อนเข้าสู่ระบบ", "error")
            return render_template("user_login.html")

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()

        if user is None:
            flash("ไม่พบบัญชีผู้ใช้นี้", "error")
        elif not check_password_hash(user["password"], password):
            flash("รหัสผ่านไม่ถูกต้อง", "error")
        else:
            login_user(user)
            flash("เข้าสู่ระบบสำเร็จ", "success")
            return redirect(next_url)

    return render_template("user_login.html")


@app.route("/logout")
def user_logout():
    logout_user()
    flash("ออกจากระบบผู้ใช้แล้ว", "info")
    return redirect(url_for("index"))


@app.route("/account", methods=["GET", "POST"])
@user_login_required
def user_account():
    user = get_current_user()
    if user is None:
        return redirect(url_for("user_login"))

    if request.method == "POST":
        action = request.form.get("action", "change_password")

        if action == "change_password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not current_password or not new_password or not confirm_password:
                flash("กรุณากรอกข้อมูลให้ครบ", "error")
            elif not check_password_hash(user["password"], current_password):
                flash("รหัสผ่านเดิมไม่ถูกต้อง", "error")
            elif new_password != confirm_password:
                flash("รหัสผ่านใหม่และยืนยันรหัสผ่านไม่ตรงกัน", "error")
            else:
                conn = get_db_connection()
                conn.execute(
                    "UPDATE users SET password = ?, plain_password = ? WHERE id = ?",
                    (generate_password_hash(new_password), new_password, user["id"]),
                )
                conn.commit()
                conn.close()
                flash("เปลี่ยนรหัสผ่านสำเร็จ", "success")
                return redirect(url_for("user_account"))

        elif action == "reset_key":
            conn = get_db_connection()
            new_key = generate_user_key()
            conn.execute(
                "UPDATE users SET user_key = ? WHERE id = ?",
                (new_key, user["id"]),
            )
            conn.commit()
            conn.close()
            flash("สร้าง key ใหม่เรียบร้อยแล้ว", "success")
            return redirect(url_for("user_account"))

    # โหลดข้อมูล user ล่าสุด (หลังเปลี่ยนรหัสผ่านหรือ key)
    user = get_current_user()
    return render_template("user_account.html", user=user)


@app.route("/me")
@user_login_required
def my_page():
    user = get_current_user()
    if user is None:
        return redirect(url_for("user_login"))

    conn = get_db_connection()
    history = conn.execute(
        """
        SELECT wh.*, s.title AS series_title, e.title AS episode_title, e.episode_number
        FROM watch_history wh
        JOIN series s ON s.id = wh.series_id
        JOIN episodes e ON e.id = wh.episode_id
        WHERE wh.user_id = ?
        ORDER BY datetime(wh.watched_at) DESC
        LIMIT 50
        """,
        (user["id"],),
    ).fetchall()
    conn.close()

    return render_template("my_page.html", user=user, history=history)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    global current_admin_username, current_admin_password

    if request.method == "POST":
        token = request.form.get("cf-turnstile-response")
        if not verify_turnstile(token, get_client_ip()):
            flash("กรุณายืนยันว่าท่านไม่ใช่บอทก่อนเข้าสู่ระบบแอดมิน", "error")
            return render_template("login.html")

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username == current_admin_username and password == current_admin_password:
            session["is_admin"] = True
            session["admin_username"] = username
            flash("เข้าสู่ระบบแอดมินสำเร็จ", "success")
            return redirect(url_for("admin_series"))
        else:
            flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "error")

    return render_template("login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    session.pop("admin_username", None)
    flash("ออกจากระบบแล้ว", "info")
    return redirect(url_for("index"))


@app.route("/admin/account", methods=["GET", "POST"])
def admin_account():
    global current_admin_username, current_admin_password

    if not admin_required():
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        new_username = request.form.get("new_username", "").strip()
        new_password = request.form.get("new_password", "").strip()

        if not new_username or not new_password:
            flash("กรุณากรอกทั้งชื่อผู้ใช้ใหม่และรหัสผ่านใหม่", "error")
        else:
            current_admin_username = new_username
            current_admin_password = new_password
            flash("เปลี่ยนชื่อผู้ใช้และรหัสผ่านแอดมินสำเร็จ (มีผลจนกว่าจะรีสตาร์ท)", "success")
            return redirect(url_for("admin_account"))

    return render_template(
        "admin_account.html",
        current_username=current_admin_username,
        default_username=DEFAULT_ADMIN_USERNAME,
        default_password=DEFAULT_ADMIN_PASSWORD,
    )


@app.route("/admin/users")
def admin_users():
    if not admin_required():
        return redirect(url_for("admin_login"))

    q = request.args.get("q", "").strip()
    conn = get_db_connection()
    if q:
        like = f"%{q}%"
        users = conn.execute(
            "SELECT * FROM users WHERE username LIKE ? OR user_key LIKE ? ORDER BY datetime(created_at) DESC",
            (like, like),
        ).fetchall()
    else:
        users = conn.execute(
            "SELECT * FROM users ORDER BY datetime(created_at) DESC LIMIT 100"
        ).fetchall()
    conn.close()
    return render_template("admin_users.html", users=users, q=q)


@app.route("/admin/users/<int:user_id>", methods=["GET", "POST"])
def admin_user_detail(user_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        conn.close()
        flash("ไม่พบบัญชีผู้ใช้นี้", "error")
        return redirect(url_for("admin_users"))

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "update_account":
            new_username = request.form.get("username", "").strip()
            new_password = request.form.get("password", "").strip()

            if not new_username:
                flash("กรุณากรอกชื่อผู้ใช้", "error")
            else:
                try:
                    if new_password:
                        hashed = generate_password_hash(new_password)
                        conn.execute(
                            "UPDATE users SET username = ?, password = ?, plain_password = ? WHERE id = ?",
                            (new_username, hashed, new_password, user_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE users SET username = ? WHERE id = ?",
                            (new_username, user_id),
                        )
                    conn.commit()
                    flash("อัปเดตบัญชีผู้ใช้เรียบร้อยแล้ว", "success")
                except sqlite3.IntegrityError:
                    flash("ชื่อผู้ใช้นี้มีอยู่ในระบบแล้ว", "error")

        elif action == "reset_key":
            new_key = generate_user_key()
            conn.execute(
                "UPDATE users SET user_key = ? WHERE id = ?",
                (new_key, user_id),
            )
            conn.commit()
            flash("รีเซ็ต key ของผู้ใช้นี้เรียบร้อยแล้ว", "success")

        elif action == "delete_user":
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            conn.close()
            flash("ลบบัญชีผู้ใช้เรียบร้อยแล้ว", "success")
            return redirect(url_for("admin_users"))

        elif action == "clear_history_all":
            # ลบประวัติการดูทั้งหมดของผู้ใช้นี้
            conn.execute("DELETE FROM watch_history WHERE user_id = ?", (user_id,))
            conn.commit()
            flash("ลบประวัติการดูทั้งหมดของผู้ใช้นี้เรียบร้อยแล้ว", "success")

        elif action == "delete_history_item":
            # ลบประวัติการดูทีละตอน
            history_id = request.form.get("history_id")
            if history_id:
                conn.execute(
                    "DELETE FROM watch_history WHERE id = ? AND user_id = ?",
                    (history_id, user_id),
                )
                conn.commit()
                flash("ลบประวัติการดูตอนนี้เรียบร้อยแล้ว", "success")

        elif action == "delete_history_series":
            # ลบประวัติการดูรายเรื่อง (ทุกตอนของเรื่องนี้ที่ผู้ใช้นี้เคยดู)
            series_id = request.form.get("series_id")
            if series_id:
                conn.execute(
                    "DELETE FROM watch_history WHERE user_id = ? AND series_id = ?",
                    (user_id, series_id),
                )
                conn.commit()
                flash("ลบประวัติการดูทั้งหมดของเรื่องนี้เรียบร้อยแล้ว", "success")

        # โหลดข้อมูล user ใหม่ล่าสุดหลังอัปเดต
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    history = conn.execute(
        """
        SELECT wh.*, s.title AS series_title, e.title AS episode_title, e.episode_number
        FROM watch_history wh
        JOIN series s ON s.id = wh.series_id
        JOIN episodes e ON e.id = wh.episode_id
        WHERE wh.user_id = ?
        ORDER BY datetime(wh.watched_at) DESC
        """
        ,
        (user_id,),
    ).fetchall()
    conn.close()

    return render_template("admin_user_detail.html", user=user, history=history)
@app.route("/admin/series", methods=["GET", "POST"])
def admin_series():
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        thumbnail_url_input = request.form.get("thumbnail_url", "").strip()
        cover_file = request.files.get("cover_file")

        if not title:
            flash("กรุณากรอกชื่อเรื่อง", "error")
        else:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO series (title, description, thumbnail_url, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (title, description, None, datetime.utcnow().isoformat()),
            )
            series_id = cur.lastrowid
            conn.commit()

            thumbnail_value = None

            if cover_file and cover_file.filename:
                filename = os.path.basename(cover_file.filename)
                base, ext = os.path.splitext(filename)
                ext = ext.lower() or ".jpg"

                series_cover_dir = os.path.join(COVER_ROOT, f"series_{series_id}")
                os.makedirs(series_cover_dir, exist_ok=True)

                safe_name = f"cover_{series_id}_{int(datetime.utcnow().timestamp())}{ext}"
                save_path = os.path.join(series_cover_dir, safe_name)
                cover_file.save(save_path)

                thumbnail_value = cover_storage_path(save_path)

            elif thumbnail_url_input:
                thumbnail_value = thumbnail_url_input

            if thumbnail_value is not None:
                conn.execute(
                    "UPDATE series SET thumbnail_url = ? WHERE id = ?",
                    (thumbnail_value, series_id),
                )
                conn.commit()

            flash("เพิ่มเรื่องใหม่สำเร็จแล้ว", "success")

    # รองรับการค้นหาเรื่องในหน้าแอดมินด้วยพารามิเตอร์ q (GET)
    search_q = request.args.get("q", "").strip()
    if search_q:
        like = f"%{search_q}%"
        series_list = conn.execute(
            """
            SELECT * FROM series
            WHERE title LIKE ? OR description LIKE ?
            ORDER BY datetime(created_at) DESC
            """,
            (like, like),
        ).fetchall()
    else:
        series_list = conn.execute(
            "SELECT * FROM series ORDER BY datetime(created_at) DESC"
        ).fetchall()

    conn.close()
    return render_template("admin_series.html", series_list=series_list, query=search_q)




@app.route("/admin/series/<int:series_id>/toggle_visibility", methods=["POST"])
def admin_toggle_series(series_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM series WHERE id = ?", (series_id,)
    ).fetchone()

    if row is None:
        conn.close()
        flash("ไม่พบเรื่องนี้", "error")
        return redirect(url_for("admin_series"))

    current = 1
    try:
        if "is_active" in row.keys() and row["is_active"] is not None:
            current = int(row["is_active"])
    except Exception:
        current = 1

    new_val = 0 if current == 1 else 1
    conn.execute(
        "UPDATE series SET is_active = ? WHERE id = ?",
        (new_val, series_id),
    )
    conn.commit()
    conn.close()

    flash("อัปเดตสถานะการเปิด/ปิดเรื่องเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_series"))

@app.route("/admin/series/<int:series_id>/edit", methods=["GET", "POST"])
def admin_edit_series(series_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    series = conn.execute(
        "SELECT * FROM series WHERE id = ?", (series_id,)
    ).fetchone()

    if series is None:
        conn.close()
        flash("ไม่พบเรื่องนี้", "error")
        return redirect(url_for("admin_series"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        thumbnail_url_input = request.form.get("thumbnail_url", "").strip()
        cover_file = request.files.get("cover_file")

        if not title:
            flash("กรุณากรอกชื่อเรื่อง", "error")
            return redirect(url_for("admin_edit_series", series_id=series_id))

        thumbnail_value = series["thumbnail_url"]

        # ถ้าอัปโหลดรูปใหม่ ให้ลบรูปเก่าที่เป็นไฟล์ใน static ออกก่อน
        if cover_file and cover_file.filename:
            if thumbnail_value and not str(thumbnail_value).startswith("http"):
                delete_cover_file(thumbnail_value)

            filename = os.path.basename(cover_file.filename)
            base, ext = os.path.splitext(filename)
            ext = ext.lower() or ".jpg"

            series_cover_dir = os.path.join(COVER_ROOT, f"series_{series_id}")
            os.makedirs(series_cover_dir, exist_ok=True)

            safe_name = f"cover_{series_id}_{int(datetime.utcnow().timestamp())}{ext}"
            save_path = os.path.join(series_cover_dir, safe_name)
            cover_file.save(save_path)

            thumbnail_value = cover_storage_path(save_path)

        # ถ้าไม่อัปโหลดไฟล์ แต่ใส่ลิงก์ใหม่ ให้ใช้ลิงก์นั้นแทน
        elif thumbnail_url_input:
            thumbnail_value = thumbnail_url_input

        conn.execute(
            """
            UPDATE series
            SET title = ?, description = ?, thumbnail_url = ?
            WHERE id = ?
            """,
            (title, description, thumbnail_value, series_id),
        )
        conn.commit()

        flash("อัปเดตข้อมูลเรื่องเรียบร้อยแล้ว", "success")
        return redirect(url_for("admin_series"))

    conn.close()
    return render_template("admin_edit_series.html", series=series)


@app.route("/admin/series/<int:series_id>/delete", methods=["POST"])
def admin_delete_series(series_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    episodes = conn.execute(
        "SELECT file_path FROM episodes WHERE series_id = ?", (series_id,)
    ).fetchall()

    for ep in episodes:
        fp = ep["file_path"]
        if fp:
            if not os.path.isabs(fp):
                fp_full = os.path.join(BASE_DIR, fp)
            else:
                fp_full = fp
            try:
                if os.path.exists(fp_full):
                    os.remove(fp_full)
            except Exception:
                pass

    conn.execute("DELETE FROM series WHERE id = ?", (series_id,))
    conn.commit()
    conn.close()

    series_dir = os.path.join(VIDEO_ROOT, f"series_{series_id}")
    if os.path.isdir(series_dir):
        try:
            import shutil
            shutil.rmtree(series_dir)
        except Exception:
            pass

    cover_dir = os.path.join(COVER_ROOT, f"series_{series_id}")
    if os.path.isdir(cover_dir):
        try:
            import shutil
            shutil.rmtree(cover_dir)
        except Exception:
            pass

    flash("ลบเรื่องและตอนทั้งหมดเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_series"))


@app.route("/admin/series/<int:series_id>/episodes", methods=["GET", "POST"])
def admin_episodes(series_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    series = conn.execute(
        "SELECT * FROM series WHERE id = ?", (series_id,)
    ).fetchone()
    if series is None:
        conn.close()
        flash("ไม่พบเรื่องนี้", "error")
        return redirect(url_for("admin_series"))

    if request.method == "POST":
        mode = request.form.get("mode", "direct")
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        episode_number_raw = request.form.get("episode_number", "").strip()
        thumbnail_url_input = request.form.get("thumbnail_url", "").strip()
        cover_file = request.files.get("cover_file")

        episode_number = int(episode_number_raw) if episode_number_raw.isdigit() else None

        if not title:
            flash("กรุณากรอกชื่อตอน", "error")
            return redirect(url_for("admin_episodes", series_id=series_id))

        source_type = None
        video_url = None
        drive_id = None
        file_path = None

        if mode == "direct":
            video_url = request.form.get("video_url", "").strip()
            if not video_url:
                flash("กรุณากรอกลิงก์วิดีโอแบบ mp4", "error")
                return redirect(url_for("admin_episodes", series_id=series_id))
            source_type = "direct"

        elif mode == "gdrive":
            drive_text = request.form.get("drive_link", "").strip()
            drive_id = extract_drive_id(drive_text)
            if not drive_id:
                flash("ไม่สามารถดึง Drive ID จากลิงก์ได้ กรุณาตรวจสอบอีกครั้ง", "error")
                return redirect(url_for("admin_episodes", series_id=series_id))

            try:
                file_real = download_drive_file(drive_id, series_id)
            except Exception as e:
                flash(str(e), "error")
                return redirect(url_for("admin_episodes", series_id=series_id))

                rel_path = video_storage_path(file_real)
            file_path = rel_path
            source_type = "gdrive"

        elif mode == "upload":
            file = request.files.get("file")
            if not file or file.filename == "":
                flash("กรุณาเลือกไฟล์วิดีโอสำหรับอัปโหลด", "error")
                return redirect(url_for("admin_episodes", series_id=series_id))

            filename = os.path.basename(file.filename)
            base, ext = os.path.splitext(filename)
            ext = ext.lower() or ".mp4"

            series_dir = os.path.join(VIDEO_ROOT, f"series_{series_id}")
            os.makedirs(series_dir, exist_ok=True)

            safe_name = f"{base}_{int(datetime.utcnow().timestamp())}{ext}"
            save_path = os.path.join(series_dir, safe_name)
            file.save(save_path)

            rel_path = video_storage_path(save_path)
            file_path = rel_path
            source_type = "upload"

        else:
            flash("โหมดที่เลือกไม่ถูกต้อง", "error")
            return redirect(url_for("admin_episodes", series_id=series_id))

        # ขั้นแรก เพิ่มตอนโดยยังไม่รู้ path ปก (thumbnail_url)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO episodes (
                id, series_id, title, description, episode_number,
                source_type, video_url, drive_id, file_path,
                thumbnail_url, created_at
            )
            VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                series_id,
                title,
                description,
                episode_number,
                source_type,
                video_url,
                drive_id,
                file_path,
                None,
                datetime.utcnow().isoformat(),
            ),
        )
        episode_id = cur.lastrowid
        conn.commit()

        thumb_value = None

        if cover_file and cover_file.filename:
            filename = os.path.basename(cover_file.filename)
            base, ext = os.path.splitext(filename)
            ext = ext.lower() or ".jpg"

            ep_dir = os.path.join(EPISODE_COVER_ROOT, f"ep_{episode_id}")
            os.makedirs(ep_dir, exist_ok=True)

            safe_name = f"ep_{episode_id}_{int(datetime.utcnow().timestamp())}{ext}"
            save_path = os.path.join(ep_dir, safe_name)
            cover_file.save(save_path)

            thumb_value = cover_storage_path(save_path)

        elif thumbnail_url_input:
            thumb_value = thumbnail_url_input

        if thumb_value is not None:
            conn.execute(
                "UPDATE episodes SET thumbnail_url = ? WHERE id = ?",
                (thumb_value, episode_id),
            )
            conn.commit()

        flash("เพิ่มตอนใหม่สำเร็จแล้ว", "success")

    episodes = conn.execute(
        """
        SELECT * FROM episodes
        WHERE series_id = ?
        ORDER BY episode_number IS NULL, episode_number, datetime(created_at)
        """,
        (series_id,),
    ).fetchall()
    conn.close()

    return render_template(
        "admin_episodes.html", series=series, episodes=episodes
    )





@app.route("/admin/episodes/<int:episode_id>/toggle_visibility", methods=["POST"])
def admin_toggle_episode(episode_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    ep = conn.execute(
        "SELECT * FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()

    if ep is None:
        conn.close()
        flash("ไม่พบตอนนี้", "error")
        return redirect(url_for("admin_series"))

    series_id = ep["series_id"]

    current = 1
    try:
        if "is_active" in ep.keys() and ep["is_active"] is not None:
            current = int(ep["is_active"])
    except Exception:
        current = 1

    new_val = 0 if current == 1 else 1
    conn.execute(
        "UPDATE episodes SET is_active = ? WHERE id = ?",
        (new_val, episode_id),
    )
    conn.commit()
    conn.close()

    flash("อัปเดตสถานะการเปิด/ปิดตอนเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_episodes", series_id=series_id))

@app.route("/admin/episodes/<int:episode_id>/edit", methods=["GET", "POST"])
def admin_edit_episode(episode_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    ep = conn.execute(
        "SELECT * FROM episodes WHERE id = ?",
        (episode_id,),
    ).fetchone()
    if ep is None:
        conn.close()
        flash("ไม่พบตอนนี้", "error")
        return redirect(url_for("admin_series"))

    series = conn.execute(
        "SELECT * FROM series WHERE id = ?",
        (ep["series_id"],),
    ).fetchone()

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        episode_number_raw = request.form.get("episode_number", "").strip()
        mode = request.form.get("mode", "keep")
        thumbnail_url_input = request.form.get("thumbnail_url", "").strip()
        cover_file = request.files.get("cover_file")

        if not title:
            flash("กรุณากรอกชื่อตอน", "error")
            conn.close()
            return redirect(url_for("admin_edit_episode", episode_id=episode_id))

        episode_number = None
        if episode_number_raw:
            try:
                episode_number = int(episode_number_raw)
            except ValueError:
                flash("เลขตอนต้องเป็นตัวเลข", "error")
                conn.close()
                return redirect(url_for("admin_edit_episode", episode_id=episode_id))

        new_source_type = ep["source_type"]
        new_video_url = ep["video_url"]
        new_drive_id = ep["drive_id"]
        new_file_path = ep["file_path"]

        def delete_old_file(path):
            if not path:
                return
            fp_full = resolve_video_path(path)
            try:
                if os.path.exists(fp_full):
                    os.remove(fp_full)
            except Exception:
                pass

        if mode == "keep":
            pass
        elif mode == "direct":
            video_url = request.form.get("video_url", "").strip()
            if not video_url:
                flash("กรุณาใส่ลิงก์วิดีโอแบบ direct", "error")
                conn.close()
                return redirect(url_for("admin_edit_episode", episode_id=episode_id))

            if new_source_type in ("gdrive", "upload"):
                delete_old_file(new_file_path)
                new_file_path = None

            new_source_type = "direct"
            new_video_url = video_url
            new_drive_id = None

        elif mode == "gdrive":
            drive_link = request.form.get("drive_link", "").strip()
            if not drive_link:
                flash("กรุณาใส่ลิงก์หรือรหัสไฟล์ Google Drive", "error")
                conn.close()
                return redirect(url_for("admin_edit_episode", episode_id=episode_id))

            drive_id = extract_drive_id(drive_link)
            if not drive_id:
                flash("ไม่สามารถดึง Drive ID จากลิงก์ได้ กรุณาตรวจสอบอีกครั้ง", "error")
                conn.close()
                return redirect(url_for("admin_edit_episode", episode_id=episode_id))

            if new_source_type in ("gdrive", "upload"):
                delete_old_file(new_file_path)

            try:
                file_real = download_drive_file(drive_id, ep["series_id"])
            except Exception as e:
                flash(str(e), "error")
                conn.close()
                return redirect(url_for("admin_edit_episode", episode_id=episode_id))

            rel_path = video_storage_path(file_real)
            new_file_path = rel_path
            new_source_type = "gdrive"
            new_drive_id = drive_id
            new_video_url = None

        elif mode == "upload":
            file = request.files.get("file")
            if not file or file.filename == "":
                flash("กรุณาเลือกไฟล์วิดีโอสำหรับอัปโหลด", "error")
                conn.close()
                return redirect(url_for("admin_edit_episode", episode_id=episode_id))

            if new_source_type in ("gdrive", "upload"):
                delete_old_file(new_file_path)

            filename = os.path.basename(file.filename)
            base, ext = os.path.splitext(filename)
            ext = ext.lower() or ".mp4"

            series_dir = os.path.join(VIDEO_ROOT, f"series_{ep['series_id']}")
            os.makedirs(series_dir, exist_ok=True)

            safe_name = f"{base}_{int(datetime.utcnow().timestamp())}{ext}"
            save_path = os.path.join(series_dir, safe_name)
            file.save(save_path)

            rel_path = video_storage_path(save_path)
            new_file_path = rel_path
            new_source_type = "upload"
            new_video_url = None
            new_drive_id = None
        else:
            flash("โหมดที่เลือกไม่ถูกต้อง", "error")
            conn.close()
            return redirect(url_for("admin_edit_episode", episode_id=episode_id))

        conn.execute(
            """
            UPDATE episodes
            SET title = ?, description = ?, episode_number = ?, source_type = ?, video_url = ?, drive_id = ?, file_path = ?
            WHERE id = ?
            """,
            (
                title,
                description or None,
                episode_number,
                new_source_type,
                new_video_url,
                new_drive_id,
                new_file_path,
                episode_id,
            ),
        )

        thumb_value = None
        old_thumb = ep["thumbnail_url"]

        if cover_file and cover_file.filename:
            if old_thumb and not str(old_thumb).startswith("http"):
                delete_cover_file(old_thumb)

            filename = os.path.basename(cover_file.filename)
            base2, ext2 = os.path.splitext(filename)
            ext2 = ext2.lower() or ".jpg"

            ep_dir = os.path.join(EPISODE_COVER_ROOT, f"ep_{episode_id}")
            os.makedirs(ep_dir, exist_ok=True)

            safe_name2 = f"ep_{episode_id}_{int(datetime.utcnow().timestamp())}{ext2}"
            save_path2 = os.path.join(ep_dir, safe_name2)
            cover_file.save(save_path2)

            thumb_value = cover_storage_path(save_path2)
        elif thumbnail_url_input:
            thumb_value = thumbnail_url_input

        if thumb_value is not None:
            conn.execute(
                "UPDATE episodes SET thumbnail_url = ? WHERE id = ?",
                (thumb_value, episode_id),
            )

        conn.commit()
        conn.close()
        flash("บันทึกการแก้ไขตอนเรียบร้อยแล้ว", "success")
        return redirect(url_for("admin_episodes", series_id=ep["series_id"]))

    conn.close()
    return render_template("admin_edit_episode.html", series=series, episode=ep)

@app.route("/admin/episodes/<int:episode_id>/delete", methods=["POST"])
def admin_delete_episode(episode_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    ep = conn.execute(
        "SELECT id, series_id, file_path, thumbnail_url FROM episodes WHERE id = ?",
        (episode_id,),
    ).fetchone()
    if ep is None:
        conn.close()
        flash("ไม่พบตอนนี้", "error")
        return redirect(url_for("admin_series"))

    file_path = ep["file_path"]
    thumb = ep["thumbnail_url"]
    series_id = ep["series_id"]

    if file_path:
        fp_full = resolve_video_path(file_path)
        try:
            if os.path.exists(fp_full):
                os.remove(fp_full)
        except Exception:
            pass

    if thumb and not str(thumb).startswith("http"):
        delete_cover_file(thumb)

    conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
    conn.commit()
    conn.close()

    flash("ลบตอนเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_episodes", series_id=series_id))


# ---------- ระบบสำรอง/คืนค่า ----------
@app.route("/admin/backup", methods=["GET", "POST"])
def admin_backup():
    if not admin_required():
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        file = request.files.get("backup_file")
        if not file or not file.filename:
            flash("กรุณาเลือกไฟล์สำรอง (.json) ก่อน", "error")
            return redirect(url_for("admin_backup"))

        try:
            data = json.load(file.stream)
        except Exception:
            flash("ไฟล์ไม่อยู่ในรูปแบบ JSON ที่ถูกต้อง", "error")
            return redirect(url_for("admin_backup"))

        backup_type = data.get("type")
        if not backup_type:
            if "series" in data or "episodes" in data:
                backup_type = "videos"
            elif "users" in data or "watch_history" in data:
                backup_type = "users"
            else:
                backup_type = "other"

        mode = request.form.get("restore_mode", "replace")
        if mode not in ("replace", "merge"):
            mode = "replace"

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys = OFF;")

        try:
            if backup_type == "videos":
                series_list = data.get("series", []) or []
                episodes_list = data.get("episodes", []) or []

                ensure_episode_thumbnail_column(conn)

                if mode == "replace":
                    cur.execute("DELETE FROM episodes")
                    cur.execute("DELETE FROM series")
                    try:
                        cur.execute(
                            "DELETE FROM sqlite_sequence WHERE name IN ('series','episodes')"
                        )
                    except Exception:
                        pass

                for s in series_list:
                    sid = s.get("id")
                    if mode == "merge" and sid is not None:
                        existing = cur.execute(
                            "SELECT id FROM series WHERE id = ?", (sid,)
                        ).fetchone()
                    else:
                        existing = None

                    if existing:
                        cur.execute(
                            """
                            UPDATE series
                            SET title = ?, description = ?, thumbnail_url = ?, created_at = ?
                            WHERE id = ?
                            """,
                            (
                                s.get("title"),
                                s.get("description"),
                                s.get("thumbnail_url"),
                                s.get("created_at") or datetime.utcnow().isoformat(),
                                sid,
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO series (id, title, description, thumbnail_url, created_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                sid,
                                s.get("title"),
                                s.get("description"),
                                s.get("thumbnail_url"),
                                s.get("created_at") or datetime.utcnow().isoformat(),
                            ),
                        )

                for ep in episodes_list:
                    eid = ep.get("id")
                    if mode == "merge" and eid is not None:
                        existing = cur.execute(
                            "SELECT id FROM episodes WHERE id = ?", (eid,)
                        ).fetchone()
                    else:
                        existing = None

                    row = (
                        eid,
                        ep.get("series_id"),
                        ep.get("title"),
                        ep.get("description"),
                        ep.get("episode_number"),
                        ep.get("source_type"),
                        ep.get("video_url"),
                        ep.get("drive_id"),
                        ep.get("file_path"),
                        ep.get("thumbnail_url"),
                        ep.get("created_at") or datetime.utcnow().isoformat(),
                    )

                    if existing:
                        cur.execute(
                            """
                            UPDATE episodes
                            SET series_id = ?, title = ?, description = ?, episode_number = ?,
                                source_type = ?, video_url = ?, drive_id = ?, file_path = ?,
                                thumbnail_url = ?, created_at = ?
                            WHERE id = ?
                            """,
                            (
                                row[1],
                                row[2],
                                row[3],
                                row[4],
                                row[5],
                                row[6],
                                row[7],
                                row[8],
                                row[9],
                                row[10],
                                row[0],
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO episodes (
                                id, series_id, title, description, episode_number,
                                source_type, video_url, drive_id, file_path,
                                thumbnail_url, created_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            row,
                        )

                msg = "คืนค่าข้อมูลวิดีโอจากไฟล์สำเร็จแล้ว"

            elif backup_type == "users":
                users_list = data.get("users", []) or []
                history_list = data.get("watch_history", []) or []

                ensure_user_extra_columns(conn)

                if mode == "replace":
                    cur.execute("DELETE FROM watch_history")
                    cur.execute("DELETE FROM users")
                    try:
                        cur.execute(
                            "DELETE FROM sqlite_sequence WHERE name IN ('users','watch_history')"
                        )
                    except Exception:
                        pass

                for u in users_list:
                    uid = u.get("id")
                    if mode == "merge" and uid is not None:
                        existing = cur.execute(
                            "SELECT id FROM users WHERE id = ?", (uid,)
                        ).fetchone()
                    else:
                        existing = None

                    username = u.get("username")
                    password = u.get("password")
                    plain_password = u.get("plain_password")
                    user_key = u.get("user_key")
                    created_at = u.get("created_at") or datetime.utcnow().isoformat()

                    if existing:
                        cur.execute(
                            """
                            UPDATE users
                            SET username = ?, password = ?, plain_password = ?, user_key = ?, created_at = ?
                            WHERE id = ?
                            """,
                            (
                                username,
                                password,
                                plain_password,
                                user_key,
                                created_at,
                                uid,
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO users (id, username, password, plain_password, user_key, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                uid,
                                username,
                                password,
                                plain_password,
                                user_key,
                                created_at,
                            ),
                        )

                for h in history_list:
                    hid = h.get("id")
                    if mode == "merge" and hid is not None:
                        existing = cur.execute(
                            "SELECT id FROM watch_history WHERE id = ?", (hid,)
                        ).fetchone()
                    else:
                        existing = None

                    row = (
                        hid,
                        h.get("user_id"),
                        h.get("series_id"),
                        h.get("episode_id"),
                        h.get("watched_at") or datetime.utcnow().isoformat(),
                    )

                    if existing:
                        cur.execute(
                            """
                            UPDATE watch_history
                            SET user_id = ?, series_id = ?, episode_id = ?, watched_at = ?
                            WHERE id = ?
                            """,
                            (
                                row[1],
                                row[2],
                                row[3],
                                row[4],
                                row[0],
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO watch_history (id, user_id, series_id, episode_id, watched_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            row,
                        )

                msg = "คืนค่าข้อมูลบัญชีผู้ใช้และประวัติการดูจากไฟล์สำเร็จแล้ว"

            else:
                msg = "ไฟล์สำรองประเภทอื่นๆ ถูกอ่านสำเร็จ (ยังไม่มีข้อมูลอื่นให้คืนค่าในระบบนี้)"

            conn.commit()
            flash(msg, "success")
        except Exception as e:
            conn.rollback()
            flash("เกิดข้อผิดพลาดระหว่างคืนค่าข้อมูล: {}".format(e), "error")
        finally:
            conn.close()

        return redirect(url_for("admin_backup"))

    return render_template("admin_backup.html")


@app.route("/admin/backup/download/videos")
def admin_backup_download_videos():
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    series = conn.execute("SELECT * FROM series").fetchall()
    episodes = conn.execute("SELECT * FROM episodes").fetchall()
    conn.close()

    data = {
        "version": "myseries_backup_v2",
        "type": "videos",
        "exported_at": datetime.utcnow().isoformat(),
        "series": [dict(row) for row in series],
        "episodes": [dict(row) for row in episodes],
    }

    json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"Video-{datetime.now().strftime('%Y%m%d')}.json"  # รูปแบบ: Video-YYYYMMDD.json
    return Response(
        json_bytes,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/admin/backup/download/users")
def admin_backup_download_users():
    if not admin_required():
        return redirect(url_for("admin_login"))

    conn = get_db_connection()
    ensure_user_extra_columns(conn)
    users = conn.execute("SELECT * FROM users").fetchall()
    history = conn.execute("SELECT * FROM watch_history").fetchall()
    conn.close()

    data = {
        "version": "myseries_backup_v2",
        "type": "users",
        "exported_at": datetime.utcnow().isoformat(),
        "users": [dict(row) for row in users],
        "watch_history": [dict(row) for row in history],
    }

    json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"user-{datetime.now().strftime('%Y%m%d')}.json"  # รูปแบบ: user-YYYYMMDD.json
    return Response(
        json_bytes,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/admin/backup/download/other")
def admin_backup_download_other():
    if not admin_required():
        return redirect(url_for("admin_login"))

    # ปัจจุบันยังไม่มีข้อมูลอื่นที่ต้องสำรอง แต่อาจใช้ในอนาคต
    data = {
        "version": "myseries_backup_v2",
        "type": "other",
        "exported_at": datetime.utcnow().isoformat(),
        "data": {},
    }

    json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"another-{datetime.now().strftime('%Y%m%d')}.json"  # รูปแบบ: another-YYYYMMDD.json
    return Response(
        json_bytes,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/admin/backup/download")
def admin_backup_download():
    # เพื่อความเข้ากันได้กับเวอร์ชันเก่า ให้รีไดเรกต์ไปที่ไฟล์วิดีโอ
    return redirect(url_for("admin_backup_download_videos"))



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=env_flag("FLASK_DEBUG", default=False),
    )
