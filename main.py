"""Backend de Turnos Pro (Fase 2) — FastAPI + SQLite.

Fuente de verdad: spec.md (§4 esquema, §7 errores, §11 contrato REST) y plan.md v2.

Reglas que hace cumplir el servidor:
    INV-01 (RN-01)  ventana mínima de 2 h para que un CLIENTE cancele.
    INV-02 (RN-02)  sin solapamiento de turnos CONFIRMADO (profesional y cliente).
    INV-03          persistencia real y atómica (una transacción por escritura).
    RN-03, RN-04 (parcial: sin horario laboral) y RN-06.

Fuera de alcance en esta fase (spec §9, plan §5): horario laboral, bloqueos, auditoría, emails y login.

Organización del archivo (de abajo hacia arriba, cada capa solo usa las anteriores):
    1. Configuración          3. Tiempo                5. Esquemas de la API (Pydantic)
    2. Errores de dominio     4. Persistencia (SQLite) 6. Reglas de dominio
    7. Aplicación FastAPI y endpoints

Levantar:  uvicorn main:app --reload --port 8012     Swagger UI: http://localhost:8012/docs
(El puerto es configurable: no forma parte del contrato, spec §11.2.)
"""
from __future__ import annotations

import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Iterator, Optional

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, StringConstraints

# ============================================================================
# 1. Configuración (constantes del spec)
# ============================================================================
DB_PATH = Path(os.environ.get("TURNOS_DB", Path(__file__).with_name("turnos.db")))
CANCELLATION_WINDOW = timedelta(hours=2)   # RN-01: un CLIENTE no cancela con menos de 2 h
MIN_LEAD_TIME = timedelta(hours=1)         # RN-04: antelación mínima para reservar
MAX_HORIZON = timedelta(days=60)           # RN-04: horizonte máximo de reserva
SLOT_GRID_MIN = 15                         # RN-04: grilla de slots, en minutos
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"           # UTC, ancho fijo: el orden de texto = orden temporal

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


# ============================================================================
# 2. Errores de dominio (catálogo del spec §7)
# ============================================================================
class DomainError(Exception):
    """Error de negocio con código HTTP y código del catálogo (§7). Se responde como `{"error", "detalle"}`."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def error_slot_no_disponible(detail: str) -> DomainError:
    """409 `SLOT_UNAVAILABLE` (INV-02)."""
    return DomainError(409, "SLOT_UNAVAILABLE", detail)


def error_no_encontrado(detail: str = "Recurso no encontrado.") -> DomainError:
    """404 `NOT_FOUND`. También se usa para recursos ajenos (RBAC-02: no se revela que existen)."""
    return DomainError(404, "NOT_FOUND", detail)


# ============================================================================
# 3. Tiempo
# ============================================================================
def utcnow() -> datetime:
    """Instante actual en UTC. Único punto de acceso al reloj: los tests lo reemplazan."""
    return datetime.now(timezone.utc)


def a_utc(dt: datetime) -> datetime:
    """Normaliza a UTC sin microsegundos. Un `datetime` sin zona horaria se interpreta como UTC."""
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)).replace(microsecond=0)


def formatear_instante(dt: datetime) -> str:
    """`datetime` → texto `YYYY-MM-DDTHH:MM:SSZ` (así se guarda en la base y se devuelve en la API)."""
    return a_utc(dt).strftime(TS_FORMAT)


def parsear_instante(texto: str) -> datetime:
    """Texto `YYYY-MM-DDTHH:MM:SSZ` → `datetime` con zona UTC (inverso de `formatear_instante`)."""
    return datetime.strptime(texto, TS_FORMAT).replace(tzinfo=timezone.utc)


# ============================================================================
# 4. Persistencia (capa 3): esquema idéntico a spec §4.1
# ============================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS usuario (
    id               TEXT PRIMARY KEY,
    email            TEXT NOT NULL UNIQUE,
    email_verificado INTEGER NOT NULL DEFAULT 0,
    password_hash    TEXT NOT NULL,
    nombre_completo  TEXT NOT NULL,
    telefono         TEXT NOT NULL,
    rol              TEXT NOT NULL CHECK (rol IN ('CLIENTE','PROFESIONAL','ADMIN')),
    activo           INTEGER NOT NULL DEFAULT 1,
    creado_en        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS servicio (
    id                TEXT PRIMARY KEY,
    profesional_id    TEXT NOT NULL REFERENCES usuario(id),
    nombre            TEXT NOT NULL,
    descripcion       TEXT,
    duracion_min      INTEGER NOT NULL CHECK (duracion_min BETWEEN 15 AND 480),
    precio_referencia REAL NOT NULL CHECK (precio_referencia >= 0),
    activo            INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS turno (
    id                 TEXT PRIMARY KEY,
    cliente_id         TEXT NOT NULL REFERENCES usuario(id),
    profesional_id     TEXT NOT NULL REFERENCES usuario(id),
    servicio_id        TEXT NOT NULL REFERENCES servicio(id),
    inicio             TEXT NOT NULL,
    fin                TEXT NOT NULL,
    estado             TEXT NOT NULL CHECK (estado IN ('CONFIRMADO','CANCELADO','COMPLETADO','AUSENTE')),
    motivo_cancelacion TEXT,
    cancelado_por_id   TEXT REFERENCES usuario(id),
    creado_en          TEXT NOT NULL,
    actualizado_en     TEXT NOT NULL,
    CHECK (cliente_id <> profesional_id),
    CHECK (inicio < fin)
);
CREATE INDEX IF NOT EXISTS idx_turno_prof_inicio ON turno(profesional_id, inicio);
"""

