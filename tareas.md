# tareas.md — Tareas por fase para ejecutar [plan.md](plan.md)

| Campo | Valor |
|---|---|
| Versión | 0.2.0 |
| Base normativa | [spec.md](spec.md) v0.2.0 · [plan.md](plan.md) v0.2.0 |
| Fecha | 2026-11-03 |

Leyenda: `[ ]` pendiente · `[x]` hecho · **Salida** = entregable de la fase · **Hecho cuando** = criterio de cierre.
Cada fase depende de la anterior. Las referencias (`RN-xx`, `INV-xx`, `ERR-xx`, `HU-xx`) apuntan a [spec.md](spec.md) y [historias-de-usuario.md](historias-de-usuario.md).

El archivo tiene dos partes:

- **Parte B — Backend desacoplado (Fase 2 del curso).** Es el trabajo actual.
- **Parte A — Prototipo en memoria (Fase 1 del curso).** Se conserva como registro. Sus fases 0 a 8 no cambian.

---

# Parte B — Backend desacoplado (`main.py`)

## Desviaciones del pedido respecto del spec

El pedido para `main.py` fija algunos nombres que **no coinciden** con `spec.md` v0.2.0. Como el spec es la única fuente de verdad y el plan prohíbe inventar tablas o campos (plan §3), `main.py` sigue el spec:

| Pedido | Spec v0.2.0 | Se implementa |
|---|---|---|
| Tabla `turnos` con `id`, `cliente_nombre`, `cliente_email`, `servicio`, `fecha_hora`, `estado` | `Turno` tiene `id`, `cliente_id`, `profesional_id`, `servicio_id`, `inicio`, `fin`, `estado`, `motivo_cancelacion`, `cancelado_por_id`, `creado_en`, `actualizado_en`. Nombre y email del cliente viven en `Usuario`; `servicio` es la entidad `Servicio` | Tabla `turno` con las columnas del spec (plan §1.3) |
| Error `HORARIO_NO_DISPONIBLE` | `SLOT_UNAVAILABLE` (§7) | `SLOT_UNAVAILABLE`, HTTP 409 |
| Error `CANCELACION_FUERA_DE_VENTANA` | `CANCELLATION_WINDOW_EXPIRED` (§7) | `CANCELLATION_WINDOW_EXPIRED`, HTTP 409 |
| Estado `CANCELADO_CLIENTE` | `CANCELADO` (RN-06); quién canceló queda en `cancelado_por_id` | `CANCELADO` |
| Cuerpo del `POST` con nombre, email, servicio y fecha | Cuerpo: `cliente_id`, `servicio_id`, `inicio` (§11.4) | Cuerpo del spec |

Se respetan sin cambios: CORS con `allow_origins=["*"]`, SQLite nativo en `turnos.db`, creación automática de tablas al arrancar, los 3 endpoints, los códigos HTTP 200/201/409, la regla INV-02 en el servidor y la regla INV-01 con 2 h exactas como límite inclusivo.

## Fase B0 — Preparación

- [x] B0.1 Actualizar `tareas.md` antes de escribir código (este documento).
- [x] B0.2 Registrar las desviaciones del pedido respecto del spec (tabla anterior) y avisar al usuario.
- [x] B0.3 Entorno Python aislado con `fastapi`, `uvicorn`, `pydantic`, y como herramientas de prueba `httpx` y `mypy`.
- [x] B0.4 `requirements.txt` con las dependencias de ejecución.

**Salida:** entorno que arranca `uvicorn` sin errores de importación.
**Hecho cuando:** `python -c "import fastapi, uvicorn"` funciona en el entorno.

## Fase B1 — Esquema y datos iniciales (capa 3)

- [x] B1.1 Crear las 6 tablas de plan §1.3 (`usuario`, `servicio`, `turno`, `horario_laboral`, `bloqueo`, `auditoria_evento`) con las columnas del spec §4 y ninguna más.
- [x] B1.2 `CHECK` de enums y rangos: `rol`, `estado`, booleanos 0/1, `duracion_min` 15–480, `precio_referencia` ≥ 0, `dia_semana` 0–6, `fin > inicio`, `cliente_id <> profesional_id`.
- [x] B1.3 Claves foráneas sin cascada sobre `turno`; `PRAGMA foreign_keys = ON`, `journal_mode = WAL`, `synchronous = FULL`.
- [x] B1.4 Triggers: INV-02 (sin solapamiento por profesional, en alta y en actualización), `turno.profesional_id = servicio.profesional_id` (spec §4.3), sin `DELETE` sobre `turno` (RN-06) y auditoría inmutable (RN-08).
- [x] B1.5 Creación automática del esquema al arrancar, idempotente.
- [x] B1.6 Carga inicial idempotente de `usuario`, `servicio`, `horario_laboral` y `bloqueo` con identificadores fijos y documentados, porque la API no expone esas entidades (D-07).

