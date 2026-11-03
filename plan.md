# plan.md — Arquitectura desacoplada de 3 capas (Fase 2)

| Campo | Valor |
|---|---|
| Versión | 0.2.0 |
| Base normativa | [spec.md](spec.md) v0.2.0 (INV-01, INV-02, INV-03 y sección 11) |
| Fecha | 2026-10-27 |
| Estado | Pendiente de aprobación |
| Versión anterior | 0.1.0: plan del prototipo en memoria (Fase 1). Se conserva íntegro en el [Apéndice A](#apéndice-a--plan-de-la-fase-1-v010-histórico) |

> Este plan no introduce entidades, tablas ni campos propios. Todo lo que se persiste o se expone sale de la sección 4 de [spec.md](spec.md). Si algo hace falta y no figura allí, primero se versiona la spec.

## 1. Arquitectura

```
┌─────────────────────────┐   HTTP + JSON    ┌─────────────────────────┐   SQL    ┌─────────────────┐
│ Capa 1 · Frontend       │ ───────────────▶ │ Capa 2 · Backend        │ ───────▶ │ Capa 3 · Datos  │
│ prototipo-turnos.html   │ ◀─────────────── │ FastAPI · puerto 8000   │ ◀─────── │ SQLite          │
│ (sin reglas de negocio) │   CORS habilitado│ (reglas INV-01..03)     │          │ turnos.db       │
└─────────────────────────┘                  └─────────────────────────┘          └─────────────────┘
```

Cada capa solo conoce a la siguiente. El Frontend no accede a la base de datos y la base de datos no conoce a la API.

### 1.1 Capa 1 — Frontend (`prototipo-turnos.html`)

- Es la evolución del prototipo actual `index.html`: la misma interfaz, pero los datos llegan por HTTP en vez de vivir en el estado de JavaScript.
- Consume el contrato de [spec.md §11](spec.md#11-contrato-de-api-rest-fase-2) con `fetch` y JSON:
  - `GET http://localhost:8000/api/turnos`
  - `POST http://localhost:8000/api/turnos`
  - `POST http://localhost:8000/api/turnos/{id}/cancelar`
- Se elimina el módulo `domain/` y el reducer como autoridad. La pantalla muestra los códigos de error que devuelve el backend (`SLOT_UNAVAILABLE`, `CANCELLATION_WINDOW_EXPIRED`, etc.) y no decide qué está permitido (RBAC-01).
- Sigue siendo un solo archivo HTML, con React y Tailwind por CDN. No hay build.
- Si el backend no responde, muestra un error visible. No cae a datos en memoria, porque eso reabriría el problema de la sección 2.

### 1.2 Capa 2 — Backend (Python + FastAPI, puerto 8000)

- Expone los 3 endpoints de [spec.md §11](spec.md#11-contrato-de-api-rest-fase-2) y ninguno más (D-07).
- **CORS habilitado** (`CORSMiddleware`) para que `prototipo-turnos.html`, abierto con `file://` o desde otro puerto, pueda llamar a la API.
- Arranque: `uvicorn` en el puerto `8000`.
- Aquí viven las reglas de negocio:

| Regla | Dónde se aplica |
|---|---|
| INV-01 (RN-01): ventana de cancelación ≥ 2 h, con `CANCELLATION_WINDOW_HOURS` configurable | `POST /api/turnos/{id}/cancelar` |
| INV-02 (RN-02): sin solapamiento `[inicio, fin)` | `POST /api/turnos`, y reforzada en la base de datos |
| RN-03, RN-04: contacto válido, email verificado, `fin` calculado, grilla y horizonte | `POST /api/turnos` |
| RN-06: estado inicial `CONFIRMADO`, transición a `CANCELADO` | ambos `POST` |
| RN-08: registro en `auditoria_evento` por cada alta y cancelación | ambos `POST`, dentro de la misma transacción |
| INV-03: `COMMIT` antes de responder éxito | ambos `POST` |

- El backend toma "ahora" del reloj del sistema, en UTC.
- Los errores usan el cuerpo y los códigos de [spec.md §7 y §11.1](spec.md#111-convenciones).

### 1.3 Capa 3 — Persistencia (SQLite, `turnos.db`)

Base relacional local en el archivo `turnos.db`. **Solo existen las 6 entidades de [spec.md §4](spec.md#4-modelo-de-datos-conceptual)**, con los atributos de esa sección y ninguno más:

| Tabla | Columnas (copia de spec §4) |
|---|---|
| `usuario` | `id`, `email`, `email_verificado`, `password_hash`, `nombre_completo`, `telefono`, `rol`, `activo`, `creado_en` |
| `servicio` | `id`, `profesional_id`, `nombre`, `descripcion`, `duracion_min`, `precio_referencia`, `activo` |
| `turno` | `id`, `cliente_id`, `profesional_id`, `servicio_id`, `inicio`, `fin`, `estado`, `motivo_cancelacion`, `cancelado_por_id`, `creado_en`, `actualizado_en` |
| `horario_laboral` | `id`, `profesional_id`, `dia_semana`, `hora_inicio`, `hora_fin` |
| `bloqueo` | `id`, `profesional_id`, `inicio`, `fin`, `motivo` |
| `auditoria_evento` | `id`, `entidad`, `entidad_id`, `accion`, `actor_id`, `rol`, `ocurrido_en`, `detalle` |

Cómo se traducen los tipos conceptuales a SQLite (esto no agrega campos):

| Tipo de la spec | Almacenamiento en SQLite |
|---|---|
| UUID | `TEXT`, clave primaria |
| timestamp UTC | `TEXT` en ISO 8601 UTC |
| booleano | `INTEGER` 0/1 con `CHECK` |
| enum (`rol`, `estado`) | `TEXT` con `CHECK` sobre los valores de la spec |
| decimal | `NUMERIC` |

**Reglas de integridad que impone la base:**

- `PRAGMA foreign_keys = ON` en cada conexión. Las FK de [spec.md §4.3](spec.md#43-invariantes-de-integridad-referencial) no usan borrado en cascada sobre `turno`.
- `CHECK (cliente_id <> profesional_id)` en `turno` (spec §4.3).
- **INV-02 en la base de datos.** SQLite no tiene `EXCLUDE USING gist`. Se usa la alternativa que RN-02 admite ("transacción con bloqueo + índice único equivalente"):
  1. Cada alta corre en una transacción `BEGIN IMMEDIATE`, que serializa a los escritores.
  2. Triggers `BEFORE INSERT` y `BEFORE UPDATE` sobre `turno` abortan si existe otro turno `CONFIRMADO` del mismo `profesional_id` con `inicio < NEW.fin` y `NEW.inicio < fin`. El backend traduce el aborto a `SLOT_UNAVAILABLE`.
- Los turnos no se borran físicamente (RN-06). No hay `DELETE` sobre `turno`.

**Carga inicial (seed):** un script carga los datos de `usuario`, `servicio`, `horario_laboral` y `bloqueo` que las reglas necesitan, con los mismos personajes del prototipo (profesionales, clientes y un cliente con email sin verificar). Usa solo las columnas de la tabla anterior. Es idempotente: no duplica datos si `turnos.db` ya existe.

### 1.4 Cómo se cumple INV-03

| Exigencia de INV-03 | Mecanismo |
|---|---|
| Persistencia atómica | Cada `POST` es una transacción única: validar, insertar o actualizar el turno y escribir `auditoria_evento`. Ante cualquier error, `ROLLBACK` |
| Sobrevive al reinicio | Los datos están en el archivo `turnos.db`. Reiniciar `uvicorn` no los toca |
| Durabilidad ante corte | `PRAGMA journal_mode = WAL` y `PRAGMA synchronous = FULL` |
| `COMMIT` antes de responder | El handler devuelve `201` o `200` solo después del `COMMIT` |
| Sin estado solo en memoria | El backend no guarda turnos en variables de proceso. Cada petición lee de la base |
| Falla de persistencia | Responde `500 PERSISTENCE_ERROR` sin detalles internos |

## 2. Por qué se desacopla: el problema de la memoria volátil de JavaScript

El prototipo de la Fase 1 guarda todo en el estado de JavaScript dentro del navegador. Sirvió para validar la interfaz y las reglas, pero **viola la integridad del sistema** por estas razones:

1. **Incumple INV-03.** Al recargar la página o cerrar la pestaña, los turnos desaparecen y los datos vuelven a los de ejemplo. Una reserva confirmada que se pierde es una reserva rota para el cliente. El propio README lo lista como límite conocido.
2. **No hay una única fuente de verdad.** Cada pestaña y cada navegador tiene su propia copia. Dos clientes reales pueden reservar el mismo horario sin enterarse, porque ninguno ve el estado del otro.
3. **INV-02 no se puede garantizar.** La spec exige que la verificación sea atómica y respaldada por la base de datos (RN-02). La "concurrencia" del prototipo es simulada: el reducer procesa las acciones una detrás de otra dentro de un único proceso, algo que no ocurre con usuarios reales.
4. **La UI se convierte en la barrera de seguridad.** Las reglas corren en el navegador, donde cualquiera puede modificarlas o saltearlas desde la consola. Eso contradice RBAC-01: la UI nunca es la barrera de seguridad.
5. **INV-01 depende del reloj del cliente.** El cálculo `inicio - ahora` se hace con el reloj del navegador (o con el reloj simulado), que el usuario controla. La ventana de 2 h solo es confiable si el reloj es el del servidor.
6. **Sin transacciones ni recuperación.** Un error a mitad de una operación deja el estado en memoria a medias, sin `ROLLBACK`. INV-03 pide lo contrario: todo o nada.

El desacoplamiento resuelve esto separando responsabilidades: el Frontend solo presenta, el Backend decide y la base de datos recuerda y refuerza las invariantes. Cada capa puede cambiar sin tocar a las otras.

**Consecuencia asumida:** el reloj simulado del prototipo ya no sirve para probar INV-01, porque "ahora" lo define el servidor. Las pruebas de la ventana de 2 h se hacen creando turnos con `inicio` relativo al reloj real (por ejemplo, a 1 h 59 min y a 3 h), tal como describe el criterio de aceptación 5.

## 3. Grounding: reglas de abstención

- **Entidades permitidas:** `Usuario`, `Servicio`, `Turno`, `HorarioLaboral`, `Bloqueo` y `AuditoriaEvento`. No se crea ninguna otra.
- **Campos permitidos:** los de [spec.md §4.1](spec.md#41-entidades-principales). No se agregan columnas como `token`, `sesion`, `version`, `deleted_at` ni similares.
- **API:** solo los 3 endpoints de [spec.md §11](spec.md#11-contrato-de-api-rest-fase-2). Los campos del JSON son los de `Turno`.
- **Verificación del esquema:** antes de cerrar la fase se compara el `CREATE TABLE` de `turnos.db` contra la tabla de la sección 1.3. Cualquier diferencia se corrige en el código, o se versiona la spec primero.

## 4. Decisiones tomadas

| # | Tema | Decisión |
|---|---|---|
| 1 | Motor de base de datos (D-04) | **SQLite** en `turnos.db`, en lugar de PostgreSQL |
| 2 | Backend | **Python + FastAPI**, puerto `8000`, CORS habilitado |
| 3 | Refuerzo de INV-02 | `BEGIN IMMEDIATE` + triggers (sin `EXCLUDE USING gist`) |
| 4 | Identificación del actor (D-06) | Campos existentes de `Turno` (`cliente_id`, `cancelado_por_id`); sin login en esta fase |
| 5 | Alcance de la API (D-07) | Solo los 3 endpoints del contrato |
| 6 | Nombre del frontend | `prototipo-turnos.html`. `index.html` queda como el prototipo de la Fase 1 |

## 5. Puntos que requieren aprobación

- **D-06:** sin login, un cliente podría declarar el `cliente_id` de otro. Es aceptable en esta fase porque F-01 queda fuera, pero RBAC-02 solo se puede demostrar contra el actor declarado.
- **`profesional_id` no viaja en `POST /api/turnos`:** el backend lo deriva de `Servicio.profesional_id` (spec §4.3), para que no pueda contradecirse con el servicio elegido.

## 6. Verificación

- Los 8 escenarios originales de spec.md §8, aplicables a la API, más los criterios 9 a 11 (reinicio, atomicidad y persistencia de la cancelación).
- Prueba de reinicio: crear un turno, detener y volver a lanzar `uvicorn`, y confirmar con `GET /api/turnos` que el turno sigue ahí.
- Prueba de concurrencia real: dos `POST /api/turnos` simultáneos al mismo slot. Debe confirmarse exactamente uno y el otro recibir `SLOT_UNAVAILABLE`.
- Verificación del esquema (sección 3).

---

## Apéndice A — Plan de la Fase 1 (v0.1.0, histórico)

> Plan original del prototipo en memoria. Se conserva sin cambios de contenido como registro de la Fase 1. No rige la Fase 2.

| Campo | Valor |
|---|---|
| Base normativa | [spec.md](spec.md) v0.1.0 · [historias-de-usuario.md](historias-de-usuario.md) |
| Fecha | 2026-10-26 |
| Estado | Pendiente de aprobación |

### 1. Formato de entrega

- Un único archivo HTML publicado como Artifact. Dentro va un solo componente React con Tailwind (`cdn.tailwindcss.com`) y Lucide Icons.
- React, Babel y Lucide se cargan desde CDN con versión fija. No hay build ni dependencias locales.
- Antes de escribirlo se carga la guía de diseño de Artifacts: tokens de color, modo oscuro y layout responsive.
- El componente es modular dentro del mismo archivo, con una sección lógica por responsabilidad:

| Módulo | Contenido |
|---|---|
| `domain/` | Funciones puras del spec: solapamiento, ventana de cancelación, validación de contacto, cálculo de slots, transiciones de estado |
| `store` | Estado único con `useReducer` y un `Context`; un reducer centralizado donde se aplican todas las reglas |
| `ClientView` | Servicio, grilla de slots, formulario, "Mis turnos" |
| `AdminView` | Agenda, métricas, bloqueos, acciones sobre turnos |
| `ui/` | Componentes compartidos: toasts, modales, badges de estado, barra superior |

### 2. Decisión de diseño clave

Toda regla vive en `domain/` y el reducer la ejecuta. La UI nunca decide si algo está permitido: solo muestra el error que devuelve el dominio, con los mismos códigos del spec (`SLOT_UNAVAILABLE`, `CANCELLATION_WINDOW_EXPIRED`, etc.). Esto simula el principio RBAC-01 de que la UI no es la barrera de seguridad.

### 3. Reglas que se implementan

- **RN-01:** cancelación del cliente bloqueada a menos de 2 h del inicio. El Administrador la salta, pero con motivo obligatorio.
- **RN-02:** sin solapamiento con intervalos `[inicio, fin)`, contigüidad válida, y sin solapar con bloqueos ni con turnos propios del cliente.
- **RN-03:** nombre, email y teléfono válidos y email verificado antes de reservar.
- **RN-04:** grilla de 15 min, antelación mínima de 1 h, horizonte de 60 días, y `fin` calculado por el sistema.
- **RN-05:** bloquear un rango con turnos activos lista los conflictos y exige confirmar la cancelación.
- **RN-06:** estados terminales, sin borrado físico.
- **RN-08:** panel de auditoría visible para el Administrador.
- **RBAC:** el cliente solo ve sus turnos. Con el cambio de rol se ve en vivo qué aparece y qué desaparece.

### 4. Datos mock en memoria

- 3 profesionales, 5 a 6 servicios con duraciones distintas (30, 45, 60 min).
- 2 clientes. Uno de ellos tiene el email sin verificar, para poder probar `EMAIL_NOT_VERIFIED`.
- Unos 12 turnos distribuidos en los próximos días: confirmados, cancelados, completados y ausentes. Incluye uno que empieza dentro de 1 h 59 min y otro a las 3 h, para probar RN-01 sin esperar.
- 1 o 2 bloqueos y horario laboral de lunes a viernes.
- Las fechas son relativas a "hoy" real, así que el prototipo no envejece.
- Una sola fuente de datos alimenta ambas vistas, así que cualquier acción en una se refleja de inmediato en la otra.

### 5. Vistas

- **Barra superior:** el selector Cliente/Administrador, el usuario activo (con selector entre los 2 clientes mock) y un reloj simulado con botones para avanzar tiempo (+1 h, +1 día). El reloj sirve para auditar RN-01 y los estados `COMPLETADO`/`AUSENTE`.
- **Cliente:**
  - Elige servicio, profesional y fecha.
  - Ve la grilla de slots y un modal de confirmación con los datos de contacto.
  - Ve "Mis turnos", con el botón de cancelar deshabilitado y con tooltip explicativo dentro de las 2 h.
- **Administrador:**
  - Vista de agenda día/semana y vista de lista.
  - Métricas: turnos por período, tasas de cancelación y ausentismo, ocupación y top servicios.
  - Acciones sobre turnos: cancelar con motivo, completar y marcar ausente.
  - Gestión de bloqueos.
  - Un filtro de profesional para el rol `ADMIN`.

### 6. Verificación

- Probar en el navegador integrado los 8 escenarios de aceptación del spec §8, incluyendo el doble clic y la reserva del mismo slot desde dos clientes (concurrencia simulada en el reducer).
- Revisar la consola, el ancho móvil y el modo oscuro antes de publicar.
- Entregar el link del Artifact y una tabla que cruza cada escenario con el resultado observado.

### 7. Decisiones tomadas (por defecto, salvo indicación contraria)

| # | Tema | Decisión |
|---|---|---|
| 1 | Reloj simulado con botones para avanzar tiempo | **Sí** |
| 2 | Selector "Profesional / ADMIN" en la vista Administrador | **Sí** |
| 3 | Emails como "bandeja simulada" (F-13) | **Sí**, panel lateral colapsable |
| 4 | Reducir el horario laboral con turnos reservados (Q-01) | **Fuera** del prototipo; solo se implementa el bloqueo (RN-05) |
| 5 | Persistencia entre recargas | **No**: todo en memoria, con botón "Reiniciar datos" |