# Semilla tomada de los datos mock de index.html (sin turnos: los crea la API).
# Usuarios: (id, rol, nombre_completo, email, telefono, email_verificado)
SEED_USERS = [
    ("admin", "ADMIN", "Admin General", "admin@turnospro.test", "+5491100000000", 1),
    ("p1", "PROFESIONAL", "Dra. Ana Martínez", "ana@turnospro.test", "+5491100000001", 1),
    ("p2", "PROFESIONAL", "Dr. Bruno Salas", "bruno@turnospro.test", "+5491100000002", 1),
    ("p3", "PROFESIONAL", "Lic. Carla Ibáñez", "carla@turnospro.test", "+5491100000003", 1),
    ("c1", "CLIENTE", "Lucía Fernández", "lucia@mail.test", "+5491155550101", 1),
    ("c2", "CLIENTE", "Martín Gómez", "martin@mail.test", "+5491155550102", 0),  # email sin verificar
    ("c3", "CLIENTE", "Sofía Ruiz", "sofia@mail.test", "+5491155550103", 1),
]
# Servicios: (id, profesional_id, nombre, descripcion, duracion_min, precio_referencia)
SEED_SERVICES = [
    ("s1", "p1", "Consulta general", "Evaluación clínica y orientación.", 30, 18000),
    ("s2", "p1", "Control anual", "Chequeo completo con estudios básicos.", 60, 32000),
    ("s3", "p2", "Sesión de terapia", "Sesión individual.", 45, 25000),
    ("s4", "p2", "Evaluación inicial", "Primera entrevista y plan de trabajo.", 60, 30000),
    ("s5", "p3", "Asesoría nutricional", "Seguimiento y ajustes.", 30, 15000),
    ("s6", "p3", "Plan alimentario", "Diseño de plan personalizado.", 45, 22000),
]
NO_LOGIN_HASH = "!SIN_LOGIN_FASE_2"  # F-01 no existe todavía (spec D-06); no es un hash válido


def conectar() -> sqlite3.Connection:
    """Abre una conexión nueva a `DB_PATH` (se lee en cada llamada, por eso los tests pueden redirigirla)."""
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)  # transacciones manuales
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaccion_escritura() -> Iterator[sqlite3.Connection]:
    """Transacción de escritura exclusiva: verificación + escritura atómicas (INV-03, RN-02).

    `BEGIN IMMEDIATE` toma el bloqueo de escritura antes de verificar, así que dos solicitudes
    simultáneas por el mismo horario no pueden confirmarse ambas. Ante cualquier excepción hace ROLLBACK.
    """
    conn = conectar()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


