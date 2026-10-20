"""版本化 schema 迁移（PRD §11.1「所有数据库 schema 使用 migration」）。

规则（PHASE1_TECH_PLAN §5）：
  * 迁移文件 `NNN_name.sql`，编号即目标 `PRAGMA user_version`。
  * 每个文件在**单个事务**内应用（SQLite 的 DDL 可回滚），失败则整体回滚、库保持原状。
  * 应用前自动在线备份到 `data/backups/pre_schema<N>_<时间戳>.db`（`VACUUM INTO`，不阻塞读）。
  * 已应用迁移的文件内容被事后修改时**拒绝启动**（checksum 不匹配），避免 schema 漂移。
  * **只前进不后退**：降级靠备份恢复，流程见 README。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..errors import MigrationError, SchemaVersionMismatch
from .connection import SCHEMA_PRAGMA_KEY, Database, utc_now

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_RE = re.compile(r"^(\d{3,})_([A-Za-z0-9_\-]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str

    @property
    def label(self) -> str:
        return f"{self.version:03d}_{self.name}"


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """读取迁移目录，返回按版本升序排列的迁移列表。"""
    directory = Path(directory) if directory is not None else MIGRATIONS_DIR
    if not directory.is_dir():
        raise MigrationError(f"迁移目录不存在：{directory}")

    found: list[Migration] = []
    seen: dict[int, str] = {}
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"迁移文件名不符合规范 NNN_name.sql：{path.name}",
                detail={"kind": "bad_migration_filename", "path": str(path)},
            )
        version = int(match.group(1))
        if version in seen:
            raise MigrationError(
                f"迁移版本号重复：{version:03d}（{seen[version]} 与 {path.name}）"
            )
        seen[version] = path.name
        raw = path.read_bytes()
        found.append(
            Migration(
                version=version,
                name=match.group(2),
                path=path,
                sql=raw.decode("utf-8"),
                checksum=hashlib.sha256(raw).hexdigest(),
            )
        )

    if not found:
        raise MigrationError(f"迁移目录为空：{directory}")

    found.sort(key=lambda m: m.version)
    if found[0].version != 1:
        raise MigrationError("迁移必须从 001 开始，缺少 001 基础迁移")
    return found


def target_version(directory: Path | None = None) -> int:
    """代码期望的最新 schema 版本。"""
    return discover_migrations(directory)[-1].version


def applied_migrations(db: Database) -> dict[int, dict[str, str]]:
    """已应用的迁移记录 {version: {name, checksum, applied_at}}；库未初始化时返回空。"""
    if not db.exists():
        return {}
    with db.read() as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if table is None:
            return {}
        rows = conn.execute(
            "SELECT version, name, checksum, applied_at FROM schema_migrations"
        ).fetchall()
    return {
        int(r["version"]): {
            "name": r["name"],
            "checksum": r["checksum"],
            "applied_at": r["applied_at"],
        }
        for r in rows
    }


#: 迁移器自己拥有的账本表。它**不属于任何迁移**，因此必须在应用迁移前就存在，
#: 否则「应用迁移 -> 记账」会因账本不存在而失败，且自建库与升级库行为不一致。
_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,
    checksum    TEXT    NOT NULL,
    app_version TEXT
)
"""


def _ensure_ledger(db: Database) -> None:
    with db.write() as conn:
        conn.execute(_BOOTSTRAP_SQL)


