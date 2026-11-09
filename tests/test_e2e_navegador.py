"""Recorrido del frontend en un navegador real contra el backend vivo (spec.md §14, escenario 15). OPCIONAL.

A diferencia del resto de la suite, abre Chrome y ejecuta el JavaScript real de `prototipo-turnos.html`. Es la única
prueba que detecta errores de ejecución del frontend (por ejemplo, una variable sin definir al reservar).

Requisitos: Node.js, Chrome o Chromium, conexión a internet (la página carga React y Tailwind por CDN) y, una vez:
    cd tests/e2e && npm install

No corre con `pytest` a secas (tarda cerca de un minuto). Para ejecutarla:
    pytest -m e2e -v

Usa una base temporal y un puerto libre: nunca toca `turnos.db` ni el servidor de desarrollo.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

pytestmark = pytest.mark.e2e

RAIZ = Path(__file__).resolve().parent.parent
E2E = Path(__file__).resolve().parent / "e2e"


def _chrome() -> str | None:
    candidato = os.environ.get("CHROME_PATH")
    if candidato and Path(candidato).exists():
        return candidato
    for nombre in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        ruta = shutil.which(nombre)
        if ruta:
            return ruta
    return None


def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def servidor(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    if shutil.which("node") is None:
        pytest.skip("falta Node.js")
    if not (E2E / "node_modules" / "puppeteer-core").exists():
        pytest.skip("falta instalar las dependencias: cd tests/e2e && npm install")
    if _chrome() is None:
        pytest.skip("falta Chrome/Chromium (o definí CHROME_PATH)")

    carpeta = tmp_path_factory.mktemp("e2e")
    entorno = {**os.environ, "TURNOS_DB": str(carpeta / "e2e.db")}
    subprocess.run([sys.executable, "seed_demo.py"], cwd=RAIZ, env=entorno, check=True, capture_output=True)

    puerto = _puerto_libre()
    log = (carpeta / "uvicorn.log").open("w")
    proceso = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--port", str(puerto)],
                               cwd=RAIZ, env=entorno, stdout=log, stderr=subprocess.STDOUT)
    try:
        limite = time.time() + 30
        while time.time() < limite:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{puerto}/api/turnos", timeout=1).read()
                break
            except OSError:
                time.sleep(0.3)
        else:
            raise RuntimeError("el backend de prueba no arrancó:\n" + (carpeta / "uvicorn.log").read_text())
        yield puerto
    finally:
        proceso.terminate()
        proceso.wait(timeout=10)
        log.close()


def test_15_recorrido_completo_del_frontend_en_un_navegador_real(servidor: int) -> None:
    entorno = {**os.environ, "E2E_PORT": str(servidor), "CHROME_PATH": _chrome() or ""}

    r = subprocess.run(["node", "run.js"], cwd=E2E, env=entorno, capture_output=True, text=True, timeout=300)

    assert r.returncode == 0, f"El recorrido en el navegador falló:\n{r.stdout}\n{r.stderr}"
    assert "comprobaciones OK" in r.stdout, r.stdout
