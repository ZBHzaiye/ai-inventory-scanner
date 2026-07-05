import os
import re
import json
import base64
import sqlite3
import zipfile
import tempfile
from io import BytesIO
from datetime import datetime

import requests as http_requests
from flask import Flask, request, jsonify, send_file, render_template, send_from_directory
from PIL import Image
from dotenv import load_dotenv
import openpyxl

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "db.sqlite")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS batches (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS items (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id         INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                part_number      TEXT    NOT NULL,
                base_part_number TEXT    NOT NULL,
                name_cn          TEXT    NOT NULL DEFAULT '',
                quantity         INTEGER NOT NULL DEFAULT 0,
                location         TEXT    NOT NULL DEFAULT '',
                created_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS images (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                file_name  TEXT    NOT NULL,
                file_path  TEXT    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            );
        """)


init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ok(data=None):
    return jsonify({"success": True, "data": data})


def err(msg, status=400):
    return jsonify({"success": False, "error": msg}), status


def compress_image(file_obj, max_side=1200) -> bytes:
    """Compress image to max longest side, return JPEG bytes."""
    img = Image.open(file_obj)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        ratio = max_side / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def row_to_dict(row) -> dict:
    return dict(row)


def get_next_image_filename(base_part: str) -> str:
    """
    Find the next available filename for a given base_part_number.
    First image: {base_part}.jpg
    Subsequent: {base_part}-2.jpg, {base_part}-3.jpg …
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT file_name FROM images WHERE file_name LIKE ?",
            (f"{base_part}%",)
        ).fetchall()
    existing = {r["file_name"] for r in rows}
    candidate = f"{base_part}.jpg"
    if candidate not in existing:
        return candidate
    n = 2
    while True:
        candidate = f"{base_part}-{n}.jpg"
        if candidate not in existing:
            return candidate
        n += 1


def save_image_file(data: bytes, filename: str) -> str:
    path = os.path.join(UPLOAD_DIR, filename)
    with open(path, "wb") as f:
        f.write(data)
    return path


def delete_image_files(item_id: int):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT file_path FROM images WHERE item_id = ?", (item_id,)
        ).fetchall()
    for row in rows:
        try:
            if os.path.exists(row["file_path"]):
                os.remove(row["file_path"])
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Static — serve uploaded images
# ---------------------------------------------------------------------------

@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# API — Batches
# ---------------------------------------------------------------------------

