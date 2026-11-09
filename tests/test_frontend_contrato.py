"""Compatibilidad frontend ↔ backend (spec.md §14, escenarios 9 a 14).

`prototipo-turnos.html` es la interfaz que consume la API. Estas pruebas NO abren un navegador: leen el HTML y
comprueban que lo que el frontend asume (URLs, campos, datos de ejemplo, reglas, códigos de error) coincide con lo
que el backend implementa, y que el backend responde de la forma que un navegador necesita (CORS).
Si alguno de los dos lados cambia y se desincroniza, el test correspondiente falla y dice qué no coincide.

El recorrido en un navegador real es opcional y está en tests/test_e2e_navegador.py (`pytest -m e2e`).
"""
from __future__ import annotations

import re
import runpy
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import main
from helpers import AHORA, CLIENTE_1, CLIENTE_3, SERVICIO_1, cancelar, listar, reservar

RAIZ = Path(__file__).resolve().parent.parent
HTML = RAIZ / "prototipo-turnos.html"
FUENTE = HTML.read_text(encoding="utf-8")


def _claves(cuerpo_js: str) -> set[str]:
    """Nombres de propiedad de un literal de objeto JS: `{ a: x, b: y(z) }` → {"a", "b"}."""
    return set(re.findall(r"(?:^|,)\s*(\w+)\s*:", cuerpo_js))


# ----------------------------------------------------------------------------
# Escenario 9 — Las llamadas HTTP del frontend corresponden a la API
# ----------------------------------------------------------------------------
def test_9_la_url_base_del_frontend_es_valida() -> None:
    m = re.search(r"const API_URL = '([^']+)'", FUENTE)
    assert m, "no se encontró `const API_URL = '...'` en prototipo-turnos.html"
    assert re.fullmatch(r"https?://[\w.-]+(:\d+)?", m.group(1)), f"API_URL inválida o con barra final: {m.group(1)!r}"


def test_9_los_endpoints_que_llama_el_frontend_existen_en_la_api() -> None:
    gets = {re.sub(r"\$\{[^}]+\}", "{}", p) for p in re.findall(r"fetch\(`\$\{API_URL\}(/api/[^`]*)`\)", FUENTE)}
    posts = {re.sub(r"\$\{[^}]+\}", "{}", p) for p in re.findall(r"callApi\(\s*[`']([^`']+)[`']", FUENTE)}
    assert gets == {"/api/turnos"} and posts == {"/api/turnos", "/api/turnos/{}/cancelar"}, (gets, posts)

    api = {(m.upper(), re.sub(r"\{[^}]+\}", "{}", ruta)) for ruta, ops in main.app.openapi()["paths"].items() for m in ops}
    llamadas = {("GET", p) for p in gets} | {("POST", p) for p in posts}
    assert llamadas <= api, f"el frontend llama a rutas que la API no tiene: {llamadas - api}"


def test_9_los_campos_que_envia_el_frontend_son_los_del_esquema() -> None:
    crear = re.search(r"callApi\('/api/turnos',\s*\{([^}]*)\}", FUENTE)
    cancel = re.search(r"callApi\(`/api/turnos/\$\{turnoId\}/cancelar`,\s*\{([^}]*)\}", FUENTE)
    assert crear and cancel, "no se encontraron los cuerpos de POST en prototipo-turnos.html"

    for enviados, modelo in ((_claves(crear.group(1)), main.TurnoCreate), (_claves(cancel.group(1)), main.CancelarIn)):
        campos = set(modelo.model_fields)
        obligatorios = {n for n, f in modelo.model_fields.items() if f.is_required()}
        assert enviados <= campos, f"{modelo.__name__}: el frontend envía campos que la API no define: {enviados - campos}"
        assert obligatorios <= enviados, f"{modelo.__name__}: el frontend no envía campos obligatorios: {obligatorios - enviados}"


def test_9_los_campos_que_lee_el_frontend_existen_en_la_respuesta() -> None:
    bloque = re.search(r"const turnoFromApi = .*?\n\}\);", FUENTE, re.S)
    assert bloque, "no se encontró `turnoFromApi` en prototipo-turnos.html"
    leidos = set(re.findall(r"\bt\.(\w+)", bloque.group(0)))
    assert len(leidos) >= 9, leidos  # evita que un cambio de formato deje el test vacío
    assert leidos <= set(main.TurnoOut.model_fields), f"el frontend lee campos que la API no devuelve: {leidos - set(main.TurnoOut.model_fields)}"