**Salida:** `turnos.db` creado al primer arranque.
**Hecho cuando:** el esquema generado coincide columna por columna con plan §1.3.

## Fase B2 — Servidor (capa 2)

- [x] B2.1 Aplicación FastAPI con `CORSMiddleware` (`allow_origins=["*"]`).
- [x] B2.2 Conexión `sqlite3` por petición, con `BEGIN IMMEDIATE` para las escrituras y `busy_timeout`.
- [x] B2.3 Configuración: ruta de la base, ventana de cancelación (`CANCELLATION_WINDOW_HOURS`, default 2), antelación mínima 1 h, grilla 15 min, horizonte 60 días y zona horaria de operación.
- [x] B2.4 Errores de dominio con el cuerpo `{"error": {"codigo", "mensaje"}}` de spec §11.1.
- [x] B2.5 Modelos Pydantic de entrada y salida, con tipado estricto y sin campos prohibidos (`fin`, `profesional_id`, `estado`).

**Salida:** servidor que arranca y sirve `/docs`.
**Hecho cuando:** `mypy --strict` no reporta errores y `/docs` lista los 3 endpoints.

## Fase B3 — `GET /api/turnos`

- [x] B3.1 Listar turnos de SQLite ordenados por `inicio`, con filtros opcionales `cliente_id`, `profesional_id` y `estado` (spec §11.3).
- [x] B3.2 Respuesta `200` con arreglo JSON (vacío si no hay turnos).

**Hecho cuando:** devuelve los turnos creados por `POST` incluso después de reiniciar el servidor (spec §8.9).

## Fase B4 — `POST /api/turnos`

- [x] B4.1 Validar el cuerpo con Pydantic y rechazar campos prohibidos. Un `inicio` inválido responde `INVALID_TIME_RANGE`.
- [x] B4.2 Verificar existencia y actividad de cliente y servicio (`NOT_FOUND`).
- [x] B4.3 RN-03: contacto válido (`INVALID_CONTACT`) y email verificado (`EMAIL_NOT_VERIFIED`).
- [x] B4.4 RN-04: antelación mínima, grilla de 15 min, horizonte de 60 días y `fin = inicio + duracion_min`.
- [x] B4.5 INV-02 / RN-02 dentro de una sola transacción: solapamiento con otro turno `CONFIRMADO`, bloqueos, horario laboral y turnos del mismo cliente. Respuesta `SLOT_UNAVAILABLE` (409).
- [x] B4.6 Insertar con estado `CONFIRMADO`, registrar `auditoria_evento` y hacer `COMMIT` antes de responder `201` con cabecera `Location`.
- [x] B4.7 Fallo de escritura: `ROLLBACK` y `PERSISTENCE_ERROR` (500) sin detalles internos.

**Hecho cuando:** los criterios de spec §8.1, 8.2, 8.3 y 8.6 pasan contra la API.

## Fase B5 — `POST /api/turnos/{id}/cancelar`

- [x] B5.1 Resolver turno y actor. Turno ajeno para un `CLIENTE` responde `NOT_FOUND` (RBAC-02).
- [x] B5.2 Motivo obligatorio para `PROFESIONAL` y `ADMIN` (`INVALID_CONTACT`, 422, según spec §11.5).
- [x] B5.3 Solo se cancela un turno `CONFIRMADO` (`INVALID_STATE_TRANSITION`).
- [x] B5.4 INV-01 / RN-01: si el actor es `CLIENTE` y faltan menos de 2 h (o el turno ya inició), responde `CANCELLATION_WINDOW_EXPIRED` (409). Con 2 h exactas se permite.
- [x] B5.5 Actualizar a `CANCELADO` con `cancelado_por_id`, `motivo_cancelacion` y `actualizado_en`, registrar auditoría y hacer `COMMIT` antes de responder `200`.

**Hecho cuando:** los criterios de spec §8.4, 8.5, 8.7 y 8.11 pasan contra la API.

## Fase B6 — Verificación

