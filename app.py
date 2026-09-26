"""数字档案长期保存服务：SQLite 多副本、哈希校验、修复与迁移。"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "preservation.db"
MAX_FILE_SIZE = 10 * 1024 * 1024


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def verify_manifest(files: object) -> list[dict]:
    if not isinstance(files, list) or not files:
        raise BusinessError("files 必须是非空数组", 422, "invalid_manifest")
    result, seen = [], set()
    for item in files:
        if not isinstance(item, dict):
            raise BusinessError("文件条目必须是对象", 422, "invalid_manifest")
        raw_path = str(item.get("path", "")).strip().replace("\\", "/")
        pure = PurePosixPath(raw_path)
        if not raw_path or pure.is_absolute() or ".." in pure.parts or pure.name in {"", ".", ".."}:
            raise BusinessError(f"档案路径不安全: {raw_path}", 422, "unsafe_path")
        if raw_path in seen:
            raise BusinessError(f"档案路径重复: {raw_path}", 409, "duplicate_path")
        seen.add(raw_path)
        encoded = item.get("content_b64")
        if not isinstance(encoded, str):
            raise BusinessError(f"{raw_path} 缺少 content_b64", 422, "content_required")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise BusinessError(f"{raw_path} 不是合法 Base64", 422, "invalid_base64")
        if len(content) > MAX_FILE_SIZE:
            raise BusinessError(f"{raw_path} 超过单文件大小限制", 413, "file_too_large")
        result.append(
            {"path": raw_path, "content": content, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        )
    return result


class PreservationStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self) -> None:
        with self._lock, self.connect() as conn:
            # 旧版数据库的 copies.state CHECK 不含替换流程状态，需在建表脚本之前整表迁移。
            old_check = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='copies'").fetchone()
            if old_check and "pending_retirement" not in (old_check["sql"] or ""):
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.executescript(
                    """
                    ALTER TABLE copies RENAME TO copies_legacy;
                    CREATE TABLE copies(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        version_id INTEGER NOT NULL REFERENCES archive_versions(id),
                        location TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'healthy' CHECK(state IN ('healthy','corrupt','pending_retirement','rebuilding','retired')),
                        created_at TEXT NOT NULL,
                        last_verified_at TEXT,
                        replaces_copy_id INTEGER REFERENCES copies(id),
                        UNIQUE(version_id,location)
                    );
                    INSERT INTO copies(id,version_id,location,state,created_at,last_verified_at,replaces_copy_id)
                    SELECT id,version_id,location,
                           CASE state WHEN 'degraded' THEN 'pending_retirement' ELSE state END,
                           created_at,last_verified_at,NULL FROM copies_legacy;
                    CREATE TABLE copy_files_new(
                        copy_id INTEGER NOT NULL REFERENCES copies(id),
                        path TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        content BLOB NOT NULL,
                        PRIMARY KEY(copy_id,path)
                    );
                    INSERT INTO copy_files_new(copy_id,path,sha256,size,content)
                    SELECT copy_id,path,sha256,size,content FROM copy_files;
                    DROP TABLE copy_files;
                    ALTER TABLE copy_files_new RENAME TO copy_files;
                    DROP TABLE copies_legacy;
                    """
                )
                conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('owner','archivist','auditor'))
                );
                CREATE TABLE IF NOT EXISTS archives(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL REFERENCES users(id),
                    retention_until TEXT NOT NULL,
                    restricted INTEGER NOT NULL DEFAULT 1 CHECK(restricted IN (0,1)),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS archive_members(
                    archive_id INTEGER NOT NULL REFERENCES archives(id),
                    user_id TEXT NOT NULL REFERENCES users(id),
                    permission TEXT NOT NULL CHECK(permission IN ('read','write')),
                    PRIMARY KEY(archive_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS archive_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    archive_id INTEGER NOT NULL REFERENCES archives(id),
                    version INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'verified' CHECK(state IN ('verified','degraded')),
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    UNIQUE(archive_id,version)
                );
                CREATE TABLE IF NOT EXISTS archive_files(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES archive_versions(id),
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    content BLOB NOT NULL,
                    UNIQUE(version_id,path)
                );
                CREATE TABLE IF NOT EXISTS copies(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES archive_versions(id),
                    location TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'healthy' CHECK(state IN ('healthy','corrupt','pending_retirement','rebuilding','retired')),
                    created_at TEXT NOT NULL,
                    last_verified_at TEXT,
                    replaces_copy_id INTEGER REFERENCES copies(id),
                    UNIQUE(version_id,location)
                );
                CREATE TABLE IF NOT EXISTS damage_reports(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES archive_versions(id),
                    copy_id INTEGER NOT NULL REFERENCES copies(id),
                    found_at TEXT NOT NULL,
                    corrupt_paths TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved')),
                    resolved_at TEXT,
                    replacement_copy_id INTEGER REFERENCES copies(id)
                );
                CREATE TABLE IF NOT EXISTS retired_locations(
                    location TEXT PRIMARY KEY,
                    copy_id INTEGER NOT NULL,
                    version_id INTEGER NOT NULL,
                    archive_id INTEGER NOT NULL,
                    retired_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS copy_files(
                    copy_id INTEGER NOT NULL REFERENCES copies(id),
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    content BLOB NOT NULL,
                    PRIMARY KEY(copy_id,path)
                );
                CREATE TABLE IF NOT EXISTS migrations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_version_id INTEGER NOT NULL REFERENCES archive_versions(id),
                    target_version_id INTEGER NOT NULL UNIQUE REFERENCES archive_versions(id),
                    source_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    target_format TEXT NOT NULL,
                    actor_id TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    archive_id INTEGER NOT NULL REFERENCES archives(id),
                    actor_id TEXT NOT NULL REFERENCES users(id),
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def seed(self) -> None:
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("owner", "机构档案负责人", "owner"),
                    ("archivist", "档案管理员", "archivist"),
                    ("auditor", "独立审计员", "auditor"),
                    ("outsider", "未授权访客", "auditor"),
                ],
            )

    def _user(self, conn, user_id: str | None, roles: set[str] | None = None) -> sqlite3.Row:
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _access(self, conn, archive_id: int, user: sqlite3.Row, require_write: bool = False) -> None:
        archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
        if not archive:
            raise BusinessError("档案不存在", 404, "not_found")
        if archive["owner_id"] == user["id"]:
            return
        row = conn.execute(
            "SELECT permission FROM archive_members WHERE archive_id=? AND user_id=?", (archive_id, user["id"])
        ).fetchone()
        if not row or (require_write and row["permission"] != "write"):
            raise BusinessError("没有该受限档案的访问权限", 403, "forbidden")

    def _audit(self, conn, archive_id: int, actor: str, action: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit_log(archive_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (archive_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _copy_diff(self, conn, copy_id: int, version_id: int) -> dict:
        """以版本清单为基准，返回副本缺失/不一致/多出的文件。"""
        expected = {r["path"]: r for r in conn.execute(
            "SELECT path,sha256,size FROM archive_files WHERE version_id=?", (version_id,)
        ).fetchall()}
        actual = {r["path"]: r for r in conn.execute(
            "SELECT path,sha256,size,content FROM copy_files WHERE copy_id=?", (copy_id,)
        ).fetchall()}
        missing, mismatched = [], []
        for path, meta in expected.items():
            row = actual.get(path)
            if row is None:
                missing.append(path)
            elif hashlib.sha256(row["content"]).hexdigest() != meta["sha256"] or len(row["content"]) != meta["size"]:
                mismatched.append(path)
        extra = [p for p in actual if p not in expected]
        return {"missing": missing, "mismatched": mismatched, "extra": extra}

    def _healthy_donor(self, conn, version_id: int, exclude: int | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM copies WHERE version_id=? AND state='healthy'"
        params: list = [version_id]
        if exclude is not None:
            sql += " AND id<>?"
            params.append(exclude)
        sql += " ORDER BY last_verified_at DESC LIMIT 1"
        candidate = conn.execute(sql, params).fetchone()
        if candidate is None:
            return None
        diff = self._copy_diff(conn, candidate["id"], version_id)
        if diff["missing"] or diff["mismatched"] or diff["extra"]:
            return None
        return candidate

    def _protection(self, conn, version_id: int) -> dict:
        """受保护的唯一标准：至少两份健康副本；否则点明缺少的保障。"""
        rows = conn.execute(
            "SELECT id,state,location FROM copies WHERE version_id=? ORDER BY id", (version_id,)
        ).fetchall()
        healthy = [r["id"] for r in rows if r["state"] == "healthy"]
        pending = [r["id"] for r in rows if r["state"] == "pending_retirement"]
        rebuilding = [r["id"] for r in rows if r["state"] == "rebuilding"]
        open_reports = conn.execute(
            "SELECT COUNT(*) FROM damage_reports WHERE version_id=? AND state='open'", (version_id,)
        ).fetchone()[0]
        protected = len(healthy) >= 2
        messages = []
        if protected:
            message = f"受保护：存在 {len(healthy)} 份健康副本（≥2）"
        else:
            if not rows:
                message = "缺少保障：该版本尚无任何副本，不存在冗余"
                messages.append("尚未登记任何副本")
            else:
                message = f"缺少保障：仅有 {len(healthy)} 份健康副本，冗余不足（需要至少 2 份）"
                messages.append("健康副本不足 2 份，无法容忍介质损坏")
            if pending:
                messages.append(f"{len(pending)} 份损坏副本处于待退役状态：{pending}")
            if rebuilding:
                messages.append(f"{len(rebuilding)} 份新副本正在重建/等待校验：{rebuilding}")
            if open_reports:
                messages.append(f"{open_reports} 份损坏清单尚未闭环")
        return {"state": "protected" if protected else "at_risk",
                "healthy_copies": healthy, "healthy_copy_count": len(healthy),
                "minimum_healthy_copies": 2,
                "pending_retirement_copies": pending, "rebuilding_copies": rebuilding,
                "open_damage_reports": open_reports, "message": message, "missing_safeguards": messages}

    def _refresh_version_state(self, conn, version_id: int) -> None:
        protected = self._protection(conn, version_id)["state"] == "protected"
        conn.execute(
            "UPDATE archive_versions SET state=? WHERE id=?",
            ("verified" if protected else "degraded", version_id),
        )

    def create_archive(self, user_id: str, name: str, retention_until: str, restricted: bool = True) -> dict:
        name = name.strip()
        if len(name) < 2:
            raise BusinessError("档案名称至少 2 字", 422, "invalid_name")
        try:
            deadline = date.fromisoformat(retention_until)
        except ValueError:
            raise BusinessError("retention_until 必须是 YYYY-MM-DD", 422, "invalid_retention")
        if deadline < date.today():
            raise BusinessError("保留期限不能早于今天", 422, "retention_in_past")
        with self.connect() as conn:
            user = self._user(conn, user_id, {"owner", "archivist"})
            try:
                cur = conn.execute(
                    "INSERT INTO archives(name,owner_id,retention_until,restricted,created_at) VALUES(?,?,?,?,?)",
                    (name, user_id, retention_until, int(bool(restricted)), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("档案名称已存在", 409, "archive_exists")
            archive_id = cur.lastrowid
            conn.execute(
                "INSERT INTO archive_members(archive_id,user_id,permission) VALUES(?,?,'write')", (archive_id, user_id)
            )
            self._audit(conn, archive_id, user_id, "archive.create", {"retention_until": retention_until, "restricted": restricted})
            return {"id": archive_id, "name": name, "retention_until": retention_until, "restricted": restricted}

    def grant(self, actor_id: str, archive_id: int, user_id: str, permission: str) -> dict:
        if permission not in {"read", "write"}:
            raise BusinessError("permission 必须是 read 或 write", 422, "invalid_permission")
        with self.connect() as conn:
            actor = self._user(conn, actor_id)
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
            if not archive:
                raise BusinessError("档案不存在", 404, "not_found")
            if archive["owner_id"] != actor_id:
                raise BusinessError("只有档案所有者可以授权", 403, "forbidden")
            self._user(conn, user_id)
            conn.execute(
                """INSERT INTO archive_members(archive_id,user_id,permission) VALUES(?,?,?)
                   ON CONFLICT(archive_id,user_id) DO UPDATE SET permission=excluded.permission""",
                (archive_id, user_id, permission),
            )
            self._audit(conn, archive_id, actor_id, "access.grant", {"user_id": user_id, "permission": permission})
            return {"archive_id": archive_id, "user_id": user_id, "permission": permission}

    def ingest_version(self, actor_id: str, archive_id: int, files: object) -> dict:
        manifest = verify_manifest(files)
        with self.connect() as conn:
            actor = self._user(conn, actor_id, {"owner", "archivist"})
            self._access(conn, archive_id, actor, require_write=True)
            try:
                conn.execute("BEGIN IMMEDIATE")
                version_no = conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM archive_versions WHERE archive_id=?", (archive_id,)
                ).fetchone()[0]
                cur = conn.execute(
                    "INSERT INTO archive_versions(archive_id,version,created_by,created_at) VALUES(?,?,?,?)",
                    (archive_id, version_no, actor_id, now()),
                )
                version_id = cur.lastrowid
                for item in manifest:
                    conn.execute(
                        "INSERT INTO archive_files(version_id,path,sha256,size,content) VALUES(?,?,?,?,?)",
                        (version_id, item["path"], item["sha256"], item["size"], item["content"]),
                    )
                self._refresh_version_state(conn, version_id)
                self._audit(
                    conn, archive_id, actor_id, "version.ingest",
                    {"version_id": version_id, "version": version_no, "files": len(manifest),
                     "manifest": [{"path": x["path"], "sha256": x["sha256"], "size": x["size"]} for x in manifest]},
                )
                return {"id": version_id, "archive_id": archive_id, "version": version_no, "file_count": len(manifest)}
            except Exception:
                conn.rollback()
                raise

    def add_copy(self, actor_id: str, version_id: int, location: str) -> dict:
        location = location.strip()
        if len(location) < 2:
            raise BusinessError("副本位置不能为空", 422, "invalid_location")
        with self.connect() as conn:
            actor = self._user(conn, actor_id, {"owner", "archivist"})
            version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise BusinessError("档案版本不存在", 404, "not_found")
            self._access(conn, version["archive_id"], actor, require_write=True)
            if conn.execute("SELECT 1 FROM retired_locations WHERE location=?", (location,)).fetchone():
                raise BusinessError("该位置已退役并列入黑名单，不能再登记副本", 422, "location_retired")
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "INSERT INTO copies(version_id,location,created_at,last_verified_at) VALUES(?,?,?,?)",
                    (version_id, location, now(), now()),
                )
                copy_id = cur.lastrowid
                conn.execute(
                    """INSERT INTO copy_files(copy_id,path,sha256,size,content)
                       SELECT ?,path,sha256,size,content FROM archive_files WHERE version_id=?""",
                    (copy_id, version_id),
                )
                self._refresh_version_state(conn, version_id)
                self._audit(conn, version["archive_id"], actor_id, "copy.create", {"copy_id": copy_id, "version_id": version_id, "location": location})
                return {"id": copy_id, "version_id": version_id, "location": location, "state": "healthy"}
            except sqlite3.IntegrityError:
                conn.rollback()
                raise BusinessError("该版本的副本位置已存在", 409, "copy_exists")
            except Exception:
                conn.rollback()
                raise

    def get_version(self, user_id: str, version_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id, {"owner", "archivist", "auditor"})
            version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise BusinessError("档案版本不存在", 404, "not_found")
            self._access(conn, version["archive_id"], user)
            files = conn.execute(
                "SELECT path,sha256,size FROM archive_files WHERE version_id=? ORDER BY path", (version_id,)
            ).fetchall()
            copies = conn.execute(
                "SELECT id,location,state,created_at,last_verified_at,replaces_copy_id FROM copies WHERE version_id=? ORDER BY id",
                (version_id,),
            ).fetchall()
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (version["archive_id"],)).fetchone()
            reports = conn.execute(
                """SELECT id,copy_id,found_at,corrupt_paths,state,resolved_at,replacement_copy_id
                   FROM damage_reports WHERE version_id=? ORDER BY id""",
                (version_id,),
            ).fetchall()
            return {
                "version": dict(version),
                "archive": dict(archive),
                "protection": self._protection(conn, version_id),
                "files": [dict(x) for x in files],
                "copies": [dict(x) for x in copies],
                "damage_reports": [dict(r) | {"corrupt_paths": json.loads(r["corrupt_paths"])} for r in reports],
            }

    def verify_copy(self, user_id: str, copy_id: int) -> dict:
        """巡检：只校验、记录损坏清单、将损坏副本转入待退役，绝不就地覆写原介质。"""
        with self.connect() as conn:
            user = self._user(conn, user_id, {"owner", "archivist", "auditor"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                copy = conn.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
                if not copy:
                    raise BusinessError("副本不存在", 404, "not_found")
                if copy["state"] == "retired":
                    raise BusinessError("副本已退役，不再参与巡检", 409, "copy_retired")
                if copy["state"] == "rebuilding":
                    raise BusinessError("新副本尚未完成校验，等待重建校验流程", 409, "copy_rebuilding")
                version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (copy["version_id"],)).fetchone()
                self._access(conn, version["archive_id"], user)
                ts = now()
                stored = conn.execute(
                    "SELECT path,sha256,size,content FROM copy_files WHERE copy_id=? ORDER BY path", (copy_id,)
                ).fetchall()
                corrupt_paths = [r["path"] for r in stored if hashlib.sha256(r["content"]).hexdigest() != r["sha256"] or len(r["content"]) != r["size"]]
                report_id = None
                if not corrupt_paths:
                    # 待退役副本即使当前哈希正常，也保留退役决定；健康副本照常刷新巡检时间。
                    if copy["state"] == "healthy":
                        conn.execute("UPDATE copies SET state='healthy',last_verified_at=? WHERE id=?", (ts, copy_id))
                    result_state = copy["state"]
                else:
                    conn.execute(
                        "UPDATE copies SET state='pending_retirement',last_verified_at=? WHERE id=?", (ts, copy_id)
                    )
                    result_state = "pending_retirement"
                    cur = conn.execute(
                        """INSERT INTO damage_reports(version_id,copy_id,found_at,corrupt_paths,state)
                           VALUES(?,?,?,?,'open')""",
                        (copy["version_id"], copy_id, ts, json.dumps(corrupt_paths, ensure_ascii=False)),
                    )
                    report_id = cur.lastrowid
                self._refresh_version_state(conn, copy["version_id"])
                self._audit(
                    conn, version["archive_id"], user_id, "copy.verify",
                    {"copy_id": copy_id, "state": result_state, "corrupt_paths": corrupt_paths,
                     "damage_report_id": report_id, "found_at": ts},
                )
                return {"copy_id": copy_id, "state": result_state, "corrupt_paths": corrupt_paths,
                        "damage_report_id": report_id, "found_at": ts if corrupt_paths else None,
                        "protection": self._protection(conn, copy["version_id"])}
            except Exception:
                conn.rollback()
                raise

    def register_replace_copy(self, actor_id: str, version_id: int, location: str, replaces_copy_id: int | None = None) -> dict:
        """管理员为待退役副本登记新的存储位置，新副本进入重建中，尚未接替。"""
        location = location.strip()
        if len(location) < 2:
            raise BusinessError("副本位置不能为空", 422, "invalid_location")
        with self.connect() as conn:
            actor = self._user(conn, actor_id, {"owner", "archivist"})
            version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise BusinessError("档案版本不存在", 404, "not_found")
            self._access(conn, version["archive_id"], actor, require_write=True)
            if conn.execute("SELECT 1 FROM retired_locations WHERE location=?", (location,)).fetchone():
                raise BusinessError("该位置已退役并列入黑名单，不能再登记副本", 422, "location_retired")
            try:
                conn.execute("BEGIN IMMEDIATE")
                pending = conn.execute(
                    "SELECT * FROM copies WHERE version_id=? AND state='pending_retirement' ORDER BY id", (version_id,)
                ).fetchall()
                if not pending:
                    raise BusinessError("该版本没有待退役副本，无需登记替换位置", 409, "no_pending_retirement")
                if replaces_copy_id is None:
                    target = pending[0]
                else:
                    target = next((r for r in pending if r["id"] == replaces_copy_id), None)
                    if target is None:
                        raise BusinessError("指定的副本不存在或不在待退役状态", 404, "pending_copy_not_found")
                donor = self._healthy_donor(conn, version_id, exclude=target["id"])
                if donor is None:
                    raise BusinessError("没有可用于重建的健康副本，替换无法进行", 409, "no_healthy_donor")
                cur = conn.execute(
                    """INSERT INTO copies(version_id,location,state,created_at,last_verified_at,replaces_copy_id)
                       VALUES(?,?,'rebuilding',?,NULL,?)""",
                    (version_id, location, now(), target["id"]),
                )
                new_copy_id = cur.lastrowid
                conn.execute(
                    """INSERT INTO copy_files(copy_id,path,sha256,size,content)
                       SELECT ?,path,sha256,size,content FROM copy_files WHERE copy_id=?""",
                    (new_copy_id, donor["id"]),
                )
                self._refresh_version_state(conn, version_id)
                self._audit(
                    conn, version["archive_id"], actor_id, "copy.replace_register",
                    {"new_copy_id": new_copy_id, "version_id": version_id, "new_location": location,
                     "replaces_copy_id": target["id"], "old_location": target["location"], "donor_copy_id": donor["id"]},
                )
                return {"id": new_copy_id, "version_id": version_id, "location": location, "state": "rebuilding",
                        "replaces_copy_id": target["id"]}
            except sqlite3.IntegrityError:
                conn.rollback()
                raise BusinessError("该版本的副本位置已存在", 409, "copy_exists")
            except Exception:
                conn.rollback()
                raise

    def rebuild_copy(self, actor_id: str, copy_id: int) -> dict:
        """重建（重试）：以健康副本重新同步新位置上的文件；同步后仍需完整校验才能接替。"""
        with self.connect() as conn:
            actor = self._user(conn, actor_id, {"owner", "archivist"})
            copy = conn.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
            if not copy:
                raise BusinessError("副本不存在", 404, "not_found")
            version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (copy["version_id"],)).fetchone()
            self._access(conn, version["archive_id"], actor, require_write=True)
            if copy["state"] != "rebuilding":
                raise BusinessError("只有重建中的新副本可以执行重建", 409, "copy_not_rebuilding")
            donor = self._healthy_donor(conn, copy["version_id"])
            if donor is None:
                raise BusinessError("没有可用于重建的健康副本，重建无法进行", 409, "no_healthy_donor")
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM copy_files WHERE copy_id=?", (copy_id,))
                conn.execute(
                    """INSERT INTO copy_files(copy_id,path,sha256,size,content)
                       SELECT ?,path,sha256,size,content FROM copy_files WHERE copy_id=?""",
                    (copy_id, donor["id"]),
                )
                conn.execute("UPDATE copies SET last_verified_at=NULL WHERE id=?", (copy_id,))
                self._audit(
                    conn, version["archive_id"], actor_id, "copy.rebuild",
                    {"copy_id": copy_id, "version_id": copy["version_id"], "donor_copy_id": donor["id"]},
                )
                return {"copy_id": copy_id, "state": "rebuilding", "donor_copy_id": donor["id"]}
            except Exception:
                conn.rollback()
                raise

    def complete_replacement(self, actor_id: str, copy_id: int) -> dict:
        """对新副本执行全量校验；全部文件通过后才标记健康、退役旧位置并闭环损坏清单。"""
        with self.connect() as conn:
            actor = self._user(conn, actor_id, {"owner", "archivist"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                copy = conn.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
                if not copy:
                    raise BusinessError("副本不存在", 404, "not_found")
                version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (copy["version_id"],)).fetchone()
                self._access(conn, version["archive_id"], actor, require_write=True)
                if copy["state"] not in ("rebuilding", "corrupt"):
                    raise BusinessError("只有重建中的新副本可以校验接替", 409, "copy_not_rebuilding")
                diff = self._copy_diff(conn, copy_id, copy["version_id"])
                ts = now()
                if diff["missing"] or diff["mismatched"] or diff["extra"]:
                    conn.execute("UPDATE copies SET state='rebuilding',last_verified_at=? WHERE id=?", (ts, copy_id))
                    self._audit(
                        conn, version["archive_id"], actor_id, "copy.replace_verify_failed",
                        {"copy_id": copy_id, "diff": diff, "found_at": ts},
                    )
                    return {"copy_id": copy_id, "passed": False, "state": "rebuilding", **diff,
                            "protection": self._protection(conn, copy["version_id"])}
                old = conn.execute("SELECT * FROM copies WHERE id=?", (copy["replaces_copy_id"],)).fetchone()
                if old is None:
                    raise BusinessError("新副本未登记待退役的原副本，不能执行接替", 409, "replacement_target_missing")
                conn.execute("UPDATE copies SET state='healthy',last_verified_at=? WHERE id=?", (ts, copy_id))
                conn.execute("UPDATE copies SET state='retired',last_verified_at=? WHERE id=?", (ts, old["id"]))
                conn.execute(
                    """INSERT OR IGNORE INTO retired_locations(location,copy_id,version_id,archive_id,retired_at)
                       VALUES(?,?,?,?,?)""",
                    (old["location"], old["id"], old["version_id"], version["archive_id"], ts),
                )
                conn.execute(
                    """UPDATE damage_reports SET state='resolved',resolved_at=?,replacement_copy_id=?
                       WHERE copy_id=? AND state='open'""",
                    (ts, copy_id, old["id"]),
                )
                self._refresh_version_state(conn, copy["version_id"])
                protection = self._protection(conn, copy["version_id"])
                self._audit(
                    conn, version["archive_id"], actor_id, "copy.replace_complete",
                    {"new_copy_id": copy_id, "new_location": copy["location"], "retired_copy_id": old["id"],
                     "retired_location": old["location"], "verified_at": ts},
                )
                return {"copy_id": copy_id, "passed": True, "state": "healthy",
                        "retired_copy_id": old["id"], "retired_location": old["location"],
                        "missing": [], "mismatched": [], "extra": [], "protection": protection}
            except Exception:
                conn.rollback()
                raise

    def simulate_corruption(self, user_id: str, copy_id: int, path: str) -> dict:
        """仅用于演示和测试，在受控环境中模拟底层介质损坏。"""
        with self.connect() as conn:
            user = self._user(conn, user_id, {"owner", "archivist"})
            copy = conn.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
            if not copy:
                raise BusinessError("副本不存在", 404, "not_found")
            if copy["state"] == "retired":
                raise BusinessError("副本已退役，不能再模拟损坏", 409, "copy_retired")
            version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (copy["version_id"],)).fetchone()
            self._access(conn, version["archive_id"], user, require_write=True)
            row = conn.execute("SELECT content FROM copy_files WHERE copy_id=? AND path=?", (copy_id, path)).fetchone()
            if not row:
                raise BusinessError("副本文件不存在", 404, "not_found")
            damaged = bytes([row["content"][0] ^ 0xFF]) + row["content"][1:] if row["content"] else b"corrupt"
            conn.execute("UPDATE copy_files SET content=? WHERE copy_id=? AND path=?", (damaged, copy_id, path))
            conn.execute("UPDATE copies SET state='corrupt' WHERE id=?", (copy_id,))
            self._audit(conn, version["archive_id"], user_id, "copy.simulate_corruption", {"copy_id": copy_id, "path": path})
            return {"copy_id": copy_id, "path": path, "state": "corrupt"}

    def migrate(self, actor_id: str, version_id: int, source_path: str, target_path: str, target_format: str, content_b64: str) -> dict:
        with self.connect() as conn:
            actor = self._user(conn, actor_id, {"owner", "archivist"})
            source_version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (version_id,)).fetchone()
            if not source_version:
                raise BusinessError("源档案版本不存在", 404, "not_found")
            self._access(conn, source_version["archive_id"], actor, require_write=True)
            source = conn.execute(
                "SELECT * FROM archive_files WHERE version_id=? AND path=?", (version_id, source_path)
            ).fetchone()
            if not source:
                raise BusinessError("源文件不存在", 404, "source_not_found")
            converted = verify_manifest([{"path": target_path, "content_b64": content_b64}])[0]
            try:
                conn.execute("BEGIN IMMEDIATE")
                version_no = conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM archive_versions WHERE archive_id=?", (source_version["archive_id"],)
                ).fetchone()[0]
                cur = conn.execute(
                    "INSERT INTO archive_versions(archive_id,version,created_by,created_at) VALUES(?,?,?,?)",
                    (source_version["archive_id"], version_no, actor_id, now()),
                )
                target_version_id = cur.lastrowid
                conn.execute(
                    """INSERT INTO archive_files(version_id,path,sha256,size,content)
                       SELECT ?,path,sha256,size,content FROM archive_files
                       WHERE version_id=? AND path<>?""",
                    (target_version_id, version_id, source_path),
                )
                conn.execute(
                    "INSERT INTO archive_files(version_id,path,sha256,size,content) VALUES(?,?,?,?,?)",
                    (target_version_id, converted["path"], converted["sha256"], converted["size"], converted["content"]),
                )
                conn.execute(
                    "INSERT INTO migrations(source_version_id,target_version_id,source_path,target_path,target_format,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (version_id, target_version_id, source_path, converted["path"], target_format.strip(), actor_id, now()),
                )
                self._refresh_version_state(conn, target_version_id)
                self._audit(
                    conn, source_version["archive_id"], actor_id, "format.migrate",
                    {"source_version_id": version_id, "target_version_id": target_version_id,
                     "source_path": source_path, "target_path": converted["path"], "target_format": target_format.strip()},
                )
                return {"id": target_version_id, "version": version_no, "source_version_id": version_id, "target_path": converted["path"]}
            except Exception:
                conn.rollback()
                raise

    def archive_status(self, user_id: str, archive_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id, {"owner", "archivist", "auditor"})
            self._access(conn, archive_id, user)
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
            versions = conn.execute("SELECT id,version,state,created_at FROM archive_versions WHERE archive_id=? ORDER BY version", (archive_id,)).fetchall()
            deadline = date.fromisoformat(archive["retention_until"])
            return {
                "archive": dict(archive),
                "days_remaining": (deadline - date.today()).days,
                "versions": [dict(v)
                             | {"file_count": conn.execute("SELECT COUNT(*) FROM archive_files WHERE version_id=?", (v["id"],)).fetchone()[0],
                                "copy_count": conn.execute("SELECT COUNT(*) FROM copies WHERE version_id=?", (v["id"],)).fetchone()[0],
                                "open_damage_reports": conn.execute(
                                    "SELECT COUNT(*) FROM damage_reports WHERE version_id=? AND state='open'", (v["id"],)).fetchone()[0],
                                "protection": self._protection(conn, v["id"])}
                             for v in versions],
                "audit": [dict(r) | {"detail": json.loads(r["detail"])} for r in conn.execute("SELECT * FROM audit_log WHERE archive_id=? ORDER BY id", (archive_id,)).fetchall()],
            }


class Handler(BaseHTTPRequestHandler):
    server_version = "Preservation/1.0"

    def _store(self):
        return self.server.store  # type: ignore[attr-defined]

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        if method == "GET" and path == "/":
            body = (BASE_DIR / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "GET" and path == "/health":
            return self._send(200, {"ok": True})
        store = self._store()
        if parts == ["api", "archives"] and method == "POST":
            d = self._body()
            return self._send(201, store.create_archive(user, d.get("name", ""), d.get("retention_until", ""), bool(d.get("restricted", True))))
        if len(parts) == 4 and parts[:2] == ["api", "archives"] and method == "POST":
            archive_id = int(parts[2])
            if parts[3] == "versions":
                d = self._body()
                return self._send(201, store.ingest_version(user, archive_id, d.get("files")))
            if parts[3] == "members":
                d = self._body()
                return self._send(201, store.grant(user, archive_id, d.get("user_id", ""), d.get("permission", "")))
        if len(parts) == 4 and parts[:2] == ["api", "archives"] and parts[3] == "status" and method == "GET":
            return self._send(200, store.archive_status(user, int(parts[2])))
        if len(parts) == 3 and parts[:2] == ["api", "versions"] and method == "GET":
            return self._send(200, store.get_version(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "copies" and method == "POST":
            d = self._body()
            return self._send(201, store.add_copy(user, int(parts[2]), d.get("location", "")))
        if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "replacements" and method == "POST":
            d = self._body()
            rid = d.get("replaces_copy_id")
            return self._send(201, store.register_replace_copy(user, int(parts[2]), d.get("location", ""), int(rid) if rid is not None else None))
        if len(parts) == 4 and parts[:2] == ["api", "copies"] and parts[3] == "rebuild" and method == "POST":
            return self._send(200, store.rebuild_copy(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "copies"] and parts[3] == "complete-replacement" and method == "POST":
            return self._send(200, store.complete_replacement(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "migrate" and method == "POST":
            d = self._body()
            return self._send(201, store.migrate(user, int(parts[2]), d.get("source_path", ""), d.get("target_path", ""), d.get("target_format", ""), d.get("content_b64", "")))
        if len(parts) == 4 and parts[:2] == ["api", "copies"] and parts[3] == "verify" and method == "POST":
            return self._send(200, store.verify_copy(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "copies"] and parts[3] == "simulate-corruption" and method == "POST":
            d = self._body()
            return self._send(200, store.simulate_corruption(user, int(parts[2]), d.get("path", "")))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method: str) -> None:
        try:
            self._dispatch(method)
        except BusinessError as exc:
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (ValueError, TypeError):
            self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc:
            self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_DELETE(self): self._handle("DELETE")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class PreservationServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store):
        self.store = store
        super().__init__(address, Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="数字档案长期保存服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    store = PreservationStore(args.db)
    store.init_schema()
    if args.seed:
        store.seed()
    if args.init or args.seed:
        print(f"数据库已初始化: {args.db}")
        return
    print(f"数字档案服务运行于 http://127.0.0.1:{args.port}")
    server = PreservationServer(("127.0.0.1", args.port), store)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
