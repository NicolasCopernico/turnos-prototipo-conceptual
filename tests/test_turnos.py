"""Suite de pruebas automatizadas del backend (spec.md §14, «Matriz de Tests Automatizados»).

Las fixtures (`client`, `db_path`, `reloj`) están en `tests/conftest.py` y las utilidades en `tests/helpers.py`.
Cada test usa su propia base SQLite temporal y un reloj inyectable: nunca se toca `turnos.db`.

El reloj es necesario para el Test 4, porque RN-04 impide crear por API un turno con menos de 1 h de
antelación: la única forma de llegar a «faltan 45 minutos» es crear el turno con tiempo y adelantar el reloj.
Además evita falsos fallos por grilla de 15 min o cambio de día.

Ejecutar:  pytest -q
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import main
from helpers import (AHORA, CLIENTE_1, CLIENTE_3, PROFESIONAL_1, REAL_DB, SERVICIO_1, Reloj, cancelar, iso,
                     listar, reservar)


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


# ----------------------------------------------------------------------------
# Casos de borde (blindaje de endpoints)
# ----------------------------------------------------------------------------
# Test 6 — Textos vacíos o compuestos solo por espacios
@pytest.mark.parametrize("vacio", ["", " ", "     ", "\t", "\n", " \t \n "], ids=repr)
@pytest.mark.parametrize("campo", ["cliente_id", "servicio_id"])
def test_6_crear_turno_rechaza_textos_vacios_o_solo_espacios(client: TestClient, campo: str, vacio: str) -> None:
    cuerpo = {**VALIDO, campo: vacio}

    r = client.post("/api/turnos", json=cuerpo)

    assert r.status_code == 422
    assert r.json()["error"] == "INVALID_REQUEST"
    assert campo in r.json()["detalle"]  # el mensaje dice qué campo está mal
    assert listar(client) == []  # no se persiste nada (INV-03)


@pytest.mark.parametrize("vacio", ["", "   ", "\t\n"], ids=repr)
def test_6_cancelar_rechaza_actor_vacio_o_solo_espacios(client: TestClient, vacio: str) -> None:
    turno = reservar(client, CLIENTE_1, AHORA + timedelta(hours=5)).json()

    r = cancelar(client, turno["id"], vacio)

    assert r.status_code == 422
    assert r.json()["error"] == "INVALID_REQUEST"
    assert [t["estado"] for t in listar(client)] == ["CONFIRMADO"]  # el turno no se tocó


# Test 7 — Cancelar un turno inexistente
@pytest.mark.parametrize("turno_id", ["no-existe", "3f2b8c1e-9d4a-4e6b-8a57-0c1d2e3f4a5b", "0"], ids=["texto", "uuid", "cero"])
def test_7_cancelar_turno_inexistente_devuelve_404(client: TestClient, turno_id: str) -> None:
    r = cancelar(client, turno_id, CLIENTE_1)

    assert r.status_code == 404
    assert r.json()["error"] == "NOT_FOUND"
    assert listar(client) == []


# Test 8 — Cancelar un turno que ya estaba cancelado (RN-06: CANCELADO es terminal)
@pytest.mark.parametrize(
    ("segundo_actor", "extra"),
    [
        pytest.param(CLIENTE_1, {}, id="el-mismo-cliente"),
        pytest.param("admin", {"motivo_cancelacion": "Duplicado"}, id="el-administrador-con-motivo"),
    ],
)
def test_8_cancelar_turno_ya_cancelado_se_rechaza(client: TestClient, reloj: Reloj, segundo_actor: str, extra: dict[str, str]) -> None:
    turno = reservar(client, CLIENTE_1, AHORA + timedelta(hours=5)).json()
    primera = cancelar(client, turno["id"], CLIENTE_1)
    assert primera.status_code == 200

    reloj.ahora += timedelta(minutes=10)  # si el 2.º intento escribiera algo, `actualizado_en` cambiaría
    segunda = cancelar(client, turno["id"], segundo_actor, **extra)

    assert segunda.status_code == 409
    assert segunda.json()["error"] == "INVALID_STATE_TRANSITION"
    assert listar(client) == [primera.json()]  # el registro quedó exactamente como tras la 1.ª cancelación
