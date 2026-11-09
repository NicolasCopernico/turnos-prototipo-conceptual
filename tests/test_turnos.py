"""Suite de pruebas automatizadas del backend (spec.md §14, «Matriz de Tests Automatizados»).

Cómo se garantiza el aislamiento (nunca se toca `turnos.db`):
  * cada test recibe su propia base SQLite en `tmp_path` (copia de una plantilla con la semilla; se redirige `main.DB_PATH`);
  * la fixture `client` ejecuta el `lifespan` real de la app sobre esa base;
  * `test_aislamiento_no_toca_turnos_db_real` lo comprueba de forma explícita.

El reloj también está aislado: se reemplaza `main.utcnow` por un reloj controlable. Es necesario para el
Test 4, porque RN-04 impide crear por API un turno con menos de 1 h de antelación: la única forma de llegar
a «faltan 45 minutos» es crear el turno con tiempo y adelantar el reloj. Además evita falsos fallos por
grilla de 15 min o cambio de día.

Ejecutar:  pytest -q        (la suite completa corre en menos de 2 segundos)
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

import main

REAL_DB = main.DB_PATH  # ruta de la base real, capturada antes de que cualquier fixture la redirija

# Instante fijo y alineado a la grilla de 15 min. «Mañana» = AHORA + 1 día.
AHORA = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)

# Ids de la semilla de main.py: c1 y c3 son clientes con contacto válido y email verificado;
# s1 es «Consulta general» (30 min) de la profesional p1.
CLIENTE_1, CLIENTE_3, SERVICIO_1, PROFESIONAL_1 = "c1", "c3", "s1", "p1"


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------
class Reloj:
    """Reloj inyectable: reemplaza a `main.utcnow` durante el test."""

    def __init__(self, ahora: datetime) -> None:
        self.ahora = ahora

    def __call__(self) -> datetime:
        return self.ahora


@pytest.fixture
def reloj(monkeypatch: pytest.MonkeyPatch) -> Reloj:
    r = Reloj(AHORA)
    monkeypatch.setattr(main, "utcnow", r)
    return r


@pytest.fixture(scope="session")
def plantilla_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Base temporal con esquema y semilla, creada UNA vez (con `main.init_db()`, el mismo código de producción).

    Solo se usa como molde: cada test trabaja sobre su propia copia. Evita repetir las escrituras de la semilla
    (con fsync) en cada test y es lo que mantiene la suite por debajo de los 2 segundos.
    """
    ruta = tmp_path_factory.mktemp("plantilla") / "plantilla.db"
    assert ruta != REAL_DB
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(main, "DB_PATH", ruta)
        main.init_db()
    return ruta


