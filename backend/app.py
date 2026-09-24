import os
from functools import wraps

import psycopg2
from flask import Flask, abort, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrap


def writer_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "writer":
            return ("仅审评员可操作母批", 403)
        return fn(*args, **kwargs)

    return wrap


def latest_members(cur, mother_id):
    """当前在册子批及其最新一轮审评加权分。

    成员身份由事件流最后一条 add/remove 决定；剔除只追加 remove 事件，
    不删历史，因此重新钉入会留下完整履历。
    """
    cur.execute(
        """
        WITH last_event AS (
            SELECT DISTINCT ON (lot) lot, action, acted_at
            FROM mother_lot_events
            WHERE mother_id = %s
            ORDER BY lot, id DESC
        )
        SELECT le.lot,
               le.acted_at AS joined_at,
               c.score,
               c.verdict,
               c.note
        FROM last_event le
        LEFT JOIN LATERAL (
            SELECT score, verdict, note
            FROM cuppings
            WHERE lot = le.lot
            ORDER BY id DESC
            LIMIT 1
        ) c ON true
        WHERE le.action = 'add'
        ORDER BY le.lot
        """,
        (mother_id,),
    )
    return cur.fetchall()


def mother_average(members):
    scores = [m["score"] for m in members if m["score"] is not None]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 2)


@app.get("/health")
def health():
    return {"status": "ok", "service": "tea-blend-cupping"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        account = ACCOUNTS.get(name)
        if not account or account["password"] != request.form.get("password", ""):
            error = "用户名或密码错误"
        else:
            session["user"] = name
            session["role"] = account["role"]
            return redirect(url_for("home"))
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        rows = cur.fetchall()
    return render_template("home.html", rows=rows, can_write=session.get("role") == "writer")


@app.post("/cuppings")
@login_required
def create():
    if session.get("role") != "writer":
        return ("仅审评员可提交拼配审评", 403)
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"]),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row)
    return redirect(url_for("home"))


@app.get("/mothers")
@login_required
def mothers():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM mother_lots ORDER BY id")
        items = cur.fetchall()
        for item in items:
            item["members"] = latest_members(cur, item["id"])
            item["average"] = mother_average(item["members"])
            item["member_count"] = len(item["members"])
    return render_template(
        "mothers.html", items=items, can_write=session.get("role") == "writer"
    )


@app.post("/mothers")
@writer_required
def create_mother():
    name = request.form.get("name", "").strip()
    if not name:
        return ("母批名称不能为空", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        try:
            cur.execute(
                "INSERT INTO mother_lots (name, created_by) VALUES (%s,%s) RETURNING id",
                (name, session["user"]),
            )
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            return ("母批名称已存在", 409)
        mother_id = cur.fetchone()["id"]
        conn.commit()
    return redirect(url_for("mother_detail", mother_id=mother_id))


def _fetch_mother(cur, mother_id):
    cur.execute("SELECT * FROM mother_lots WHERE id = %s", (mother_id,))
    mother = cur.fetchone()
    if mother is None:
        abort(404)
    return mother


@app.get("/mothers/<int:mother_id>")
@login_required
def mother_detail(mother_id):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        mother = _fetch_mother(cur, mother_id)
        members = latest_members(cur, mother_id)
        average = mother_average(members)
        cur.execute("SELECT DISTINCT lot FROM cuppings ORDER BY lot")
        all_lots = [r["lot"] for r in cur.fetchall()]
        current_lots = {m["lot"] for m in members}
        available_lots = [lot for lot in all_lots if lot not in current_lots]
        cur.execute(
            """SELECT * FROM mother_lot_events
               WHERE mother_id = %s ORDER BY id DESC""",
            (mother_id,),
        )
        events = cur.fetchall()
    return render_template(
        "mother_detail.html",
        mother=mother,
        members=members,
        average=average,
        available_lots=available_lots,
        events=events,
        can_write=session.get("role") == "writer",
    )


@app.post("/mothers/<int:mother_id>/add")
@writer_required
def add_member(mother_id):
    lot = request.form.get("lot", "").strip()
    if not lot:
        return ("子批名称不能为空", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        _fetch_mother(cur, mother_id)
        cur.execute("SELECT 1 FROM cuppings WHERE lot = %s LIMIT 1", (lot,))
        if cur.fetchone() is None:
            return ("该子批尚无审评记录，无法钉入母批", 400)
        current = {m["lot"] for m in latest_members(cur, mother_id)}
        if lot in current:
            return ("该子批已在母批中", 409)
        cur.execute(
            """INSERT INTO mother_lot_events (mother_id, lot, action, acted_by)
               VALUES (%s,%s,'add',%s)""",
            (mother_id, lot, session["user"]),
        )
        conn.commit()
    return redirect(url_for("mother_detail", mother_id=mother_id))


@app.post("/mothers/<int:mother_id>/remove")
@writer_required
def remove_member(mother_id):
    lot = request.form.get("lot", "").strip()
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        _fetch_mother(cur, mother_id)
        current = {m["lot"] for m in latest_members(cur, mother_id)}
        if lot not in current:
            return ("该子批不在母批中", 400)
        cur.execute(
            """INSERT INTO mother_lot_events (mother_id, lot, action, acted_by)
               VALUES (%s,%s,'remove',%s)""",
            (mother_id, lot, session["user"]),
        )
        conn.commit()
    return redirect(url_for("mother_detail", mother_id=mother_id))
