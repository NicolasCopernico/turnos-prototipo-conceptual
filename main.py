"""Backend del Sistema de Turnos (Fase 2): FastAPI + SQLite.

Fuente de verdad: spec.md v0.2.0 (sección 4 modelo de datos, sección 7 errores,
sección 11 contrato REST) y plan.md v0.2.0 (arquitectura de 3 capas).

Levantar el servidor (desde la carpeta de este archivo):

    uvicorn main:app --reload --port 8000

Swagger UI: http://localhost:8000/docs

La API no expone usuarios ni servicios (spec D-07), así que el arranque carga
datos iniciales con identificadores fijos. Ver las constantes ``*_ID`` más abajo.
"""

import logging
import os
import re
import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Final
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict

logger = logging.getLogger("turnos")

# --------------------------------------------------------------------------- #
# Configuración (spec RN-01, RN-04 y §1.4)
# --------------------------------------------------------------------------- #

CANCELLATION_WINDOW_HOURS: Final[float] = float(os.environ.get("CANCELLATION_WINDOW_HOURS", "2"))
MIN_LEAD_TIME: Final[timedelta] = timedelta(hours=1)
SLOT_GRID_MINUTES: Final[int] = 15
BOOKING_HORIZON: Final[timedelta] = timedelta(days=60)
OPERATION_TZ: Final[ZoneInfo] = ZoneInfo(os.environ.get("OPERATION_TZ", "America/Argentina/Buenos_Aires"))

TIME_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"  # ISO 8601 UTC de ancho fijo: ordena igual como texto

EMAIL_RE: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE: Final[re.Pattern[str]] = re.compile(r"^\+[1-9]\d{7,14}$")  # E.164


def db_path() -> Path:
    """Ruta de la base. Se lee en cada uso para poder apuntar a otra en las pruebas."""
    return Path(os.environ.get("TURNOS_DB", str(Path(__file__).with_name("turnos.db"))))


def now_utc() -> datetime:
    """Reloj del servidor en UTC (plan §1.2: el cliente no controla "ahora")."""
    return datetime.now(timezone.utc)


def fmt_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