- [x] B6.1 Pruebas contra la API real con `TestClient`: los 8 criterios aplicables de spec §8 y los criterios 9 a 11. _(63 comprobaciones, todas OK. El script de pruebas no se guardó en el repositorio.)_
- [x] B6.2 Prueba de reinicio: crear, descartar la aplicación, volver a crearla sobre el mismo `turnos.db` y listar (INV-03).
- [x] B6.3 Concurrencia real: dos `POST` simultáneos al mismo slot, exactamente uno `201` y el otro `409`.
- [x] B6.4 Atomicidad: forzar un fallo de escritura y comprobar que no queda un turno parcial (spec §8.10).
- [x] B6.5 Comparar `PRAGMA table_info` de cada tabla contra plan §1.3 (grounding, plan §3).
- [x] B6.6 `mypy --strict` sin errores.
- [x] B6.7 Arranque real con `uvicorn --reload` y una llamada por `curl` a `/docs` y a `GET /api/turnos`. _(Hecho con variación: se probó en el puerto 8765 porque en esta máquina el 8000 ya estaba ocupado por otro proceso.)_

**Salida:** tabla criterio ↔ resultado observado.
**Hecho cuando:** todo pasa, o las fallas están corregidas y vueltas a probar.

## Fase B7 — Entrega

- [x] B7.1 Entregar `main.py` completo.
- [x] B7.2 Entregar el comando de arranque con `--reload` y la URL de Swagger UI (`http://localhost:8000/docs`).
- [x] B7.3 Documentar los identificadores fijos de la carga inicial para poder probar desde Swagger.
- [x] B7.4 Completar la columna "Código" de la trazabilidad de spec §10.
- [x] B7.5 `seed_mock.py`: datos de ejemplo equivalentes a los de `index.html` (7 usuarios, 6 servicios, horarios, 2 bloqueos y 15 turnos con estados variados), más `--ventana` para probar INV-01.

**Salida:** backend ejecutable y documentado.
**Hecho cuando:** un usuario puede levantar el servidor, abrir `/docs` y recorrer los 3 endpoints.

```
B0 Preparación → B1 Esquema → B2 Servidor → B3 GET ┐
                                           → B4 POST crear ┼→ B6 Verificación → B7 Entrega
                                           → B5 POST cancelar ┘
```

---

# Parte A — Prototipo en memoria (Fase 1 del curso)

### Fase 0 — Preparación

- [x] 0.1 Confirmar con el usuario la aprobación del plan y las 5 decisiones por defecto (plan §7).
- [x] 0.2 Cargar la guía `artifact-design` (tokens en `:root`, modo oscuro, layout móvil, título de 2 a 4 palabras).
- [x] 0.3 Definir el archivo de trabajo del prototipo (`prototipo-turnos.html`) y las versiones fijas de React, Babel y Lucide. _(Hecho con variación: el archivo es `index.html` y Lucide va como SVG inline.)_
- [x] 0.4 Definir las constantes de configuración del spec: ventana 2 h, antelación 1 h, horizonte 60 días, grilla 15 min.

**Salida:** archivo vacío con el esqueleto HTML, CDNs y tokens de tema.
**Hecho cuando:** la página carga sin errores en claro y oscuro.

---

### Fase 1 — Dominio puro (`domain/`)

- [x] 1.1 Utilidades de tiempo: sumar minutos, alinear a la grilla, comparar intervalos `[inicio, fin)`.
- [x] 1.2 `overlaps(a, b)`: solapamiento con contigüidad válida (RN-02).
- [x] 1.3 `computeSlots(profesional, servicio, fecha, estado, ahora)`: respeta horario laboral, turnos activos, bloqueos, antelación mínima, horizonte y que el servicio entre antes del fin de jornada (HU-01, EC-01.1 a EC-01.4).
- [x] 1.4 `validateContact(usuario)`: nombre, email, teléfono, email verificado (RN-03).
- [x] 1.5 `canClientCancel(turno, ahora)`: ventana de 2 h con límite exacto inclusivo (RN-01, EC-04.2).
- [x] 1.6 `transition(turno, accion, actor)`: tabla de transiciones y estados terminales (RN-06). _(Hecho con variación: sin función `transition`; la tabla de transiciones está repartida en `completeAppointment`, `markNoShow` y `applyCancel`.)_
- [x] 1.7 `bookAppointment`, `cancelAppointment`, `completeAppointment`, `markNoShow`, `createBlock`, `removeBlock`: cada una devuelve `{ok, estado}` o `{error: CODIGO}` usando el catálogo del spec §7.
- [x] 1.8 `createBlock` con detección de conflictos: lista los turnos afectados y, con confirmación, cancela y bloquea en una sola operación (RN-05, EC-06.2).
- [x] 1.9 Generación de eventos de auditoría y de correos simulados (RN-08, F-13).
- [ ] 1.10 Autoverificación en consola: casos de prueba rápidos de las funciones puras (solapamiento parcial, contiguo, límite 2 h, bloqueo).