# ----------------------------------------------------------------------------
# Escenario 10 — Datos de ejemplo y reglas del frontend = los del backend
# ----------------------------------------------------------------------------
def test_10_los_usuarios_de_ejemplo_del_frontend_coinciden_con_la_semilla() -> None:
    patron = (r"\{ id: '(\w+)', role: '(\w+)', nombre: '([^']+)', email: '([^']+)', "
              r"telefono: '([^']+)', emailVerificado: (true|false) \}")
    frontend = [(i, r, n, e, t, 1 if v == "true" else 0) for i, r, n, e, t, v in re.findall(patron, FUENTE)]
    assert len(frontend) == len(main.SEED_USERS) == 7, frontend
    assert frontend == main.SEED_USERS, "los usuarios de ejemplo del HTML difieren de main.SEED_USERS"


def test_10_los_servicios_de_ejemplo_del_frontend_coinciden_con_la_semilla() -> None:
    patron = (r"\{ id: '(\w+)', profesionalId: '(\w+)', nombre: '([^']+)', descripcion: '([^']+)', "
              r"duracionMin: (\d+), precio: (\d+), activo: true \}")
    frontend = [(i, p, n, d, int(m), int(pr)) for i, p, n, d, m, pr in re.findall(patron, FUENTE)]
    assert len(frontend) == len(main.SEED_SERVICES) == 6, frontend
    assert frontend == main.SEED_SERVICES, "los servicios de ejemplo del HTML difieren de main.SEED_SERVICES"


def test_10_las_reglas_de_tiempo_del_frontend_coinciden_con_las_del_backend() -> None:
    m = re.search(r"const CONFIG = \{ CANCEL_WINDOW_H: (\d+), MIN_LEAD_H: (\d+), HORIZON_DAYS: (\d+), GRID_MIN: (\d+) \}", FUENTE)
    assert m, "no se encontró `const CONFIG = {...}` en prototipo-turnos.html"
    ventana, antelacion, horizonte, grilla = map(int, m.groups())
    assert timedelta(hours=ventana) == main.CANCELLATION_WINDOW, "ventana de cancelación (INV-01)"
    assert timedelta(hours=antelacion) == main.MIN_LEAD_TIME, "antelación mínima (RN-04)"
    assert timedelta(days=horizonte) == main.MAX_HORIZON, "horizonte de reserva (RN-04)"
    assert grilla == main.SLOT_GRID_MIN, "grilla de slots (RN-04)"


def test_10_los_codigos_de_error_del_backend_son_conocidos_por_el_frontend() -> None:
    tabla = re.search(r"const HTTP = \{(.*?)\};", FUENTE, re.S)
    assert tabla, "no se encontró la tabla `const HTTP = {...}` en prototipo-turnos.html"
    frontend = {c: int(n) for c, n in re.findall(r"(\w+): (\d+)", tabla.group(1))}

    codigo = main.__file__ and Path(main.__file__).read_text(encoding="utf-8")
    backend = {c: int(n) for n, c in re.findall(r'DomainError\(\s*(\d+),\s*"(\w+)"', codigo)}
    backend.update({c: int(n) for n, c in re.findall(r'status_code=(\d+), content=\{"error": "(\w+)"', codigo)})
    assert len(backend) >= 8, backend  # evita que un cambio de formato deje el test vacío

    for cod, http in backend.items():
        assert cod in frontend, f"el backend puede responder {cod} y el frontend no lo conoce"
        assert frontend[cod] == http, f"{cod}: el backend responde {http} y el frontend espera {frontend[cod]}"


