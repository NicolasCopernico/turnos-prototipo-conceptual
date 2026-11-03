"""Carga en turnos.db los datos de ejemplo del prototipo index.html (función makeInitialState).

Uso (desde esta carpeta, con el entorno de main.py):

    python seed_mock.py              # usuarios, servicios, horarios, bloqueos y 15 turnos
    python seed_mock.py --ventana    # además, 3 turnos cercanos a "ahora" para probar INV-01

Es idempotente: se puede ejecutar varias veces sin duplicar datos. Las fechas son relativas al día
en que se ejecuta por primera vez: los turnos pasados caen en los 3 días hábiles anteriores a hoy
y los futuros, en los días hábiles siguientes. Para regenerarlas, borrá turnos.db y volvé a correr.

Reutiliza los identificadores fijos de main.py (Ana, Bruno, Lucía, Martín, admin y sus servicios)
y actualiza sus nombres a los de index.html. Se conecta con ``main.connect()``, así que respeta
la variable de entorno TURNOS_DB.
"""

import sqlite3
import sys
import uuid
from datetime import date, datetime, time, timedelta

import main
from main import OPERATION_TZ, fmt_utc

# Identificadores del prototipo -> UUID. Los cinco primeros usuarios y los tres primeros servicios
# son los de main.py; el resto es nuevo.
U = {
    "admin": main.ADMIN_ID,
    "p1": main.PROFESIONAL_ANA_ID,
    "p2": main.PROFESIONAL_BRUNO_ID,
    "p3": "bbbbbbbb-0000-4000-8000-000000000003",
    "c1": main.CLIENTE_LUCIA_ID,
    "c2": main.CLIENTE_MARTIN_ID,
    "c3": "cccccccc-0000-4000-8000-000000000003",
}
S = {
    "s1": main.SERVICIO_CONSULTA_ID,
    "s2": main.SERVICIO_CONTROL_ID,
    "s3": main.SERVICIO_CORTE_ID,
    "s4": "dddddddd-0000-4000-8000-000000000004",
    "s5": "dddddddd-0000-4000-8000-000000000005",
    "s6": "dddddddd-0000-4000-8000-000000000006",
}
NS = uuid.UUID("5eed5eed-0000-4000-8000-0000000000aa")

# (clave, rol, nombre, email, teléfono, email verificado)
USERS = (
    ("admin", "ADMIN", "Admin General", "admin@turnospro.test", "+5491100000000", 1),
    ("p1", "PROFESIONAL", "Dra. Ana Martínez", "ana@turnospro.test", "+5491100000001", 1),
    ("p2", "PROFESIONAL", "Dr. Bruno Salas", "bruno@turnospro.test", "+5491100000002", 1),
    ("p3", "PROFESIONAL", "Lic. Carla Ibáñez", "carla@turnospro.test", "+5491100000003", 1),
    ("c1", "CLIENTE", "Lucía Fernández", "lucia@mail.test", "+5491155550101", 1),
    ("c2", "CLIENTE", "Martín Gómez", "martin@mail.test", "+5491155550102", 0),  # email sin verificar
    ("c3", "CLIENTE", "Sofía Ruiz", "sofia@mail.test", "+5491155550103", 1),
)
# (clave, profesional, nombre, descripción, minutos, precio)
SERVICES = (
    ("s1", "p1", "Consulta general", "Evaluación clínica y orientación.", 30, 18000),
    ("s2", "p1", "Control anual", "Chequeo completo con estudios básicos.", 60, 32000),
    ("s3", "p2", "Sesión de terapia", "Sesión individual.", 45, 25000),
    ("s4", "p2", "Evaluación inicial", "Primera entrevista y plan de trabajo.", 60, 30000),
    ("s5", "p3", "Asesoría nutricional", "Seguimiento y ajustes.", 30, 15000),
    ("s6", "p3", "Plan alimentario", "Diseño de plan personalizado.", 45, 22000),
)
# Lunes a viernes, en minutos desde medianoche (como `schedules` en index.html)
SCHEDULES = {
    "p1": ((9 * 60, 13 * 60), (14 * 60, 18 * 60)),
    "p2": ((10 * 60, 14 * 60), (15 * 60, 19 * 60)),
    "p3": ((9 * 60, 13 * 60),),
}