def verify_checksums(db: Database, directory: Path | None = None) -> None:
    """校验已应用迁移与磁盘文件一致。

    顺序很重要：**先查缺失、再查篡改**。若某个已应用的迁移文件整体消失，
    报「文件缺失」比报「checksum 不匹配」更准确、更可操作。
    """
    migrations = {m.version: m for m in discover_migrations(directory)}
    records = applied_migrations(db)

    missing = sorted(v for v in records if v not in migrations)
    if missing:
        labels = "、".join(f"{v:03d}" for v in missing)
        raise MigrationError(
            f"数据库已应用迁移 {labels}，但对应迁移文件缺失。"
            "请恢复对应迁移文件，或从备份恢复数据库。",
            detail={"kind": "missing_migration_file", "versions": missing},
        )

    # 按版本升序检查，保证报错稳定可复现；一次报告全部被篡改的迁移，便于一次修完。
    mismatched: list[dict[str, object]] = []
    for version in sorted(records):
        record = records[version]
        migration = migrations[version]
        if migration.checksum != record["checksum"]:
            mismatched.append(
                {
                    "label": migration.label,
                    "expected": record["checksum"],
                    "actual": migration.checksum,
                    "path": str(migration.path),
                }
            )
    if mismatched:
        labels = "、".join(str(item["label"]) for item in mismatched)
        raise MigrationError(
            f"迁移 {labels} 的文件内容与已应用记录不一致（checksum 不匹配）。"
            "已应用的迁移不得修改；如需变更请新增一个迁移文件。",
            detail={"kind": "checksum_mismatch", "migrations": mismatched},
        )


def pending_migrations(db: Database, directory: Path | None = None) -> list[Migration]:
    done = set(applied_migrations(db))
    return [m for m in discover_migrations(directory) if m.version not in done]


def check_schema_compatible(db: Database, directory: Path | None = None) -> None:
    """库版本高于代码版本 → 说明库来自更新的 PhotoOS，拒绝打开（迁移包场景）。"""
    current = db.schema_version()
    expected = target_version(directory)
    if current > expected:
        raise SchemaVersionMismatch(
            f"数据库 schema 版本为 {current}，高于当前程序支持的 {expected}。"
            "该库可能来自更新版本的 PhotoOS，请升级程序后再打开。",
            detail={"db_version": current, "supported": expected},
        )


_TXN_KEYWORDS = ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE")