@contextmanager
def conexion_lectura() -> Iterator[sqlite3.Connection]:
    """Conexión de solo lectura para las consultas (los datos siempre salen de la base, INV-03)."""
    conn = conectar()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Crea las tablas si no existen y carga la semilla solo si la base está vacía."""
    ahora = formatear_instante(utcnow())
    conn = conectar()
    try:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM usuario").fetchone()[0] == 0:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO usuario (id, rol, nombre_completo, email, telefono, email_verificado,"
                " password_hash, activo, creado_en) VALUES (?,?,?,?,?,?,?,1,?)",
                [(i, r, n, e, t, v, NO_LOGIN_HASH, ahora) for (i, r, n, e, t, v) in SEED_USERS],
            )
            conn.executemany(
                "INSERT INTO servicio (id, profesional_id, nombre, descripcion, duracion_min,"
                " precio_referencia, activo) VALUES (?,?,?,?,?,?,1)",
                SEED_SERVICES,
            )
            conn.execute("COMMIT")
    finally:
        conn.close()


# ============================================================================
# 5. Esquemas de la API (Pydantic)
# ============================================================================
# Un identificador no puede ser vacío ni estar compuesto solo por espacios: se recorta y se exige 1+ carácter.
IdTexto = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Estado(str, Enum):
    """Ciclo de vida del turno (RN-06). CANCELADO, COMPLETADO y AUSENTE son estados terminales."""

    CONFIRMADO = "CONFIRMADO"
    CANCELADO = "CANCELADO"
    COMPLETADO = "COMPLETADO"
    AUSENTE = "AUSENTE"


class TurnoCreate(BaseModel):
    """Cuerpo de `POST /api/turnos`. `fin` y `profesional_id` los calcula el servidor (spec §11.2)."""

    cliente_id: IdTexto = Field(description="Id del cliente que reserva (rol CLIENTE, activo).", examples=["c1"])
    servicio_id: IdTexto = Field(description="Id del servicio a reservar. Define el profesional y la duración.", examples=["s1"])
    inicio: datetime = Field(
        description="Inicio del turno en ISO 8601. Sin zona horaria se interpreta como UTC. "
        "Debe caer en la grilla de 15 min, con al menos 1 h de antelación y a no más de 60 días (RN-04).",
        examples=["2026-11-10T14:00:00Z"],
    )


class CancelarIn(BaseModel):
    """Cuerpo de `POST /api/turnos/{id}/cancelar` (spec §11.3). El actor viaja en el cuerpo por D-06."""

    cancelado_por_id: IdTexto = Field(description="Id del usuario que cancela (cliente, profesional o admin).", examples=["c1"])
    motivo_cancelacion: Optional[str] = Field(
        default=None,
        description="Obligatorio si cancela un PROFESIONAL o un ADMIN; opcional para el CLIENTE.",
        examples=["El profesional no puede atender ese día."],
    )


class TurnoOut(BaseModel):
    """Turno tal como lo devuelve la API. Los instantes están en UTC con formato `YYYY-MM-DDTHH:MM:SSZ`."""

    id: str = Field(description="Identificador del turno (UUID).")
    cliente_id: str
    profesional_id: str = Field(description="Profesional del servicio (lo calcula el servidor).")
    servicio_id: str
    inicio: str = Field(examples=["2026-11-10T14:00:00Z"])
    fin: str = Field(description="`inicio` + duración del servicio (lo calcula el servidor).", examples=["2026-11-10T14:30:00Z"])
    estado: Estado
    motivo_cancelacion: Optional[str]
    cancelado_por_id: Optional[str]
    creado_en: str
    actualizado_en: str


class ErrorOut(BaseModel):
    """Cuerpo de todos los errores de la API (spec §7 y §11): `{"error": CODIGO, "detalle": texto}`."""

    error: str = Field(description="Código del catálogo de errores (spec §7).", examples=["SLOT_UNAVAILABLE"])
    detalle: str = Field(description="Explicación legible del motivo del rechazo.")


def fila_a_turno(fila: sqlite3.Row) -> TurnoOut:
    """Fila de la tabla `turno` → respuesta de la API."""
    return TurnoOut(**{campo: fila[campo] for campo in TurnoOut.model_fields})


# ============================================================================
# 6. Reglas de dominio (funciones sobre la conexión de la transacción)
# ============================================================================
def validar_contacto(usuario: sqlite3.Row) -> None:
    """RN-03: nombre, email y teléfono válidos, y email verificado. Si no, `INVALID_CONTACT` / `EMAIL_NOT_VERIFIED`."""
    nombre = (usuario["nombre_completo"] or "").strip()
    if not (2 <= len(nombre) <= 100):
        raise DomainError(422, "INVALID_CONTACT", "El nombre completo debe tener entre 2 y 100 caracteres.")
    if not EMAIL_RE.match((usuario["email"] or "").strip().lower()):
        raise DomainError(422, "INVALID_CONTACT", "El email no es válido.")
    if not PHONE_E164_RE.match((usuario["telefono"] or "").strip()):
        raise DomainError(422, "INVALID_CONTACT", "El teléfono debe estar en formato E.164 (ej. +5491155550101).")
    if not usuario["email_verificado"]:
        raise DomainError(403, "EMAIL_NOT_VERIFIED", "Verificá tu email antes de reservar.")


def validar_horario(inicio: datetime, ahora: datetime) -> None:
    """RN-04: futuro con antelación mínima, dentro del horizonte y alineado a la grilla de 15 min."""
    if inicio < ahora + MIN_LEAD_TIME:
        raise DomainError(422, "INVALID_TIME_RANGE", "El turno debe comenzar con al menos 1 hora de antelación.")
    if inicio > ahora + MAX_HORIZON:
        raise DomainError(422, "INVALID_TIME_RANGE", "No se puede reservar con más de 60 días de anticipación.")
    if inicio.second != 0 or inicio.minute % SLOT_GRID_MIN != 0:
        raise DomainError(422, "INVALID_TIME_RANGE", f"El inicio debe estar alineado a la grilla de {SLOT_GRID_MIN} minutos.")


def verificar_sin_solapamiento(conn: sqlite3.Connection, columna: str, id_propietario: str, inicio: str, fin: str) -> None:
    """INV-02: sin solapamiento de `[inicio, fin)` entre turnos CONFIRMADO (turnos contiguos son válidos).

    `columna` es `profesional_id` o `cliente_id` (valores internos, nunca vienen del usuario).
    Los turnos CANCELADO no cuentan: su horario queda libre.
    """
    hay_solapado = conn.execute(
        f"SELECT 1 FROM turno WHERE {columna} = ? AND estado = 'CONFIRMADO' AND inicio < ? AND ? < fin LIMIT 1",
        (id_propietario, fin, inicio),
    ).fetchone()
    if hay_solapado:
        quien = "El profesional" if columna == "profesional_id" else "El cliente"
        raise error_slot_no_disponible(f"{quien} ya tiene un turno confirmado que se solapa con ese horario.")


def registrar_reserva(conn: sqlite3.Connection, datos: TurnoCreate, ahora: datetime) -> sqlite3.Row:
    """Valida y crea un turno `CONFIRMADO` dentro de la transacción abierta (spec §11.2).

    Orden de las comprobaciones: existencia (404) → contacto (RN-03) → horario (RN-04) → solapamiento (INV-02).
    """
    cliente = conn.execute(
        "SELECT * FROM usuario WHERE id = ? AND rol = 'CLIENTE' AND activo = 1", (datos.cliente_id,)
    ).fetchone()
    servicio = conn.execute("SELECT * FROM servicio WHERE id = ? AND activo = 1", (datos.servicio_id,)).fetchone()
    if cliente is None:
        raise error_no_encontrado("Cliente inexistente.")
    if servicio is None:
        raise error_no_encontrado("Servicio inexistente o inactivo.")

    validar_contacto(cliente)
    inicio_dt = a_utc(datos.inicio)
    validar_horario(inicio_dt, ahora)

    inicio = formatear_instante(inicio_dt)
    fin = formatear_instante(inicio_dt + timedelta(minutes=servicio["duracion_min"]))
    verificar_sin_solapamiento(conn, "profesional_id", servicio["profesional_id"], inicio, fin)
    verificar_sin_solapamiento(conn, "cliente_id", cliente["id"], inicio, fin)

    turno_id, marca = str(uuid.uuid4()), formatear_instante(ahora)
    conn.execute(
        "INSERT INTO turno (id, cliente_id, profesional_id, servicio_id, inicio, fin, estado, creado_en, actualizado_en)"
        " VALUES (?,?,?,?,?,?,'CONFIRMADO',?,?)",
        (turno_id, cliente["id"], servicio["profesional_id"], servicio["id"], inicio, fin, marca, marca),
    )
    return conn.execute("SELECT * FROM turno WHERE id = ?", (turno_id,)).fetchone()


def registrar_cancelacion(conn: sqlite3.Connection, turno_id: str, datos: CancelarIn, ahora: datetime) -> sqlite3.Row:
    """Valida y cancela un turno dentro de la transacción abierta (spec §11.3).

    Orden de las comprobaciones: existencia y permisos (404) → estado terminal (409, RN-06) →
    ventana de 2 h para el CLIENTE (409, INV-01) o motivo obligatorio para PROFESIONAL/ADMIN (422).
    """
    turno = conn.execute("SELECT * FROM turno WHERE id = ?", (turno_id,)).fetchone()
    actor = conn.execute("SELECT * FROM usuario WHERE id = ? AND activo = 1", (datos.cancelado_por_id,)).fetchone()
    if turno is None or actor is None:
        raise error_no_encontrado("Turno inexistente.")

    # RBAC-02: un recurso ajeno responde 404, no 403 (no se revela que existe).
    if actor["rol"] == "CLIENTE" and turno["cliente_id"] != actor["id"]:
        raise error_no_encontrado("Turno inexistente.")
    if actor["rol"] == "PROFESIONAL" and turno["profesional_id"] != actor["id"]:
        raise error_no_encontrado("Turno inexistente.")

    if turno["estado"] != "CONFIRMADO":  # RN-06: CANCELADO, COMPLETADO y AUSENTE son terminales
        raise DomainError(409, "INVALID_STATE_TRANSITION", f"El turno está {turno['estado']} y no puede cancelarse.")

    motivo = (datos.motivo_cancelacion or "").strip() or None
    if actor["rol"] == "CLIENTE":
        # INV-01: el cliente no cancela con menos de 2 h (con exactamente 2 h sí se permite).
        if parsear_instante(turno["inicio"]) - ahora < CANCELLATION_WINDOW:
            raise DomainError(
                409, "CANCELLATION_WINDOW_EXPIRED", "Solo se puede cancelar hasta 2 horas antes del inicio del turno."
            )
    elif motivo is None:  # excepción de INV-01: PROFESIONAL/ADMIN cancelan siempre, pero con motivo
        raise DomainError(422, "INVALID_REQUEST", "El motivo de cancelación es obligatorio para Profesional/Administrador.")

    conn.execute(
        "UPDATE turno SET estado = 'CANCELADO', motivo_cancelacion = ?, cancelado_por_id = ?, actualizado_en = ? WHERE id = ?",
        (motivo, actor["id"], formatear_instante(ahora), turno_id),
    )
    return conn.execute("SELECT * FROM turno WHERE id = ?", (turno_id,)).fetchone()


# ============================================================================
# 7. Aplicación FastAPI (capa 2) y endpoints
# ============================================================================
@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Al iniciar el servidor crea las tablas (y la semilla si la base está vacía)."""
    init_db()
    yield