**Salida:** módulo de dominio sin dependencias de React.
**Hecho cuando:** los casos de 1.10 pasan en la consola del navegador.

---

### Fase 2 — Datos mock y estado

- [x] 2.1 Semillas relativas a "hoy": 3 profesionales, 5 o 6 servicios (30, 45, 60 min), horario de lunes a viernes.
- [x] 2.2 Dos clientes, uno con email sin verificar (para `EMAIL_NOT_VERIFIED`) y un `ADMIN`.
- [x] 2.3 Unos 12 turnos con estados variados, incluyendo uno a 1 h 59 min y otro a 3 h de "ahora".
- [x] 2.4 1 o 2 bloqueos de ejemplo.
- [x] 2.5 Reloj simulado en el estado (`ahora`), con acciones `+1 h`, `+1 día` y reinicio.
- [x] 2.6 Reducer centralizado que invoca las funciones de la Fase 1 y devuelve errores de dominio sin mutar el estado. _(Hecho con variación: lo cubre `act()` con funciones de dominio puras, sin reducer.)_
- [x] 2.7 `Context` con `dispatch`, usuario activo, rol activo y cola de toasts.
- [x] 2.8 Acción "Reiniciar datos".

**Salida:** `store` funcional con una sola fuente de verdad.
**Hecho cuando:** despachar acciones desde la consola cambia el estado y respeta las reglas.

---

### Fase 3 — Componentes compartidos (`ui/`)

- [x] 3.1 Barra superior: selector Cliente/Administrador, selector de cliente activo, selector Profesional/ADMIN en vista Administrador, reloj simulado y botones de tiempo.
- [x] 3.2 Badges de estado (`CONFIRMADO`, `CANCELADO`, `COMPLETADO`, `AUSENTE`) con color y texto, no solo color.
- [x] 3.3 Modal reutilizable accesible (foco, `Esc`, etiquetas).
- [x] 3.4 Toasts para éxito y para errores de dominio con su código.
- [x] 3.5 Panel lateral colapsable de "Bandeja simulada" (emails de confirmación y cancelación).
- [x] 3.6 Layout responsive con gutters de 16 px y sin scroll horizontal.

**Salida:** estructura de la aplicación con navegación por rol.
**Hecho cuando:** alternar el rol cambia la vista y la barra muestra el contexto correcto.

---

### Fase 4 — Vista Cliente (Flujo 1)

- [x] 4.1 Selector de servicio, profesional y fecha (limitado al horizonte de 60 días) (HU-01).
- [x] 4.2 Grilla de slots libres calculada por `computeSlots`, con estado vacío cuando no hay.
- [x] 4.3 Modal de confirmación con resumen y formulario de contacto editable con validación (HU-02, RN-03).
- [x] 4.4 Envío de la reserva por el reducer, con deshabilitado del botón durante el envío para evitar doble clic (ERR-02.3).
- [x] 4.5 Pantalla de confirmación con servicio, profesional, inicio y fin, y aviso de la política de cancelación (HU-03).
- [x] 4.6 "Mis turnos": próximos e historial, solo del cliente activo (RBAC-02).
- [x] 4.7 Botón Cancelar deshabilitado con tooltip a menos de 2 h, y mensaje `CANCELLATION_WINDOW_EXPIRED` si se fuerza (HU-04).
- [x] 4.8 Mensajes claros para `SLOT_UNAVAILABLE`, `INVALID_CONTACT`, `EMAIL_NOT_VERIFIED`, `INVALID_TIME_RANGE`, y acción "Reenviar verificación".

**Salida:** flujo completo de reserva, confirmación y cancelación del cliente.
**Hecho cuando:** un cliente reserva, ve el turno en "Mis turnos" y el slot desaparece de la grilla.

---

### Fase 5 — Vista Administrador (Flujo 2)