def _strip_sql_noise(sql: str) -> str:
    """去掉 SQL 注释与字符串字面量，保留其余文本（含换行，便于定位）。

    必要性来自一次真实误报：006 迁移里的 `CREATE TRIGGER` **必须**含
    `BEGIN ... END;`（SQLite 语法要求，实测 `BEGIN`-less 的触发器是语法错误），
    而原实现是"逐行看是否有以 BEGIN 开头"，把触发器体当成了事务控制语句。
    同类合法用法还有 `CASE ... BEGIN`（SQLite 的 CASE 允许 BEGIN 形式）。

    因此判据必须是"**独立语句**以事务关键字开头"，而不是"文本里出现过该词"。
    为做到这一点，先掩掉两类噪音：
      * `--` 行注释 与 `/* */` 块注释（注释里写 BEGIN 是说明，不是语句）
      * `'...'` / `"..."` / `[...]` / `` `...` `` 字符串与标识符（含转义 `''`）
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # 行注释：保留换行，避免把两行粘成一条语句
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            if j == -1:
                break
            out.append("\n")
            i = j + 1
            continue
        # 块注释
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j == -1:
                break
            out.append("\n" * sql.count("\n", i, j))
            i = j + 2
            continue
        # 字符串 / 带引号标识符
        if ch in ("'", '"', "`", "["):
            closer = "]" if ch == "[" else ch
            out.append(" ")
            i += 1
            while i < n:
                if sql[i] == closer:
                    # SQL 的 '' 与 "" 转义：连续两个引号是字面量，不是结束
                    if closer != "]" and sql.startswith(closer * 2, i):
                        i += 2
                        continue
                    i += 1
                    break
                if sql[i] == "\n":
                    out.append("\n")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _explicit_transaction(sql: str) -> str | None:
    """若脚本里存在**独立的事务控制语句**，返回该关键字；否则返回 None。

    只认"语句开头"出现的关键字：用 `;` 切分后逐条检查首个词。
    这样 `CREATE TRIGGER ... BEGIN ... END;` 与 `CASE ... BEGIN ... END` 都能通过，
    而 `BEGIN;` / `COMMIT;` / `SAVEPOINT x;` 仍会被拒。
    """
    cleaned = _strip_sql_noise(sql)
    for statement in cleaned.split(";"):
        head = statement.strip().split(None, 1)
        if head and head[0].upper() in _TXN_KEYWORDS:
            return head[0].upper()
    return None


def _assert_no_explicit_transaction(migration: Migration) -> None:
    """迁移文件内不得自己写事务控制语句。

    `sqlite3.executescript()` 会先隐式 COMMIT 再执行脚本，因此原子性必须由
    脚本自身携带的 `BEGIN` / `COMMIT` 保证；若文件里再出现 BEGIN/COMMIT，
    就会变成嵌套事务（SQLite 不支持），错误信息也会难以理解。这里显式拒绝。

    判据是"独立语句以事务关键字开头"，不是"文本里出现过该词"——
    后者会把 `CREATE TRIGGER`（语法上必须含 BEGIN）误判为事务语句。
    """
    keyword = _explicit_transaction(migration.sql)
    if keyword:
        raise MigrationError(
            f"迁移 {migration.label} 内不得出现事务控制语句（{keyword}）；"
            "迁移的原子性由迁移器负责。",
            detail={"kind": "explicit_transaction", "path": str(migration.path)},
        )


def _run_migration_sql(conn: sqlite3.Connection, migration: Migration) -> None:
    """在**单个事务**内执行整个迁移文件。

    实现要点：必须把 `BEGIN`/`COMMIT` 写进脚本文本（而不是先用 conn.execute 开事务），
    因为 `executescript()` 会隐式提交调用前的待处理事务 —— 那样每条语句都会各自提交，
    迁移中途失败就会留下半截 schema。脚本执行完事务仍处于打开状态，
    调用方随后写入账本并统一提交，从而保证「schema 变更 + 账本记录」原子一致。
    """
    conn.executescript(f"BEGIN;\n{migration.sql}\nCOMMIT;")


@dataclass
class ApplyResult:
    applied: list[str]
    backup_path: Path | None
    from_version: int
    to_version: int


def apply_migrations(
    db: Database,
    *,
    directory: Path | None = None,
    backup_dir: Path | None = None,
    make_backup: bool = True,
    app_version: str = "0.2.0",
) -> ApplyResult:
    """把数据库迁移到最新版本。返回实际应用的迁移与备份路径。"""
    migrations = discover_migrations(directory)
    expected = migrations[-1].version

    if db.exists():
        verify_checksums(db, directory)
        check_schema_compatible(db, directory)
        from_version = db.schema_version()
    else:
        db.path.parent.mkdir(parents=True, exist_ok=True)
        from_version = 0

    todo = [m for m in migrations if m.version > from_version]
    if not todo:
        return ApplyResult(applied=[], backup_path=None, from_version=from_version, to_version=from_version)

    # 账本表必须先于任何迁移存在（自建库与升级库行为一致）
    _ensure_ledger(db)

    backup_path: Path | None = None
    if make_backup and from_version > 0 and backup_dir is not None:
        stamp = utc_now().replace(":", "").replace("+", "_").replace("-", "")
        backup_path = Path(backup_dir) / f"pre_schema{expected:03d}_{stamp}.db"
        try:
            db.vacuum_into(backup_path)
        except Exception as exc:  # noqa: BLE001 - 备份失败必须阻止迁移
            raise MigrationError(f"迁移前备份失败，已中止迁移：{exc}") from exc

    applied: list[str] = []
    for migration in todo:
        _assert_no_explicit_transaction(migration)
        try:
            # 迁移 SQL 与其账本记录必须同一事务：要么都成功，要么都回滚。
            with db.write() as conn:
                _run_migration_sql(conn, migration)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at, checksum, app_version)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        migration.version,
                        migration.name,
                        utc_now(),
                        migration.checksum,
                        app_version,
                    ),
                )
                conn.execute(f"PRAGMA {SCHEMA_PRAGMA_KEY} = {migration.version}")
        except Exception as exc:
            raise MigrationError(
                f"应用迁移 {migration.label} 失败，数据库已回滚到版本 {from_version}：{exc}",
                detail={"kind": "apply_failed", "migration": migration.label, "path": str(migration.path)},
            ) from exc
        applied.append(migration.label)

    final = db.schema_version()
    if final != expected:
        raise MigrationError(
            f"迁移后 schema 版本异常：期望 {expected}，实际 {final}", detail={"applied": applied}
        )
    return ApplyResult(applied=applied, backup_path=backup_path, from_version=from_version, to_version=final)
