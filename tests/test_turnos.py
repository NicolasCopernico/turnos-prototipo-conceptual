"""Suite de pruebas de la API de turnos (spec.md §3, §7, §11).

Aislamiento: cada test usa una base SQLite propia en un directorio temporal
(`tmp_path`), por lo que `turnos.db` real nunca se abre ni se modifica.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main

REAL_DB = main.DB_PATH  # ruta de la base real, solo para verificar que no se toca

# Datos de la semilla (main.SEED_USERS / SEED_SERVICES)
CLIENTE_1, CLIENTE_3 = "c1", "c3"      # ambos con email verificado
SERVICIO_30MIN = "s1"                   # profesional p1, 30 min


def proximo_slot(delta: timedelta) -> datetime:
    """Instante futuro `now + delta` redondeado hacia arriba a la grilla de 15 min (RN-04)."""
    t = (datetime.now(timezone.utc) + delta).replace(second=0, microsecond=0)
    return t + timedelta(minutes=(-t.minute) % 15)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient sobre una base SQLite temporal, creada y sembrada por el lifespan de la app."""
    db_temporal = tmp_path / "turnos_test.db"
    monkeypatch.setattr(main, "DB_PATH", db_temporal)
    assert main.DB_PATH != REAL_DB
    with TestClient(main.app) as c:  # el `with` ejecuta el lifespan (init_db)
        yield c
    assert db_temporal.exists()


def crear(client, inicio: datetime, cliente_id=CLIENTE_1, servicio_id=SERVICIO_30MIN):
    return client.post("/api/turnos", json={
        "cliente_id": cliente_id, "servicio_id": servicio_id, "inicio": iso(inicio),
    })


def cancelar(client, turno_id, actor="c1"):
    return client.post(f"/api/turnos/{turno_id}/cancelar", json={"cancelado_por_id": actor})


# --- Test 1: camino feliz + INV-03 ------------------------------------------
def test_crear_turno_valido_aparece_en_el_listado(client):
    inicio = proximo_slot(timedelta(days=1))

    resp = crear(client, inicio)
    assert resp.status_code == 201
    turno = resp.json()
    assert turno["estado"] == "CONFIRMADO"
    assert turno["inicio"] == iso(inicio)

    listado = client.get("/api/turnos")
    assert listado.status_code == 200
    assert [t["id"] for t in listado.json()] == [turno["id"]]


# --- Test 2: INV-02 no solapamiento -----------------------------------------
def test_segundo_turno_en_mismo_horario_es_rechazado(client):
    inicio = proximo_slot(timedelta(days=1))

    assert crear(client, inicio, cliente_id=CLIENTE_1).status_code == 201
    segundo = crear(client, inicio, cliente_id=CLIENTE_3)  # mismo profesional, mismo horario

    assert segundo.status_code in (400, 409)
    assert segundo.status_code == 409
    assert segundo.json()["error"] == "SLOT_UNAVAILABLE"
    assert len(client.get("/api/turnos").json()) == 1


# --- Test 3: INV-01 cancelación con >= 2 h ----------------------------------
def test_cancelar_con_cinco_horas_de_anticipacion_funciona(client):
    turno = crear(client, proximo_slot(timedelta(hours=5))).json()

    resp = cancelar(client, turno["id"])

    assert resp.status_code == 200
    assert resp.json()["estado"] == "CANCELADO"


# --- Test 4: INV-01 bloqueo con < 2 h ---------------------------------------
def test_cancelar_a_45_minutos_es_rechazado(client, monkeypatch):
    # RN-04 impide *crear* un turno a 45 min (antelación mínima 1 h), así que se crea con
    # tiempo de sobra y luego se adelanta el reloj del servidor hasta 45 min antes del inicio.
    inicio = proximo_slot(timedelta(hours=3))
    turno = crear(client, inicio).json()
    ahora_simulado = inicio - timedelta(minutes=45)
    monkeypatch.setattr(main, "utcnow", lambda: ahora_simulado)

    resp = cancelar(client, turno["id"])

    assert resp.status_code in (400, 409)
    assert resp.status_code == 409
    assert resp.json()["error"] == "CANCELLATION_WINDOW_EXPIRED"
    estado = client.get("/api/turnos").json()[0]["estado"]
    assert estado == "CONFIRMADO"  # el turno no cambió


# --- Test 5: validación automática de Pydantic ------------------------------
@pytest.mark.parametrize("cuerpo", [
    {},                                                                   # sin campos
    {"cliente_id": "c1", "servicio_id": "s1"},                            # falta inicio
    {"servicio_id": "s1", "inicio": "2030-01-01T10:00:00Z"},              # falta cliente_id
    {"cliente_id": "c1", "servicio_id": "s1", "inicio": "no-es-una-fecha"},  # fecha inválida
    {"cliente_id": 123, "servicio_id": "s1", "inicio": "2030-01-01T10:00:00Z"},  # tipo inválido
])
def test_post_con_datos_invalidos_es_rechazado(client, cuerpo):
    resp = client.post("/api/turnos", json=cuerpo)

    assert resp.status_code in (400, 422)
    assert resp.status_code == 422
    assert resp.json()["error"] == "INVALID_REQUEST"
    assert client.get("/api/turnos").json() == []