@pytest.fixture
def db_path(plantilla_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Base SQLite propia de cada test: copia limpia de la plantilla, en `tmp_path`.

    `connect()` lee `main.DB_PATH` en cada llamada, así que alcanza con redirigirla.
    """
    ruta = tmp_path / "turnos_test.db"
    assert ruta != REAL_DB
    shutil.copyfile(plantilla_db, ruta)
    monkeypatch.setattr(main, "DB_PATH", ruta)
    return ruta


@pytest.fixture
def client(db_path: Path, reloj: Reloj) -> Iterator[TestClient]:
    with TestClient(main.app) as c:  # el lifespan de la app crea esquema y semilla en `db_path`
        yield c


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def iso(dt: datetime) -> str:
    return dt.strftime(main.TS_FORMAT)


def reservar(client: TestClient, cliente_id: str, inicio: datetime, servicio_id: str = SERVICIO_1) -> Any:
    return client.post("/api/turnos", json={"cliente_id": cliente_id, "servicio_id": servicio_id, "inicio": iso(inicio)})


def cancelar(client: TestClient, turno_id: str, actor_id: str, **extra: str) -> Any:
    return client.post(f"/api/turnos/{turno_id}/cancelar", json={"cancelado_por_id": actor_id, **extra})


def listar(client: TestClient, **filtros: str) -> list[dict[str, Any]]:
    r = client.get("/api/turnos", params=filtros)
    assert r.status_code == 200
    return list(r.json())


# ----------------------------------------------------------------------------
# Aislamiento (requisito transversal)
# ----------------------------------------------------------------------------
def _huella(ruta: Path) -> tuple[int, int] | None:
    return (ruta.stat().st_size, ruta.stat().st_mtime_ns) if ruta.exists() else None


def test_aislamiento_no_toca_turnos_db_real(client: TestClient, db_path: Path) -> None:
    """Escribir por la API deja intacta la `turnos.db` real y usa solo la base temporal."""
    assert main.DB_PATH == db_path != REAL_DB
    antes = _huella(REAL_DB)

    assert reservar(client, CLIENTE_1, AHORA + timedelta(days=1)).status_code == 201

    assert _huella(REAL_DB) == antes, "la prueba modificó la base de datos real"
    assert db_path.exists()


# ----------------------------------------------------------------------------
# Test 1 — Camino feliz e INV-03
# ----------------------------------------------------------------------------
def test_1_camino_feliz_crear_turno_y_listarlo(client: TestClient, db_path: Path) -> None:
    inicio = AHORA + timedelta(days=1)  # mañana

    r = reservar(client, CLIENTE_1, inicio)

    assert r.status_code == 201
    turno = r.json()
    assert turno["estado"] == "CONFIRMADO"
    assert turno["cliente_id"] == CLIENTE_1
    assert turno["profesional_id"] == PROFESIONAL_1  # lo calcula el servidor a partir del servicio
    assert turno["inicio"] == iso(inicio)
    assert turno["fin"] == iso(inicio + timedelta(minutes=30))  # fin = inicio + duración (RN-04)

    # Aparece al listar, con los mismos datos.
    assert listar(client) == [turno]

    # INV-03: está en la base, no en la memoria del proceso (lectura desde una conexión independiente)…
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM turno WHERE id = ?", (turno["id"],)).fetchone()[0] == 1

    # …y sobrevive a un «reinicio» del backend: una instancia nueva de la app lee el mismo turno.
    with TestClient(main.app) as reiniciado:
        assert listar(reiniciado) == [turno]


# ----------------------------------------------------------------------------
# Test 2 — INV-02: no solapamiento
# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("desplazamiento_min", "codigo_http"),
    [
        pytest.param(0, 409, id="mismo-horario"),
        pytest.param(15, 409, id="solapado-parcialmente"),
        pytest.param(30, 201, id="contiguo-es-valido"),  # intervalos [inicio, fin): A.fin == B.inicio no solapa
    ],
)
def test_2_inv02_no_solapamiento_del_profesional(client: TestClient, desplazamiento_min: int, codigo_http: int) -> None:
    inicio = AHORA + timedelta(days=1)
    primero = reservar(client, CLIENTE_1, inicio)
    assert primero.status_code == 201

    # Otro cliente, mismo profesional (servicio s1 es de p1).
    segundo = reservar(client, CLIENTE_3, inicio + timedelta(minutes=desplazamiento_min))

    assert segundo.status_code == codigo_http
    turnos = listar(client)
    if codigo_http == 409:
        assert segundo.json()["error"] == "SLOT_UNAVAILABLE"
        assert [t["id"] for t in turnos] == [primero.json()["id"]]  # el rechazado no dejó nada persistido
    else:
        assert len(turnos) == 2


# ----------------------------------------------------------------------------
# Test 3 — INV-01: cancelación exitosa (>= 2 h)
# ----------------------------------------------------------------------------
def test_3_inv01_cancelacion_exitosa_con_5_horas_de_anticipacion(client: TestClient) -> None:
    inicio = AHORA + timedelta(hours=5)
    turno = reservar(client, CLIENTE_1, inicio).json()

    r = cancelar(client, turno["id"], CLIENTE_1)

    assert r.status_code == 200
    cancelado = r.json()
    assert cancelado["id"] == turno["id"]
    assert cancelado["estado"] == "CANCELADO"
    assert cancelado["cancelado_por_id"] == CLIENTE_1

    # El cambio quedó persistido y el slot se liberó (RN-02: un turno CANCELADO no cuenta para el solapamiento).
    assert [t["estado"] for t in listar(client, estado="CANCELADO")] == ["CANCELADO"]
    assert listar(client, estado="CONFIRMADO") == []
    assert reservar(client, CLIENTE_3, inicio).status_code == 201


# ----------------------------------------------------------------------------
# Test 4 — INV-01: bloqueo de cancelación tardía (< 2 h)
# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("faltan", "codigo_http"),
    [
        pytest.param(timedelta(minutes=45), 409, id="45-minutos"),
        pytest.param(timedelta(hours=1, minutes=59), 409, id="1h59m-criterio-5"),
        pytest.param(timedelta(hours=2), 200, id="exactamente-2h-se-permite"),  # el bloqueo es solo para «menos de 2 horas»
    ],
)
def test_4_inv01_cancelacion_tardia_rechazada(client: TestClient, reloj: Reloj, faltan: timedelta, codigo_http: int) -> None:
    # RN-04 exige 1 h de antelación para reservar, así que el turno se crea con tiempo
    # y luego se adelanta el reloj hasta que falten `faltan` para el inicio.
    inicio = AHORA + timedelta(hours=3)
    turno = reservar(client, CLIENTE_1, inicio).json()
    reloj.ahora = inicio - faltan

    r = cancelar(client, turno["id"], CLIENTE_1)

    assert r.status_code == codigo_http
    if codigo_http == 409:
        assert r.json()["error"] == "CANCELLATION_WINDOW_EXPIRED"
        assert [t["estado"] for t in listar(client)] == ["CONFIRMADO"]  # el turno sigue intacto
    else:
        assert r.json()["estado"] == "CANCELADO"


# ----------------------------------------------------------------------------
# Test 5 — Validación automática de Pydantic (spec §11, INVALID_REQUEST)
# ----------------------------------------------------------------------------
VALIDO = {"cliente_id": CLIENTE_1, "servicio_id": SERVICIO_1, "inicio": "2026-11-10T12:00:00Z"}


@pytest.mark.parametrize(
    ("ruta", "cuerpo"),
    [
        pytest.param("/api/turnos", {}, id="crear-cuerpo-vacio"),
        pytest.param("/api/turnos", {k: v for k, v in VALIDO.items() if k != "inicio"}, id="crear-falta-inicio"),
        pytest.param("/api/turnos", {k: v for k, v in VALIDO.items() if k != "servicio_id"}, id="crear-falta-servicio"),
        pytest.param("/api/turnos", {**VALIDO, "cliente_id": 123}, id="crear-cliente_id-numerico"),
        pytest.param("/api/turnos", {**VALIDO, "inicio": "mañana a la tarde"}, id="crear-inicio-no-es-fecha"),
        pytest.param("/api/turnos", {**VALIDO, "inicio": None}, id="crear-inicio-nulo"),
        pytest.param("/api/turnos/cualquiera/cancelar", {}, id="cancelar-falta-actor"),
        pytest.param("/api/turnos/cualquiera/cancelar", {"cancelado_por_id": 7}, id="cancelar-actor-numerico"),
    ],
)
def test_5_pydantic_rechaza_cuerpos_invalidos(client: TestClient, ruta: str, cuerpo: dict[str, Any]) -> None:
    r = client.post(ruta, json=cuerpo)

    assert r.status_code == 422
    assert r.json()["error"] == "INVALID_REQUEST"
    assert listar(client) == []  # un cuerpo inválido no persiste nada (INV-03)


def test_5_pydantic_rechaza_json_malformado(client: TestClient) -> None:
    r = client.post("/api/turnos", content="{no es json", headers={"Content-Type": "application/json"})

    assert r.status_code == 422
    assert r.json()["error"] == "INVALID_REQUEST"
