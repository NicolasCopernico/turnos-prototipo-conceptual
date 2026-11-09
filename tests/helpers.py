"""Constantes y utilidades compartidas por los módulos de prueba (no contiene tests)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient

import main

REAL_DB = main.DB_PATH  # ruta de la base real, capturada antes de que cualquier fixture la redirija

# Instante fijo y alineado a la grilla de 15 min. «Mañana» = AHORA + 1 día.
AHORA = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)

# Ids de la semilla de main.py: c1 y c3 son clientes con contacto válido y email verificado;
# s1 es «Consulta general» (30 min) de la profesional p1.
CLIENTE_1, CLIENTE_3, SERVICIO_1, PROFESIONAL_1 = "c1", "c3", "s1", "p1"


class Reloj:
    """Reloj inyectable: reemplaza a `main.utcnow` durante el test."""

    def __init__(self, ahora: datetime) -> None:
        self.ahora = ahora

    def __call__(self) -> datetime:
        return self.ahora


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