def parse_utc(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Errores de dominio (spec §7 y §11.1)
# --------------------------------------------------------------------------- #

ERROR_CATALOG: Final[dict[str, tuple[int, str]]] = {
    "SLOT_UNAVAILABLE": (409, "El horario ya no está disponible."),
    "CANCELLATION_WINDOW_EXPIRED": (409, "Ya no se puede cancelar: faltan menos de 2 horas para el turno."),
    "INVALID_CONTACT": (422, "Los datos de contacto o el motivo son inválidos o faltan."),
    "EMAIL_NOT_VERIFIED": (403, "El email del cliente todavía no está verificado."),
    "INVALID_TIME_RANGE": (422, "El horario solicitado no es válido."),
    "INVALID_STATE_TRANSITION": (409, "El turno no admite esta operación en su estado actual."),
    "FORBIDDEN": (403, "No tenés permiso para realizar esta operación."),
    "NOT_FOUND": (404, "El recurso solicitado no existe."),
    "PERSISTENCE_ERROR": (500, "No se pudo guardar la operación. No se realizó ningún cambio."),
}


class DomainError(Exception):
    def __init__(self, codigo: str, mensaje: str | None = None) -> None:
        status, default_message = ERROR_CATALOG[codigo]
        super().__init__(codigo)
        self.status = status
        self.codigo = codigo
        self.mensaje = mensaje or default_message


# --------------------------------------------------------------------------- #
# Capa 3: esquema SQLite. Solo las 6 entidades y los atributos de spec §4.
# --------------------------------------------------------------------------- #

SCHEMA: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS usuario (
        id               TEXT PRIMARY KEY,
        email            TEXT NOT NULL UNIQUE,
        email_verificado INTEGER NOT NULL DEFAULT 0 CHECK (email_verificado IN (0, 1)),
        password_hash    TEXT NOT NULL,
        nombre_completo  TEXT NOT NULL CHECK (length(nombre_completo) BETWEEN 2 AND 100),
        telefono         TEXT NOT NULL,
        rol              TEXT NOT NULL CHECK (rol IN ('CLIENTE', 'PROFESIONAL', 'ADMIN')),
        activo           INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1)),
        creado_en        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS servicio (
        id                TEXT PRIMARY KEY,
        profesional_id    TEXT NOT NULL REFERENCES usuario (id),
        nombre            TEXT NOT NULL,
        descripcion       TEXT,
        duracion_min      INTEGER NOT NULL CHECK (duracion_min BETWEEN 15 AND 480 AND duracion_min % 15 = 0),
        precio_referencia NUMERIC NOT NULL CHECK (precio_referencia >= 0),
        activo            INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0, 1))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS turno (
        id                 TEXT PRIMARY KEY,
        cliente_id         TEXT NOT NULL REFERENCES usuario (id),
        profesional_id     TEXT NOT NULL REFERENCES usuario (id),
        servicio_id        TEXT NOT NULL REFERENCES servicio (id),
        inicio             TEXT NOT NULL,
        fin                TEXT NOT NULL,
        estado             TEXT NOT NULL CHECK (estado IN ('CONFIRMADO', 'CANCELADO', 'COMPLETADO', 'AUSENTE')),
        motivo_cancelacion TEXT,
        cancelado_por_id   TEXT REFERENCES usuario (id),
        creado_en          TEXT NOT NULL,
        actualizado_en     TEXT NOT NULL,
        CHECK (fin > inicio),
        CHECK (cliente_id <> profesional_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS horario_laboral (
        id             TEXT PRIMARY KEY,
        profesional_id TEXT NOT NULL REFERENCES usuario (id),
        dia_semana     INTEGER NOT NULL CHECK (dia_semana BETWEEN 0 AND 6),
        hora_inicio    TEXT NOT NULL,
        hora_fin       TEXT NOT NULL,
        CHECK (hora_fin > hora_inicio)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bloqueo (
        id             TEXT PRIMARY KEY,
        profesional_id TEXT NOT NULL REFERENCES usuario (id),
        inicio         TEXT NOT NULL,
        fin            TEXT NOT NULL,
        motivo         TEXT,
        CHECK (fin > inicio)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auditoria_evento (
        id          TEXT PRIMARY KEY,
        entidad     TEXT NOT NULL,
        entidad_id  TEXT NOT NULL,
        accion      TEXT NOT NULL,
        actor_id    TEXT NOT NULL REFERENCES usuario (id),
        rol         TEXT NOT NULL,
        ocurrido_en TEXT NOT NULL,
        detalle     TEXT
    )
    """,
    # INV-02 / RN-02 reforzada en la base: SQLite no tiene EXCLUDE USING gist, así que
    # se combina BEGIN IMMEDIATE (serializa escritores) con estos triggers. Intervalos [inicio, fin).
    """
    CREATE TRIGGER IF NOT EXISTS turno_sin_solapamiento_ins
    BEFORE INSERT ON turno
    WHEN NEW.estado = 'CONFIRMADO'
    BEGIN
        SELECT RAISE(ABORT, 'SLOT_UNAVAILABLE')
        WHERE EXISTS (
            SELECT 1 FROM turno t
            WHERE t.profesional_id = NEW.profesional_id AND t.estado = 'CONFIRMADO'
              AND t.id <> NEW.id AND t.inicio < NEW.fin AND NEW.inicio < t.fin
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS turno_sin_solapamiento_upd
    BEFORE UPDATE OF estado, inicio, fin, profesional_id ON turno
    WHEN NEW.estado = 'CONFIRMADO'
    BEGIN
        SELECT RAISE(ABORT, 'SLOT_UNAVAILABLE')
        WHERE EXISTS (
            SELECT 1 FROM turno t
            WHERE t.profesional_id = NEW.profesional_id AND t.estado = 'CONFIRMADO'
              AND t.id <> NEW.id AND t.inicio < NEW.fin AND NEW.inicio < t.fin
        );
    END
    """,
    # Spec §4.3: Turno.profesional_id debe coincidir con Servicio.profesional_id.
    """
    CREATE TRIGGER IF NOT EXISTS turno_profesional_coherente
    BEFORE INSERT ON turno
    BEGIN
        SELECT RAISE(ABORT, 'PROFESIONAL_INCOHERENTE')
        WHERE NOT EXISTS (
            SELECT 1 FROM servicio s WHERE s.id = NEW.servicio_id AND s.profesional_id = NEW.profesional_id
        );
    END
    """,
    # RN-06: los estados terminales no cambian y los turnos no se borran.
    """
    CREATE TRIGGER IF NOT EXISTS turno_estado_terminal
    BEFORE UPDATE ON turno
    WHEN OLD.estado <> 'CONFIRMADO'
    BEGIN
        SELECT RAISE(ABORT, 'INVALID_STATE_TRANSITION');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS turno_sin_borrado
    BEFORE DELETE ON turno
    BEGIN
        SELECT RAISE(ABORT, 'TURNO_NO_ELIMINABLE');
    END
    """,
    # RN-08: el log de auditoría es inmutable.
    """
    CREATE TRIGGER IF NOT EXISTS auditoria_sin_update
    BEFORE UPDATE ON auditoria_evento
    BEGIN
        SELECT RAISE(ABORT, 'AUDITORIA_INMUTABLE');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS auditoria_sin_delete
    BEFORE DELETE ON auditoria_evento
    BEGIN
        SELECT RAISE(ABORT, 'AUDITORIA_INMUTABLE');
    END
    """,
)

# Datos iniciales con identificadores fijos, para poder probar desde Swagger.
ADMIN_ID: Final[str] = "aaaaaaaa-0000-4000-8000-000000000001"
PROFESIONAL_ANA_ID: Final[str] = "bbbbbbbb-0000-4000-8000-000000000001"
PROFESIONAL_BRUNO_ID: Final[str] = "bbbbbbbb-0000-4000-8000-000000000002"
CLIENTE_LUCIA_ID: Final[str] = "cccccccc-0000-4000-8000-000000000001"  # email verificado
CLIENTE_MARTIN_ID: Final[str] = "cccccccc-0000-4000-8000-000000000002"  # email SIN verificar
SERVICIO_CONSULTA_ID: Final[str] = "dddddddd-0000-4000-8000-000000000001"  # Ana, 30 min
SERVICIO_CONTROL_ID: Final[str] = "dddddddd-0000-4000-8000-000000000002"  # Ana, 60 min
SERVICIO_CORTE_ID: Final[str] = "dddddddd-0000-4000-8000-000000000003"  # Bruno, 45 min
BLOQUEO_BRUNO_ID: Final[str] = "eeeeeeee-0000-4000-8000-000000000001"

NO_LOGIN_HASH: Final[str] = "!"  # marcador que nunca coincide con un hash: sin login en la Fase 2 (spec D-06)
SEED_NAMESPACE: Final[uuid.UUID] = uuid.UUID("5eed5eed-0000-4000-8000-000000000000")


def seed_db(conn: sqlite3.Connection, now: datetime) -> None:
    """Carga idempotente (INSERT OR IGNORE) de las entidades que la API no expone."""
    created = fmt_utc(now)
    users = (
        (ADMIN_ID, "admin@turnos.test", 1, "Administración General", "+5491100000001", "ADMIN"),
        (PROFESIONAL_ANA_ID, "ana.perez@turnos.test", 1, "Ana Pérez", "+5491100000002", "PROFESIONAL"),
        (PROFESIONAL_BRUNO_ID, "bruno.gomez@turnos.test", 1, "Bruno Gómez", "+5491100000003", "PROFESIONAL"),
        (CLIENTE_LUCIA_ID, "lucia.fernandez@turnos.test", 1, "Lucía Fernández", "+5491100000004", "CLIENTE"),
        (CLIENTE_MARTIN_ID, "martin.rios@turnos.test", 0, "Martín Ríos", "+5491100000005", "CLIENTE"),
    )
    for user_id, email, verified, name, phone, role in users:
        conn.execute(
            "INSERT OR IGNORE INTO usuario (id, email, email_verificado, password_hash, nombre_completo,"
            " telefono, rol, activo, creado_en) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (user_id, email, verified, NO_LOGIN_HASH, name, phone, role, created),
        )
    services = (
        (SERVICIO_CONSULTA_ID, PROFESIONAL_ANA_ID, "Consulta general", "Consulta de 30 minutos", 30, 15000),
        (SERVICIO_CONTROL_ID, PROFESIONAL_ANA_ID, "Control completo", "Control de 60 minutos", 60, 28000),
        (SERVICIO_CORTE_ID, PROFESIONAL_BRUNO_ID, "Corte y peinado", "Servicio de 45 minutos", 45, 12000),
    )
    for service_id, prof_id, name, description, minutes, price in services:
        conn.execute(
            "INSERT OR IGNORE INTO servicio (id, profesional_id, nombre, descripcion, duracion_min,"
            " precio_referencia, activo) VALUES (?, ?, ?, ?, ?, ?, 1)",
            (service_id, prof_id, name, description, minutes, price),
        )
    for prof_id in (PROFESIONAL_ANA_ID, PROFESIONAL_BRUNO_ID):
        for weekday in range(5):  # 0 = lunes ... 4 = viernes (convención de ``datetime.weekday``)
            conn.execute(
                "INSERT OR IGNORE INTO horario_laboral (id, profesional_id, dia_semana, hora_inicio, hora_fin)"
                " VALUES (?, ?, ?, '09:00', '18:00')",
                (str(uuid.uuid5(SEED_NAMESPACE, f"horario-{prof_id}-{weekday}")), prof_id, weekday),
            )
    block_day = (now.astimezone(OPERATION_TZ) + timedelta(days=7)).replace(hour=12, minute=0, second=0, microsecond=0)
    conn.execute(
        "INSERT OR IGNORE INTO bloqueo (id, profesional_id, inicio, fin, motivo) VALUES (?, ?, ?, ?, 'Almuerzo')",
        (BLOQUEO_BRUNO_ID, PROFESIONAL_BRUNO_ID, fmt_utc(block_day), fmt_utc(block_day + timedelta(hours=1))),
    )


# --------------------------------------------------------------------------- #
# Conexión y transacciones (INV-03)
# --------------------------------------------------------------------------- #


def connect() -> sqlite3.Connection:
    # isolation_level=None: las transacciones se controlan a mano con BEGIN IMMEDIATE / COMMIT.
    conn = sqlite3.connect(db_path(), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(get_db)]


def _translate(exc: sqlite3.Error) -> DomainError:
    message = str(exc)
    if isinstance(exc, sqlite3.IntegrityError) and message in ("SLOT_UNAVAILABLE", "INVALID_STATE_TRANSITION"):
        return DomainError(message)
    logger.error("Falla de persistencia: %s", message)  # el detalle va al log, nunca a la respuesta
    return DomainError("PERSISTENCE_ERROR")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Transacción de todo o nada. Responder éxito solo es válido después de salir sin error (INV-03)."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise _translate(exc) from exc
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def init_db() -> None:
    """Crea el esquema y los datos iniciales si faltan. Idempotente."""
    conn = connect()
    try:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error as exc:
            raise DomainError("PERSISTENCE_ERROR") from exc
        with transaction(conn):
            for statement in SCHEMA:
                conn.execute(statement)
            seed_db(conn, now_utc())
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Modelos de entrada y salida (spec §11.2, §11.4, §11.5)
# --------------------------------------------------------------------------- #


class Estado(str, Enum):
    CONFIRMADO = "CONFIRMADO"
    CANCELADO = "CANCELADO"
    COMPLETADO = "COMPLETADO"
    AUSENTE = "AUSENTE"


class TurnoCrear(BaseModel):
    """Cuerpo de POST /api/turnos. Rechaza ``fin``, ``profesional_id``, ``estado`` e ``id`` (spec §11.4)."""

    model_config = ConfigDict(extra="forbid")

    cliente_id: uuid.UUID
    servicio_id: uuid.UUID
    inicio: AwareDatetime


class TurnoCancelar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cancelado_por_id: uuid.UUID
    motivo_cancelacion: str | None = None


class TurnoOut(BaseModel):
    id: str
    cliente_id: str
    profesional_id: str
    servicio_id: str
    inicio: str
    fin: str
    estado: Estado
    motivo_cancelacion: str | None
    cancelado_por_id: str | None
    creado_en: str
    actualizado_en: str


def turno_out(row: sqlite3.Row) -> TurnoOut:
    return TurnoOut.model_validate(dict(row))


# --------------------------------------------------------------------------- #
# Reglas de dominio
# --------------------------------------------------------------------------- #


def fetch_user(conn: sqlite3.Connection, user_id: uuid.UUID) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute("SELECT * FROM usuario WHERE id = ?", (str(user_id),)).fetchone()
    return row


def fetch_turno(conn: sqlite3.Connection, turno_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute("SELECT * FROM turno WHERE id = ?", (turno_id,)).fetchone()
    return row


def audit(
    conn: sqlite3.Connection, entity_id: str, action: str, actor: sqlite3.Row, moment: datetime, detail: str
) -> None:
    """RN-08: toda creación y cancelación deja un evento (en la misma transacción)."""
    conn.execute(
        "INSERT INTO auditoria_evento (id, entidad, entidad_id, accion, actor_id, rol, ocurrido_en, detalle)"
        " VALUES (?, 'turno', ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), entity_id, action, str(actor["id"]), str(actor["rol"]), fmt_utc(moment), detail),
    )


def validate_contact(client: sqlite3.Row) -> None:
    """RN-03: contacto normalizado y válido, y email verificado."""
    name = str(client["nombre_completo"]).strip()
    email = str(client["email"]).strip().lower()
    phone = re.sub(r"[\s\-()]", "", str(client["telefono"]))
    if not 2 <= len(name) <= 100 or not EMAIL_RE.match(email) or not PHONE_RE.match(phone):
        raise DomainError("INVALID_CONTACT")
    if not client["email_verificado"]:
        raise DomainError("EMAIL_NOT_VERIFIED")


def validate_start(start: datetime, now: datetime) -> None:
    """RN-04: antelación mínima, horizonte máximo y alineación a la grilla de slots."""
    if start < now + MIN_LEAD_TIME:
        raise DomainError("INVALID_TIME_RANGE", "El turno requiere al menos 1 hora de antelación.")
    if start > now + BOOKING_HORIZON:
        raise DomainError("INVALID_TIME_RANGE", "El turno excede el horizonte máximo de reserva (60 días).")
    local = start.astimezone(OPERATION_TZ)
    if local.second != 0 or local.microsecond != 0 or local.minute % SLOT_GRID_MINUTES != 0:
        raise DomainError("INVALID_TIME_RANGE", "El inicio debe estar alineado a la grilla de 15 minutos.")


def ensure_slot_free(
    conn: sqlite3.Connection, professional_id: str, client_id: str, start: datetime, end: datetime
) -> None:
    """INV-02 / RN-02. Debe ejecutarse dentro de la misma transacción que el INSERT."""
    local_start, local_end = start.astimezone(OPERATION_TZ), end.astimezone(OPERATION_TZ)
    within_hours = (
        local_start.date() == local_end.date()
        and conn.execute(
            "SELECT 1 FROM horario_laboral WHERE profesional_id = ? AND dia_semana = ?"
            " AND hora_inicio <= ? AND hora_fin >= ?",
            (professional_id, local_start.weekday(), local_start.strftime("%H:%M"), local_end.strftime("%H:%M")),
        ).fetchone()
        is not None
    )
    if not within_hours:
        raise DomainError("SLOT_UNAVAILABLE", "El horario está fuera del horario laboral del profesional.")
    start_s, end_s = fmt_utc(start), fmt_utc(end)
    overlaps = (
        ("SELECT 1 FROM turno WHERE profesional_id = ? AND estado = 'CONFIRMADO' AND inicio < ? AND ? < fin",
         professional_id),
        ("SELECT 1 FROM bloqueo WHERE profesional_id = ? AND inicio < ? AND ? < fin", professional_id),
        ("SELECT 1 FROM turno WHERE cliente_id = ? AND estado = 'CONFIRMADO' AND inicio < ? AND ? < fin", client_id),
    )
    for query, owner_id in overlaps:
        if conn.execute(query, (owner_id, end_s, start_s)).fetchone() is not None:
            raise DomainError("SLOT_UNAVAILABLE")


def create_turno(conn: sqlite3.Connection, body: TurnoCrear, now: datetime) -> TurnoOut:
    with transaction(conn):
        client = fetch_user(conn, body.cliente_id)
        service = conn.execute("SELECT * FROM servicio WHERE id = ?", (str(body.servicio_id),)).fetchone()
        if client is None or client["rol"] != "CLIENTE" or not client["activo"]:
            raise DomainError("NOT_FOUND", "El cliente no existe.")
        if service is None or not service["activo"]:
            raise DomainError("NOT_FOUND", "El servicio no existe o no está activo.")
        validate_contact(client)
        start = body.inicio.astimezone(timezone.utc)
        validate_start(start, now)
        end = start + timedelta(minutes=int(service["duracion_min"]))  # RN-04: ``fin`` lo calcula el servidor
        professional_id = str(service["profesional_id"])
        ensure_slot_free(conn, professional_id, str(client["id"]), start, end)

        turno_id, stamp = str(uuid.uuid4()), fmt_utc(now)
        conn.execute(
            "INSERT INTO turno (id, cliente_id, profesional_id, servicio_id, inicio, fin, estado,"
            " motivo_cancelacion, cancelado_por_id, creado_en, actualizado_en)"
            " VALUES (?, ?, ?, ?, ?, ?, 'CONFIRMADO', NULL, NULL, ?, ?)",
            (turno_id, str(client["id"]), professional_id, str(service["id"]), fmt_utc(start), fmt_utc(end), stamp, stamp),
        )
        audit(conn, turno_id, "TURNO_CREADO", client, now, f"inicio={fmt_utc(start)}; fin={fmt_utc(end)}")
        created = fetch_turno(conn, turno_id)
        if created is None:  # pragma: no cover - no puede ocurrir dentro de la transacción
            raise DomainError("PERSISTENCE_ERROR")
        return turno_out(created)


def cancel_turno(conn: sqlite3.Connection, turno_id: str, body: TurnoCancelar, now: datetime) -> TurnoOut:
    with transaction(conn):
        actor = fetch_user(conn, body.cancelado_por_id)
        turno = fetch_turno(conn, turno_id)
        if actor is None or turno is None:
            raise DomainError("NOT_FOUND", "El turno o el usuario no existe.")
        if not actor["activo"]:
            raise DomainError("FORBIDDEN")
        role = str(actor["rol"])
        if role == "CLIENTE" and turno["cliente_id"] != actor["id"]:
            raise DomainError("NOT_FOUND", "El turno no existe.")  # RBAC-02: no revelar turnos ajenos
        if role == "PROFESIONAL" and turno["profesional_id"] != actor["id"]:
            raise DomainError("FORBIDDEN")
        reason = (body.motivo_cancelacion or "").strip() or None
        if role != "CLIENTE" and reason is None:
            raise DomainError("INVALID_CONTACT", "El motivo de cancelación es obligatorio para PROFESIONAL y ADMIN.")
        if turno["estado"] != Estado.CONFIRMADO.value:
            raise DomainError("INVALID_STATE_TRANSITION")
        # INV-01 / RN-01: con la ventana exacta (>= 2 h) se permite; un turno ya iniciado queda dentro de la ventana.
        if role == "CLIENTE" and parse_utc(str(turno["inicio"])) - now < timedelta(hours=CANCELLATION_WINDOW_HOURS):
            raise DomainError("CANCELLATION_WINDOW_EXPIRED")

        conn.execute(
            "UPDATE turno SET estado = 'CANCELADO', motivo_cancelacion = ?, cancelado_por_id = ?, actualizado_en = ?"
            " WHERE id = ? AND estado = 'CONFIRMADO'",
            (reason, str(actor["id"]), fmt_utc(now), turno_id),
        )
        audit(conn, turno_id, "TURNO_CANCELADO", actor, now, reason or "")
        cancelled = fetch_turno(conn, turno_id)
        if cancelled is None:  # pragma: no cover - no puede ocurrir dentro de la transacción
            raise DomainError("PERSISTENCE_ERROR")
        return turno_out(cancelled)


# --------------------------------------------------------------------------- #
# Capa 2: aplicación FastAPI y endpoints (spec §11)
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()  # la tabla ``turno`` (y el resto del esquema) se crea sola al iniciar
    yield


app = FastAPI(title="Sistema de Turnos", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Location"],
)


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": {"codigo": exc.codigo, "mensaje": exc.mensaje}})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
    if any("inicio" in error["loc"] for error in exc.errors()):  # spec §11.4: INVALID_TIME_RANGE
        return await domain_error_handler(request, DomainError("INVALID_TIME_RANGE"))
    return await request_validation_exception_handler(request, exc)


@app.get("/api/turnos", response_model=list[TurnoOut])
def listar_turnos(
    conn: Db,
    cliente_id: uuid.UUID | None = None,
    profesional_id: uuid.UUID | None = None,
    estado: Annotated[Estado | None, Query()] = None,
) -> list[TurnoOut]:
    """Lista los turnos persistidos, por ``inicio`` ascendente. Los filtros se combinan con AND."""
    conditions: list[str] = []
    params: list[str] = []
    for column, value in (("cliente_id", cliente_id), ("profesional_id", profesional_id), ("estado", estado)):
        if value is not None:
            conditions.append(f"{column} = ?")
            params.append(value.value if isinstance(value, Estado) else str(value))
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    try:
        rows = conn.execute(f"SELECT * FROM turno{where} ORDER BY inicio ASC", params).fetchall()
    except sqlite3.Error as exc:
        raise _translate(exc) from exc
    return [turno_out(row) for row in rows]


@app.post("/api/turnos", response_model=TurnoOut, status_code=201)
def crear_turno(body: TurnoCrear, response: Response, conn: Db) -> TurnoOut:
    """Crea una reserva ``CONFIRMADO`` aplicando INV-02, RN-03 y RN-04 en una sola transacción."""
    created = create_turno(conn, body, now_utc())
    response.headers["Location"] = f"/api/turnos/{created.id}"
    return created


@app.post("/api/turnos/{turno_id}/cancelar", response_model=TurnoOut)
def cancelar_turno(turno_id: str, body: TurnoCancelar, conn: Db) -> TurnoOut:
    """Cancela un turno aplicando INV-01: el cliente solo puede hacerlo con 2 horas o más de anticipación."""
    return cancel_turno(conn, turno_id, body, now_utc())