DESCRIPCION_API = """
API REST de **Turnos Pro** (Fase 2 del curso): reserva y cancelación de turnos con persistencia real en SQLite.

**Reglas que garantiza el servidor**

* **INV-01** — un cliente no puede cancelar con menos de 2 horas de anticipación.
* **INV-02** — no se solapan turnos confirmados del mismo profesional ni del mismo cliente.
* **INV-03** — cada operación se guarda de forma atómica: o se aplican todos los cambios o ninguno.

**Errores.** Todos tienen el cuerpo `{"error": "<CODIGO>", "detalle": "<texto>"}` con los códigos del spec §7.

**Autenticación.** Todavía no existe: el actor se indica en el cuerpo (`cliente_id`, `cancelado_por_id`) según la decisión D-06.
"""

app = FastAPI(
    title="Turnos Pro API",
    version="0.2.1",
    description=DESCRIPCION_API,
    openapi_tags=[{"name": "Turnos", "description": "Consulta, reserva y cancelación de turnos (spec §11)."}],
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(DomainError)
async def manejar_error_de_dominio(_: Request, exc: DomainError) -> JSONResponse:
    """Errores de negocio (§7) → `{"error": CODIGO, "detalle": texto}` con el HTTP del catálogo."""
    return JSONResponse(status_code=exc.status, content={"error": exc.code, "detalle": exc.detail})


@app.exception_handler(RequestValidationError)
async def manejar_error_de_validacion(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Cuerpo o parámetros mal formados (Pydantic) → `422 INVALID_REQUEST` con el campo y el motivo."""
    detalle = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return JSONResponse(status_code=422, content={"error": "INVALID_REQUEST", "detalle": detalle})


# Respuestas de error que se documentan en Swagger (el cuerpo real lo arman los handlers de arriba).
RESPUESTAS_422: dict[int | str, dict[str, Any]] = {
    422: {"model": ErrorOut, "description": "`INVALID_REQUEST`: cuerpo o parámetros mal formados (campo faltante, vacío o de tipo inválido)."}
}


@app.get("/", include_in_schema=False)
def servir_frontend() -> FileResponse:
    """Sirve la interfaz web (`prototipo-turnos.html`), que consume esta misma API."""
    return FileResponse(Path(__file__).parent / "prototipo-turnos.html", media_type="text/html")


@app.get(
    "/api/turnos",
    response_model=list[TurnoOut],
    tags=["Turnos"],
    summary="Listar turnos",
    responses=RESPUESTAS_422,
)
def listar_turnos(
    cliente_id: Optional[str] = Query(default=None, description="Solo los turnos de este cliente."),
    profesional_id: Optional[str] = Query(default=None, description="Solo los turnos de este profesional."),
    estado: Optional[Estado] = Query(default=None, description="Solo los turnos en este estado."),
) -> list[TurnoOut]:
    """Devuelve los turnos ordenados por `inicio`. Los tres filtros son opcionales y se combinan con AND.

    Los datos salen siempre de la base de datos (INV-03), nunca de una caché en memoria.
    """
    condiciones: list[str] = []
    parametros: list[str] = []
    for columna, valor in (("cliente_id", cliente_id), ("profesional_id", profesional_id)):
        if valor is not None:
            condiciones.append(f"{columna} = ?")
            parametros.append(valor)
    if estado is not None:
        condiciones.append("estado = ?")
        parametros.append(estado.value)
    sql = "SELECT * FROM turno" + (" WHERE " + " AND ".join(condiciones) if condiciones else "") + " ORDER BY inicio"
    with conexion_lectura() as conn:
        return [fila_a_turno(fila) for fila in conn.execute(sql, parametros).fetchall()]


@app.post(
    "/api/turnos",
    response_model=TurnoOut,
    status_code=201,
    tags=["Turnos"],
    summary="Crear una reserva",
    responses={
        **RESPUESTAS_422,
        403: {"model": ErrorOut, "description": "`EMAIL_NOT_VERIFIED`: el cliente no verificó su email (RN-03)."},
        404: {"model": ErrorOut, "description": "`NOT_FOUND`: el cliente o el servicio no existen (o el servicio está inactivo)."},
        409: {"model": ErrorOut, "description": "`SLOT_UNAVAILABLE`: el horario se solapa con otro turno confirmado (INV-02)."},
    },
)
def crear_turno(datos: TurnoCreate) -> TurnoOut:
    """Crea un turno `CONFIRMADO`. El servidor calcula `profesional_id`, `fin`, `estado` y las marcas de tiempo.

    Códigos `422` posibles: `INVALID_REQUEST` (cuerpo mal formado, ids vacíos o solo espacios),
    `INVALID_CONTACT` (datos de contacto inválidos, RN-03) e `INVALID_TIME_RANGE` (horario fuera de RN-04).

    La validación y la inserción ocurren en una única transacción (INV-03).
    """
    with transaccion_escritura() as conn:
        return fila_a_turno(registrar_reserva(conn, datos, utcnow()))


@app.post(
    "/api/turnos/{turno_id}/cancelar",
    response_model=TurnoOut,
    tags=["Turnos"],
    summary="Cancelar una reserva",
    responses={
        **RESPUESTAS_422,
        404: {"model": ErrorOut, "description": "`NOT_FOUND`: el turno no existe (o es de otro cliente/profesional, RBAC-02)."},
        409: {
            "model": ErrorOut,
            "description": "`CANCELLATION_WINDOW_EXPIRED`: faltan menos de 2 h (INV-01), o "
            "`INVALID_STATE_TRANSITION`: el turno ya no está CONFIRMADO (RN-06).",
        },
    },
)
def cancelar_turno(turno_id: str, datos: CancelarIn) -> TurnoOut:
    """Pasa el turno de `CONFIRMADO` a `CANCELADO`; el horario queda libre y el turno nunca se borra.

    * **CLIENTE**: solo puede cancelar sus turnos y hasta 2 h antes del inicio (INV-01).
    * **PROFESIONAL / ADMIN**: pueden cancelar fuera de ventana, pero el `motivo_cancelacion` es obligatorio (`422`).
    * Un turno ya cancelado (o completado / ausente) no se puede cancelar de nuevo (`409`).
    """
    with transaccion_escritura() as conn:
        return fila_a_turno(registrar_cancelacion(conn, turno_id, datos, utcnow()))