@app.route("/api/batches", methods=["GET"])
def list_batches():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT b.id, b.name, b.created_at,
                   COUNT(i.id) AS item_count
            FROM batches b
            LEFT JOIN items i ON i.batch_id = b.id
            GROUP BY b.id
            ORDER BY b.created_at DESC
        """).fetchall()
    return ok([row_to_dict(r) for r in rows])


@app.route("/api/batches/<int:batch_id>", methods=["DELETE"])
def delete_batch(batch_id):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT id FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if not batch:
            return err("批次不存在", 404)
        # Collect all image file paths for this batch
        rows = conn.execute(
            """SELECT img.file_path FROM images img
               JOIN items i ON i.id = img.item_id
               WHERE i.batch_id = ?""",
            (batch_id,)
        ).fetchall()

    # Delete image files from disk
    for row in rows:
        try:
            if os.path.exists(row["file_path"]):
                os.remove(row["file_path"])
        except OSError:
            pass

    # Delete batch (cascades to items and images via FK)
    with get_db() as conn:
        conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))

    return ok({"deleted_id": batch_id})


@app.route("/api/batches", methods=["POST"])
def create_batch():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return err("批次名称不能为空")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO batches (name) VALUES (?)", (name,)
        )
        batch_id = cur.lastrowid
        row = conn.execute(
            "SELECT id, name, created_at FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
    return ok(row_to_dict(row)), 201


# ---------------------------------------------------------------------------
# API — Items
# ---------------------------------------------------------------------------

@app.route("/api/batches/<int:batch_id>/items", methods=["GET"])
def list_items(batch_id):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT id FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if not batch:
            return err("批次不存在", 404)

        items = conn.execute(
            "SELECT * FROM items WHERE batch_id = ? ORDER BY created_at DESC",
            (batch_id,)
        ).fetchall()

        result = []
        for item in items:
            d = row_to_dict(item)
            imgs = conn.execute(
                "SELECT id, file_name FROM images WHERE item_id = ? ORDER BY created_at",
                (item["id"],)
            ).fetchall()
            d["images"] = [row_to_dict(img) for img in imgs]
            result.append(d)

    return ok(result)


@app.route("/api/batches/<int:batch_id>/items", methods=["POST"])
def create_item(batch_id):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT id FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if not batch:
            return err("批次不存在", 404)

    part_number = (request.form.get("part_number") or "").strip()
    name_cn = (request.form.get("name_cn") or "").strip()
    location = (request.form.get("location") or "").strip()
    try:
        quantity = int(request.form.get("quantity") or 0)
    except ValueError:
        return err("数量必须为整数")

    if not part_number:
        return err("物料号不能为空")

    base_part = part_number

    image_file = request.files.get("image")
    if not image_file:
        return err("请上传图片")

    try:
        img_bytes = compress_image(image_file)
    except Exception as e:
        return err(f"图片处理失败: {e}")

    filename = get_next_image_filename(base_part)
    save_image_file(img_bytes, filename)
    file_path = os.path.join(UPLOAD_DIR, filename)

    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO items (batch_id, part_number, base_part_number, name_cn, quantity, location)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (batch_id, part_number, base_part, name_cn, quantity, location)
        )
        item_id = cur.lastrowid
        conn.execute(
            "INSERT INTO images (item_id, file_name, file_path) VALUES (?, ?, ?)",
            (item_id, filename, file_path)
        )
        item = conn.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        imgs = conn.execute(
            "SELECT id, file_name FROM images WHERE item_id = ?", (item_id,)
        ).fetchall()

    result = row_to_dict(item)
    result["images"] = [row_to_dict(i) for i in imgs]
    return ok(result), 201


@app.route("/api/items/<int:item_id>", methods=["PUT"])
def update_item(item_id):
    body = request.get_json(silent=True) or {}
    with get_db() as conn:
        item = conn.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not item:
            return err("条目不存在", 404)

        fields = {}
        if "location" in body:
            fields["location"] = str(body["location"]).strip()
        if "name_cn" in body:
            fields["name_cn"] = str(body["name_cn"]).strip()
        if "quantity" in body:
            try:
                fields["quantity"] = int(body["quantity"])
            except ValueError:
                return err("数量必须为整数")
        if "part_number" in body:
            fields["part_number"] = str(body["part_number"]).strip()

        if not fields:
            return err("没有可更新的字段")

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [item_id]
        conn.execute(f"UPDATE items SET {set_clause} WHERE id = ?", values)

        updated = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        imgs = conn.execute(
            "SELECT id, file_name FROM images WHERE item_id = ?", (item_id,)
        ).fetchall()

    result = row_to_dict(updated)
    result["images"] = [row_to_dict(i) for i in imgs]
    return ok(result)


@app.route("/api/items/<int:item_id>/merge", methods=["PUT"])
def merge_item(item_id):
    """Merge new quantity + image into an existing item."""
    try:
        add_qty = int(request.form.get("quantity") or 0)
    except ValueError:
        return err("数量必须为整数")

    new_location = (request.form.get("location") or "").strip()

    with get_db() as conn:
        item = conn.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not item:
            return err("条目不存在", 404)

    # Merge location: append new location if not already present
    existing_locs = [l.strip() for l in (item["location"] or "").split(",") if l.strip()]
    if new_location and new_location not in existing_locs:
        existing_locs.append(new_location)
    merged_location = ", ".join(existing_locs)

    image_file = request.files.get("image")
    if image_file:
        try:
            img_bytes = compress_image(image_file)
        except Exception as e:
            return err(f"图片处理失败: {e}")
        filename = get_next_image_filename(item["base_part_number"])
        save_image_file(img_bytes, filename)
        file_path = os.path.join(UPLOAD_DIR, filename)
        with get_db() as conn:
            conn.execute(
                "INSERT INTO images (item_id, file_name, file_path) VALUES (?, ?, ?)",
                (item_id, filename, file_path)
            )

    with get_db() as conn:
        conn.execute(
            "UPDATE items SET quantity = quantity + ?, location = ? WHERE id = ?",
            (add_qty, merged_location, item_id)
        )
        updated = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        imgs = conn.execute(
            "SELECT id, file_name FROM images WHERE item_id = ?", (item_id,)
        ).fetchall()

    result = row_to_dict(updated)
    result["images"] = [row_to_dict(i) for i in imgs]
    return ok(result)


@app.route("/api/items/<int:item_id>", methods=["DELETE"])
def delete_item(item_id):
    with get_db() as conn:
        item = conn.execute(
            "SELECT id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not item:
            return err("条目不存在", 404)

    delete_image_files(item_id)

    with get_db() as conn:
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))

    return ok({"deleted_id": item_id})


# ---------------------------------------------------------------------------
# API — Recognize
# ---------------------------------------------------------------------------

@app.route("/api/recognize", methods=["POST"])
def recognize():
    api_key = os.environ.get("ZHIPU_API_KEY", "")
    if not api_key:
        return err("未配置 ZHIPU_API_KEY 环境变量", 500)

    image_file = request.files.get("image")
    if not image_file:
        return err("请上传图片")

    try:
        img_bytes = compress_image(image_file)
    except Exception as e:
        return err(f"图片处理失败: {e}")

    b64 = base64.b64encode(img_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"

    prompt = (
        "请从图片中的配件标签提取以下信息，以JSON格式返回，不要加任何其他文字：\n"
        "{\n"
        "  \"part_number\": \"物料号（标签上最显眼的编号，如3701-01515）\",\n"
        "  \"name_cn\": \"中文名称\",\n"
        "  \"quantity\": \"数量（只要数字）\"\n"
        "}\n"
        "如果标签倒置请自行旋转识别。只返回JSON，不要加代码块符号。"
    )

    try:
        resp = http_requests.post(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "glm-4v-plus",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            },
            timeout=60,
        )
        if resp.status_code != 200:
            return err(f"识别失败: Error code: {resp.status_code} - {resp.text}", 500)
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return err(f"AI返回内容无法解析为JSON: {raw}")
    except Exception as e:
        return err(f"识别失败: {e}", 500)

    result = {
        "part_number": str(parsed.get("part_number") or "").strip(),
        "name_cn": str(parsed.get("name_cn") or "").strip(),
        "quantity": str(parsed.get("quantity") or "").strip(),
    }
    return ok(result)


# ---------------------------------------------------------------------------
# API — Duplicate check
# ---------------------------------------------------------------------------

@app.route("/api/batches/<int:batch_id>/check_duplicate", methods=["POST"])
def check_duplicate(batch_id):
    body = request.get_json(silent=True) or {}
    part_number = (body.get("part_number") or "").strip()
    if not part_number:
        return err("物料号不能为空")

    with get_db() as conn:
        row = conn.execute(
            """SELECT i.id, i.part_number, i.name_cn, i.quantity, i.location
               FROM items i
               WHERE i.batch_id = ? AND i.part_number = ?
               LIMIT 1""",
            (batch_id, part_number)
        ).fetchone()

    if row:
        return ok({"duplicate": True, "item": row_to_dict(row)})
    return ok({"duplicate": False})


# ---------------------------------------------------------------------------
# API — Export
# ---------------------------------------------------------------------------

@app.route("/api/batches/<int:batch_id>/export/excel")
def export_excel(batch_id):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if not batch:
            return err("批次不存在", 404)

        items = conn.execute(
            "SELECT * FROM items WHERE batch_id = ? ORDER BY created_at",
            (batch_id,)
        ).fetchall()

        rows = []
        for item in items:
            imgs = conn.execute(
                "SELECT file_name FROM images WHERE item_id = ? ORDER BY created_at",
                (item["id"],)
            ).fetchall()
            img_names = ", ".join(i["file_name"] for i in imgs)
            rows.append({
                "part_number": item["part_number"],
                "name_cn": item["name_cn"],
                "quantity": item["quantity"],
                "location": item["location"],
                "images": img_names,
            })

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "入库清单"
    headers = ["序号", "物料号", "中文名称", "数量", "库位", "图片文件名"]
    ws.append(headers)
    for idx, row in enumerate(rows, 1):
        ws.append([
            idx,
            row["part_number"],
            row["name_cn"],
            row["quantity"],
            row["location"],
            row["images"],
        ])

    # Column widths
    for col, width in zip("ABCDEF", [6, 20, 25, 8, 15, 40]):
        ws.column_dimensions[col].width = width

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    safe_name = re.sub(r'[\\/:*?"<>|]', "_", batch["name"])
    filename = f"{safe_name}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/batches/<int:batch_id>/export/zip")
def export_zip(batch_id):
    with get_db() as conn:
        batch = conn.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if not batch:
            return err("批次不存在", 404)

        items = conn.execute(
            "SELECT * FROM items WHERE batch_id = ? ORDER BY created_at",
            (batch_id,)
        ).fetchall()

        all_images = []
        rows = []
        for item in items:
            imgs = conn.execute(
                "SELECT file_name, file_path FROM images WHERE item_id = ? ORDER BY created_at",
                (item["id"],)
            ).fetchall()
            img_names = ", ".join(i["file_name"] for i in imgs)
            rows.append({
                "part_number": item["part_number"],
                "name_cn": item["name_cn"],
                "quantity": item["quantity"],
                "location": item["location"],
                "images": img_names,
            })
            all_images.extend(imgs)

    # Build Excel in memory
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "入库清单"
    ws.append(["序号", "物料号", "中文名称", "数量", "库位", "图片文件名"])
    for idx, row in enumerate(rows, 1):
        ws.append([
            idx,
            row["part_number"],
            row["name_cn"],
            row["quantity"],
            row["location"],
            row["images"],
        ])
    for col, width in zip("ABCDEF", [6, 20, 25, 8, 15, 40]):
        ws.column_dimensions[col].width = width

    excel_buf = BytesIO()
    wb.save(excel_buf)
    excel_bytes = excel_buf.getvalue()

    safe_name = re.sub(r'[\\/:*?"<>|]', "_", batch["name"])

    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{safe_name}.xlsx", excel_bytes)
        for img in all_images:
            if os.path.exists(img["file_path"]):
                zf.write(img["file_path"], f"images/{img['file_name']}")

    zip_buf.seek(0)
    return send_file(
        zip_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{safe_name}.zip",
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