# ----------------------------------------------------------------------------
# Escenario 11 — El backend acepta exactamente lo que envía el frontend
# ----------------------------------------------------------------------------
def _fecha_js(dt: datetime) -> str:
    """Lo que produce `new Date(ms).toISOString()` en el navegador: milisegundos y `Z`."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_11_el_backend_acepta_el_formato_de_fecha_y_el_cuerpo_del_frontend(client: TestClient) -> None:
    inicio = AHORA + timedelta(days=1)

    r = client.post("/api/turnos", json={"cliente_id": CLIENTE_1, "servicio_id": SERVICIO_1, "inicio": _fecha_js(inicio)})

    assert r.status_code == 201, r.text
    turno = r.json()
    # El frontend hace `Date.parse(...)` de estos campos: tienen que ser fechas ISO 8601 en UTC.
    for campo in ("inicio", "fin", "creado_en", "actualizado_en"):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", turno[campo]), (campo, turno[campo])
        datetime.fromisoformat(turno[campo].replace("Z", "+00:00"))

    # El frontend cancela enviando `motivo_cancelacion: null` cuando el cliente no escribe motivo.
    c = client.post(f"/api/turnos/{turno['id']}/cancelar", json={"cancelado_por_id": CLIENTE_1, "motivo_cancelacion": None})
    assert c.status_code == 200 and c.json()["estado"] == "CANCELADO", c.text


def test_11_los_errores_tienen_la_forma_que_el_frontend_lee(client: TestClient) -> None:
    """`callApi` lee `data.error` y `data.detalle`: todo error de dominio debe traer ambos como texto."""
    inicio = AHORA + timedelta(days=1)
    reservar(client, CLIENTE_1, inicio)
    errores = [
        reservar(client, CLIENTE_3, inicio),                                    # 409 SLOT_UNAVAILABLE
        reservar(client, "no-existe", inicio),                                  # 404 NOT_FOUND
        reservar(client, CLIENTE_3, AHORA + timedelta(minutes=30)),             # 422 INVALID_TIME_RANGE
        client.post("/api/turnos", json={}),                                    # 422 INVALID_REQUEST
        cancelar(client, "no-existe", CLIENTE_1),                               # 404 NOT_FOUND
    ]
    assert [r.status_code for r in errores] == [409, 404, 422, 422, 404]
    for r in errores:
        cuerpo = r.json()
        assert isinstance(cuerpo["error"], str) and cuerpo["error"].isupper()
        assert isinstance(cuerpo["detalle"], str) and cuerpo["detalle"]


# ----------------------------------------------------------------------------
# Escenario 12 — CORS: el navegador puede llamar a la API y leer los errores
# ----------------------------------------------------------------------------
# «null» es el origen de una página abierta como archivo (file://), que es como se usa el frontend.
@pytest.mark.parametrize("origen", ["null", "http://localhost:5500", "http://localhost:8012"], ids=["file", "otro-puerto", "mismo-origen"])
def test_12_cors_responde_el_preflight_del_navegador(client: TestClient, origen: str) -> None:
    for ruta in ("/api/turnos", "/api/turnos/cualquiera/cancelar"):
        r = client.options(ruta, headers={"Origin": origen, "Access-Control-Request-Method": "POST",
                                          "Access-Control-Request-Headers": "content-type"})

        assert r.status_code == 200, (ruta, r.text)
        assert r.headers["access-control-allow-origin"] in ("*", origen)
        assert "content-type" in r.headers["access-control-allow-headers"].lower()
        assert "POST" in r.headers["access-control-allow-methods"]


def test_12_cors_tambien_llega_en_las_respuestas_de_error(client: TestClient) -> None:
    """Sin la cabecera en un 409/404/422 el navegador oculta el cuerpo y la UI no puede mostrar el motivo."""
    inicio = AHORA + timedelta(days=1)
    reservar(client, CLIENTE_1, inicio)
    respuestas = [
        client.post("/api/turnos", json={"cliente_id": CLIENTE_3, "servicio_id": SERVICIO_1, "inicio": _fecha_js(inicio)},
                    headers={"Origin": "null"}),
        client.post("/api/turnos/no-existe/cancelar", json={"cancelado_por_id": CLIENTE_1}, headers={"Origin": "null"}),
        client.post("/api/turnos", json={}, headers={"Origin": "null"}),
        client.get("/api/turnos", headers={"Origin": "null"}),
    ]
    assert [r.status_code for r in respuestas] == [409, 404, 422, 200]
    assert all(r.headers.get("access-control-allow-origin") for r in respuestas)


# ----------------------------------------------------------------------------
# Escenario 13 — El backend sirve el frontend
# ----------------------------------------------------------------------------
def test_13_la_raiz_sirve_el_frontend(client: TestClient) -> None:
    r = client.get("/")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.content == HTML.read_bytes()
    assert "/" not in main.app.openapi()["paths"]  # no ensucia Swagger


# ----------------------------------------------------------------------------
# Escenario 14 — Los datos de ejemplo (seed_demo.py) cargan y la API los devuelve
# ----------------------------------------------------------------------------
def _correr_seed(monkeypatch: pytest.MonkeyPatch, *argumentos: str) -> None:
    monkeypatch.setattr(sys, "argv", ["seed_demo.py", *argumentos])
    runpy.run_path(str(RAIZ / "seed_demo.py"), run_name="__main__")


def test_14_seed_demo_carga_los_15_turnos_y_la_api_los_devuelve(db_path: Path, client: TestClient,
                                                                monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`seed_demo.py` usa funciones de `main.py`; si se renombra una, este test lo detecta (ya pasó una vez)."""
    _correr_seed(monkeypatch)

    assert "15 turnos cargados" in capsys.readouterr().out
    turnos = listar(client)
    assert len(turnos) == 15
    assert {t["estado"] for t in turnos} == {"CONFIRMADO", "CANCELADO", "COMPLETADO", "AUSENTE"}
    assert [t["inicio"] for t in turnos] == sorted(t["inicio"] for t in turnos)  # la API los entrega ordenados
    assert all(set(t) == set(main.TurnoOut.model_fields) for t in turnos)

    with pytest.raises(SystemExit, match="Ya hay 15 turnos"):  # sin --reset no pisa datos existentes
        _correr_seed(monkeypatch)
    _correr_seed(monkeypatch, "--reset")
    assert len(listar(client)) == 15
