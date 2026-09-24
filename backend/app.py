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

# 每个子批最新一轮审评（id 最大者）及其加权分
LATEST_SCORES_SQL = """
    SELECT DISTINCT ON (lot) lot, score, verdict, id AS cupping_id, created_at
      FROM cuppings
     ORDER BY lot, id DESC
"""
# 嵌入上述子查询的占位标记，避免与 psycopg2 的 %s 参数占位符冲突
LATEST = "__latest_scores__"


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
    @login_required
    def wrap(*args, **kwargs):
        if session.get("role") != "writer":
            return ("仅审评员可钉母批或剔除子批", 403)
        return fn(*args, **kwargs)

    return wrap


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
        cur.execute(LATEST_SCORES_SQL)
        lots = cur.fetchall()
    return render_template(
        "home.html",
        rows=rows,
        lots=lots,
        can_write=session.get("role") == "writer",
    )


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


@app.get("/blends")
@login_required
def blends():
    """母批报告专页：每个母批的均分均由服务端按子批最新加权分实时算出。"""
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT b.id, b.name, b.created_by, b.created_at,
                   ls.lot, ls.score AS lot_score, ls.verdict,
                   bl.pinned_by, bl.pinned_at
              FROM blends b
              JOIN blend_lots bl ON bl.blend_id = b.id AND bl.removed_at IS NULL
              JOIN (__latest_scores__) ls ON ls.lot = bl.lot
             ORDER BY b.id, bl.pinned_at
            """.replace(LATEST, LATEST_SCORES_SQL)
        )
        members = cur.fetchall()
        cur.execute("SELECT lot FROM (__latest_scores__) ls ORDER BY lot".replace(LATEST, LATEST_SCORES_SQL))
        lots = [r["lot"] for r in cur.fetchall()]
    grouped = {}
    for m in members:
        grouped.setdefault(m["id"], []).append(m)
    reports = []
    for blend_id, items in grouped.items():
        scores = [i["lot_score"] for i in items]
        reports.append(
            {
                "id": blend_id,
                "name": items[0]["name"],
                "created_by": items[0]["created_by"],
                "created_at": items[0]["created_at"],
                "members": items,
                "avg_score": round(sum(scores) / len(scores), 2),
            }
        )
    return render_template(
        "blends.html",
        reports=reports,
        lots=lots,
        can_write=session.get("role") == "writer",
    )


@app.post("/blends")
@writer_required
def create_blend():
    """钉成母批：新建母批并把选中的子批钉进去。"""
    name = request.form.get("name", "").strip()
    selected = [l.strip() for l in request.form.getlist("lots") if l.strip()]
    if not name:
        return ("母批名称必填", 400)
    if not selected:
        return ("至少要钉一个子批", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "INSERT INTO blends (name, created_by) VALUES (%s,%s) RETURNING id",
            (name, session["user"]),
        )
        blend_id = cur.fetchone()["id"]
        cur.executemany(
            """INSERT INTO blend_lots (blend_id, lot, pinned_by)
               VALUES (%s,%s,%s)
               ON CONFLICT (blend_id, lot) WHERE removed_at IS NULL DO NOTHING""",
            [(blend_id, lot, session["user"]) for lot in dict.fromkeys(selected)],
        )
        conn.commit()
    return redirect(url_for("blend_detail", blend_id=blend_id))


@app.get("/blends/<int:blend_id>")
@login_required
def blend_detail(blend_id):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM blends WHERE id = %s", (blend_id,))
        blend = cur.fetchone()
        if not blend:
            abort(404)
        cur.execute(
            """
            SELECT bl.lot, bl.pinned_by, bl.pinned_at,
                   ls.score AS lot_score, ls.verdict, ls.cupping_id
              FROM blend_lots bl
              JOIN (__latest_scores__) ls ON ls.lot = bl.lot
             WHERE bl.blend_id = %s AND bl.removed_at IS NULL
             ORDER BY bl.pinned_at
            """.replace(LATEST, LATEST_SCORES_SQL),
            (blend_id,),
        )
        members = cur.fetchall()
        # 可追加钉入的候选：当前不在册的已审评批次（剔除后重钉会留下完整履历）
        cur.execute(
            """SELECT ls.lot FROM (__latest_scores__) ls
                WHERE ls.lot NOT IN (
                    SELECT lot FROM blend_lots WHERE blend_id = %s AND removed_at IS NULL
                )
               ORDER BY ls.lot""".replace(LATEST, LATEST_SCORES_SQL),
            (blend_id,),
        )
        available = [r["lot"] for r in cur.fetchall()]
        cur.execute(
            """SELECT lot, pinned_by, pinned_at, removed_by, removed_at
                 FROM blend_lots
                WHERE blend_id = %s
                ORDER BY pinned_at DESC""",
            (blend_id,),
        )
        history = cur.fetchall()
    scores = [m["lot_score"] for m in members]
    avg_score = round(sum(scores) / len(scores), 2) if scores else None
    return render_template(
        "blend_detail.html",
        blend=blend,
        members=members,
        available=available,
        history=history,
        avg_score=avg_score,
        can_write=session.get("role") == "writer",
    )


@app.post("/blends/<int:blend_id>/lots")
@writer_required
def pin_lot(blend_id):
    """向已存在的母批追加钉入子批。"""
    lot = request.form.get("lot", "").strip()
    if not lot:
        return ("子批必填", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if not _blend_exists(cur, blend_id):
            abort(404)
        cur.execute(
            "SELECT 1 FROM (__latest_scores__) ls WHERE lot = %s".replace(LATEST, LATEST_SCORES_SQL),
            (lot,),
        )
        if cur.fetchone() is None:
            return ("该批次尚无审评记录", 400)
        cur.execute(
            """INSERT INTO blend_lots (blend_id, lot, pinned_by)
               VALUES (%s,%s,%s)
               ON CONFLICT (blend_id, lot) WHERE removed_at IS NULL DO NOTHING""",
            (blend_id, lot, session["user"]),
        )
        conn.commit()
    return redirect(url_for("blend_detail", blend_id=blend_id))


@app.post("/blends/<int:blend_id>/remove")
@writer_required
def remove_lot(blend_id):
    """剔除子批：软删除，原钉入记录保留在履历里。"""
    lot = request.form.get("lot", "").strip()
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """UPDATE blend_lots
                  SET removed_by = %s, removed_at = now()
                WHERE blend_id = %s AND lot = %s AND removed_at IS NULL""",
            (session["user"], blend_id, lot),
        )
        if cur.rowcount == 0:
            conn.rollback()
            abort(404)
        conn.commit()
    return redirect(url_for("blend_detail", blend_id=blend_id))


def _blend_exists(cur, blend_id):
    cur.execute("SELECT 1 FROM blends WHERE id = %s", (blend_id,))
    return cur.fetchone() is not None