# (clave, cliente, servicio, día relativo, hora, minuto, estado, cancelado por, motivo)
# Día relativo: negativo = días hábiles antes de hoy; 0 = primer día hábil después de hoy.
APPOINTMENTS = (
    ("t1", "c1", "s1", -3, 9, 0, "COMPLETADO", None, None),
    ("t2", "c3", "s2", -3, 10, 0, "COMPLETADO", None, None),
    ("t3", "c1", "s3", -2, 10, 0, "COMPLETADO", None, None),
    ("t4", "c3", "s5", -2, 11, 0, "AUSENTE", None, None),
    ("t5", "c2", "s1", -1, 9, 30, "COMPLETADO", None, None),
    ("t6", "c1", "s4", -1, 15, 0, "CANCELADO", "c1", None),
    ("t7", "c3", "s6", -1, 10, 0, "CANCELADO", "p3", "Imprevisto médico"),
    ("t8", "c1", "s1", 0, 9, 30, "CONFIRMADO", None, None),
    ("t9", "c3", "s5", 0, 10, 30, "CONFIRMADO", None, None),
    ("t10", "c2", "s3", 0, 11, 0, "CONFIRMADO", None, None),
    ("t11", "c1", "s2", 0, 15, 0, "CONFIRMADO", None, None),
    ("t12", "c3", "s1", 1, 10, 0, "CONFIRMADO", None, None),
    ("t13", "c1", "s4", 1, 11, 0, "CONFIRMADO", None, None),
    ("t14", "c2", "s5", 2, 9, 0, "CONFIRMADO", None, None),
    ("t15", "c1", "s2", 3, 15, 30, "CONFIRMADO", None, None),
)
# (clave, profesional, día relativo, desde, hasta, motivo)
BLOCKS = (
    ("b1", "p1", 1, (12, 0), (13, 0), "Trámite personal"),
    ("b2", "p2", 2, (10, 0), (19, 0), "Capacitación"),
)


def business_day(start: date, offset: int) -> date:
    """Día hábil (lunes a viernes) a ``offset`` días hábiles de ``start`` (sin contarlo)."""
    step = 1 if offset > 0 else -1
    current = start
    for _ in range(abs(offset)):
        current += timedelta(days=step)
        while current.weekday() >= 5:
            current += timedelta(days=step)
    return current


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=OPERATION_TZ)


def relative_day(today: date, rel: int) -> date:
    return business_day(today, rel if rel < 0 else rel + 1)


def seed_people_and_catalog(conn: sqlite3.Connection, now: datetime) -> None:
    for key, role, name, email, phone, verified in USERS:
        conn.execute(
            "INSERT INTO usuario (id, email, email_verificado, password_hash, nombre_completo, telefono, rol, activo,"
            " creado_en) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)"
            " ON CONFLICT (id) DO UPDATE SET email = excluded.email, email_verificado = excluded.email_verificado,"
            " nombre_completo = excluded.nombre_completo, telefono = excluded.telefono, rol = excluded.rol",
            (U[key], email, verified, main.NO_LOGIN_HASH, name, phone, role, fmt_utc(now)),
        )
    for key, prof, name, description, minutes, price in SERVICES:
        conn.execute(
            "INSERT INTO servicio (id, profesional_id, nombre, descripcion, duracion_min, precio_referencia, activo)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)"
            " ON CONFLICT (id) DO UPDATE SET nombre = excluded.nombre, descripcion = excluded.descripcion,"
            " duracion_min = excluded.duracion_min, precio_referencia = excluded.precio_referencia, activo = 1",
            (S[key], U[prof], name, description, minutes, price),
        )
    for prof, ranges in SCHEDULES.items():
        conn.execute("DELETE FROM horario_laboral WHERE profesional_id = ?", (U[prof],))  # reemplaza el horario base
        for weekday in range(5):
            for start_min, end_min in ranges:
                conn.execute(
                    "INSERT INTO horario_laboral (id, profesional_id, dia_semana, hora_inicio, hora_fin)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid5(NS, f"{prof}-{weekday}-{start_min}")),
                        U[prof],
                        weekday,
                        f"{start_min // 60:02d}:{start_min % 60:02d}",
                        f"{end_min // 60:02d}:{end_min % 60:02d}",
                    ),
                )


def seed_blocks(conn: sqlite3.Connection, today: date) -> None:
    for key, prof, rel, (h1, m1), (h2, m2), reason in BLOCKS:
        day = relative_day(today, rel)
        conn.execute(
            "INSERT OR IGNORE INTO bloqueo (id, profesional_id, inicio, fin, motivo) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid5(NS, key)), U[prof], fmt_utc(local(day, h1, m1)), fmt_utc(local(day, h2, m2)), reason),
        )