- [x] 5.1 Agenda día/semana y vista de lista, filtrada por profesional según el rol (PROFESIONAL ve lo suyo, ADMIN ve todo) (HU-05).
- [x] 5.2 Detalle del turno con datos de contacto del cliente (RBAC-04).
- [x] 5.3 Acciones sobre turnos: cancelar (modal con motivo obligatorio), completar (desde `inicio`), marcar ausente (desde `fin`), con habilitación según el estado (HU-07, RN-06).
- [x] 5.4 Gestión de bloqueos: crear, listar, quitar; validación de rango y de pasado (HU-06, EC-06.4).
- [x] 5.5 Flujo de conflicto de bloqueo: lista los turnos afectados y pide confirmación con motivo antes de cancelarlos (ERR-06.1, EC-06.2).
- [x] 5.6 Métricas: turnos por período, tasa de cancelación con desglose por quién canceló, tasa de ausentismo, ocupación y top servicios (spec §5).
- [x] 5.7 Panel de auditoría de eventos (RN-08).
- [x] 5.8 Permisos: un PROFESIONAL no puede actuar sobre la agenda ajena; muestra el resultado de RBAC-02.

**Salida:** gestión completa de la agenda por el Administrador.
**Hecho cuando:** una cancelación o bloqueo hecho aquí se refleja de inmediato en la vista Cliente.

---

### Fase 6 — Integración y pulido

- [ ] 6.1 Verificar que las acciones de una vista impactan en la otra sin recargar (requisito 5).
- [ ] 6.2 Revisar el contraste, el foco por teclado, las etiquetas y los estados vacíos.
- [ ] 6.3 Revisar modo oscuro y ancho móvil (375 px).
- [ ] 6.4 Eliminar logs de depuración y código muerto; confirmar que la UI no contiene lógica de reglas.

**Salida:** prototipo estable y consistente.
**Hecho cuando:** ningún error en consola y navegación completa sin pasos rotos.

---

### Fase 7 — Verificación contra el spec

Probar en el navegador integrado y registrar el resultado observado:

- [ ] 7.1 Doble reserva: solapamiento parcial devuelve `SLOT_UNAVAILABLE` (spec §8.1, ERR-02.1).
- [x] 7.2 Concurrencia simulada: dos clientes sobre el mismo slot, solo uno se confirma (spec §8.2, EC-02.2).
- [x] 7.3 Contigüidad: 10:00–10:30 y 10:30–11:00 se aceptan (spec §8.3).
- [x] 7.4 Cancelación dentro de ventana (3 h) aceptada y slot liberado (spec §8.4).
- [x] 7.5 Cancelación a 1 h 59 min rechazada con `CANCELLATION_WINDOW_EXPIRED` (spec §8.5).
- [ ] 7.6 Contacto inválido y email sin verificar bloquean la reserva (spec §8.6, ERR-02.4, ERR-02.5).
- [ ] 7.7 Aislamiento entre clientes en "Mis turnos" (spec §8.7).
- [ ] 7.8 Bloqueo con conflicto lista los turnos y no se crea sin confirmar (spec §8.8, ERR-06.1).
- [ ] 7.9 Administrador cancela fuera de ventana con motivo y notifica al cliente (HU-07).
- [ ] 7.10 Avance del reloj: transiciones a `COMPLETADO`/`AUSENTE` y estados terminales no cancelables (ERR-07.2, ERR-07.3).
- [ ] 7.11 Doble clic en reservar no duplica turnos ni correos (ERR-02.3, EC-03.3).

**Salida:** tabla escenario ↔ resultado observado (pasa / falla).
**Hecho cuando:** todos los escenarios pasan, o las fallas están corregidas y vueltas a probar.

---

### Fase 8 — Publicación y cierre

- [x] 8.1 Publicar el Artifact con título corto, icono genérico y descripción de una oración.
- [ ] 8.2 Entregar el link y la tabla de la Fase 7.
- [x] 8.3 Listar las desviaciones o límites conocidos respecto del spec (por ejemplo, Q-01 fuera de alcance).
- [ ] 8.4 Completar la columna "Código" de la trazabilidad del spec §10.

**Salida:** link del Artifact y reporte de verificación.
**Hecho cuando:** el usuario puede abrir el prototipo y recorrer ambos flujos.

---

### Resumen de dependencias

```
F0 Preparación → F1 Dominio → F2 Estado → F3 UI compartida → F4 Cliente ┐
                                                           → F5 Admin   ┴→ F6 Integración → F7 Verificación → F8 Publicación
```

F4 y F5 pueden avanzar en paralelo una vez cerrada la Fase 3, porque ambas consumen el mismo `store` y no se tocan entre sí.
