"""Fixtures compartidas: base SQLite aislada, reloj inyectable y cliente HTTP de la app.

Cómo se garantiza el aislamiento (nunca se toca `turnos.db`):
  * cada test recibe su propia base SQLite en `tmp_path` (copia de una plantilla con la semilla; se redirige `main.DB_PATH`);
  * la fixture `client` ejecuta el `lifespan` real de la app sobre esa base;
  * `test_aislamiento_no_toca_turnos_db_real` (tests/test_turnos.py) lo comprueba de forma explícita.

El reloj también está aislado: se reemplaza `main.utcnow` por un reloj controlable (ver `helpers.Reloj`).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

import main
from helpers import AHORA, REAL_DB, Reloj


@pytest.fixture
def reloj(monkeypatch: pytest.MonkeyPatch) -> Reloj:
    r = Reloj(AHORA)
    monkeypatch.setattr(main, "utcnow", r)
    return r


@pytest.fixture(scope="session")
def plantilla_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Base temporal con esquema y semilla, creada UNA vez (con `main.init_db()`, el mismo código de producción).

    Solo se usa como molde: cada test trabaja sobre su propia copia. Evita repetir las escrituras de la semilla
    (con fsync) en cada test y es lo que mantiene la suite rápida.
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

    `main.conectar()` lee `main.DB_PATH` en cada llamada, así que alcanza con redirigirla.
    """
    ruta = tmp_path / "turnos_test.db"
    assert ruta != REAL_DB
    shutil.copyfile(plantilla_db, ruta)
    monkeypatch.setattr(main, "DB_PATH", ruta)
    return ruta


@pytest.fixture
def client(db_path: Path, reloj: Reloj) -> Iterator[TestClient]:
    with TestClient(main.app) as c:  # el lifespan de la app abre la base de `db_path`
        yield c