def insert_turno(
    conn: sqlite3.Connection, turno_id: str, client: str, service: str, start: datetime, state: str,
    canceled_by: str | None, reason: str | None, created: datetime, updated: datetime,
) -> bool:
    """Inserta el turno y sus eventos de auditoría. Devuelve False si la base lo rechaza (p. ej. solapamiento)."""
    row = conn.execute("SELECT profesional_id, duracion_min FROM servicio WHERE id = ?", (S[service],)).fetchone()
    end = start + timedelta(minutes=int(row["duracion_min"]))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO turno (id, cliente_id, profesional_id, servicio_id, inicio, fin, estado,"
            " motivo_cancelacion, cancelado_por_id, creado_en, actualizado_en) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (turno_id, U[client], row["profesional_id"], S[service], fmt_utc(start), fmt_utc(end), state, reason,
             U[canceled_by] if canceled_by else None, fmt_utc(created), fmt_utc(updated)),
        )
    except sqlite3.IntegrityError:
        return False
    if conn.execute("SELECT 1 FROM auditoria_evento WHERE entidad_id = ?", (turno_id,)).fetchone() is None:
        client_row = main.fetch_user(conn, uuid.UUID(U[client]))
        assert client_row is not None
        main.audit(conn, turno_id, "TURNO_CREADO", client_row, created, f"inicio={fmt_utc(start)}; fin={fmt_utc(end)}")
        if state == "CANCELADO" and canceled_by:
            actor = main.fetch_user(conn, uuid.UUID(U[canceled_by]))
            assert actor is not None
            main.audit(conn, turno_id, "TURNO_CANCELADO", actor, updated, reason or "")
    return True


def seed_appointments(conn: sqlite3.Connection, now: datetime, today: date) -> list[str]:
    skipped: list[str] = []
    for key, client, service, rel, hour, minute, state, canceled_by, reason in APPOINTMENTS:
        start = local(relative_day(today, rel), hour, minute)
        created = min(now - timedelta(days=5), start - timedelta(days=1))
        if state == "CANCELADO":
            updated = start - timedelta(days=1) + timedelta(hours=1)
        elif state in ("COMPLETADO", "AUSENTE"):
            updated = start + timedelta(minutes=60)
        else:
            updated = created
        if not insert_turno(conn, str(uuid.uuid5(NS, key)), client, service, start, state, canceled_by, reason,
                            created, updated):
            skipped.append(key)
    return skipped


def seed_window_cases(conn: sqlite3.Connection, now: datetime) -> list[tuple[str, str, str, str]]:
    """Turnos relativos al reloj real para probar INV-01 sin esperar. Se crean nuevos en cada ejecución."""
    base = now.replace(second=0, microsecond=0)
    cases = (
        ("c1", "s1", timedelta(hours=3), "a 3 h: se puede cancelar"),
        ("c3", "s3", timedelta(hours=1, minutes=59), "a 1 h 59 min: CANCELLATION_WINDOW_EXPIRED"),
        ("c1", "s5", timedelta(hours=2, minutes=10), "a 2 h 10 min: se puede cancelar durante ~10 min"),
    )
    created: list[tuple[str, str, str, str]] = []
    for client, service, delta, label in cases:
        turno_id = str(uuid.uuid4())
        if insert_turno(conn, turno_id, client, service, base + delta, "CONFIRMADO", None, None, now, now):
            created.append((turno_id, U[client], fmt_utc(base + delta), label))
    return created


def main_cli(argv: list[str]) -> None:
    now = main.now_utc()
    today = now.astimezone(OPERATION_TZ).date()
    main.init_db()
    conn = main.connect()
    try:
        with main.transaction(conn):
            seed_people_and_catalog(conn, now)
            seed_blocks(conn, today)
            skipped = seed_appointments(conn, now, today)
            window = seed_window_cases(conn, now) if "--ventana" in argv else []
        total = conn.execute("SELECT count(*) FROM turno").fetchone()[0]
    finally:
        conn.close()

    print(f"Listo. Base: {main.db_path()}  ·  turnos en la base: {total}")
    if skipped:
        print(f"Omitidos por conflicto con turnos ya existentes: {', '.join(skipped)}")
    print("\nUsuarios (para cliente_id / cancelado_por_id):")
    for key, role, name, *_ in USERS:
        print(f"  {role:<11} {name:<20} {U[key]}")
    print("\nServicios (para servicio_id):")
    for key, prof, name, _, minutes, _price in SERVICES:
        print(f"  {name:<20} {minutes:>2} min  {S[key]}")
    for turno_id, client_id, start, label in window:
        print(f"\nCaso INV-01 {label}\n  turno {turno_id}  inicio {start}\n  cancelado_por_id: {client_id}")


if __name__ == "__main__":
    main_cli(sys.argv[1:])
