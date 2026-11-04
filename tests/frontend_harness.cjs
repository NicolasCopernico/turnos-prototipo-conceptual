/* Arnés de integración frontend <-> backend.
 *
 * Carga el código REAL de la capa de datos y de dominio de prototipo-turnos.html (secciones 1 a 4,
 * sin la parte de React/JSX) y ejecuta contra un backend vivo los mismos flujos que usa la interfaz:
 * calcular slots, reservar, cancelar y releer la lista.
 *
 * Uso:  node tests/frontend_harness.cjs <url_del_backend>
 * Salida: un único objeto JSON por stdout, que valida tests/test_frontend.py.
 */
const fs = require('fs');
const path = require('path');

const backend = process.argv[2];
const html = fs.readFileSync(path.join(__dirname, '..', 'prototipo-turnos.html'), 'utf8').split(/\r?\n/);

// Desde "1. CONFIG" hasta el cierre de makeInitialState (antes de "5. UI COMPARTIDA").
const ini = html.findIndex((l) => l.includes('1. CONFIG'));
const fin = html.findIndex((l) => l.includes('5. UI COMPARTIDA'));
if (ini < 0 || fin < 0) throw new Error('No se encontraron las secciones del frontend');
const apiLine = /^const API_URL = '[^']*';$/;
const fuente = html.slice(ini, fin).map((l) => (apiLine.test(l) ? `const API_URL = ${JSON.stringify(backend)};` : l)).join('\n');
if (!fuente.includes(JSON.stringify(backend))) throw new Error('No se pudo apuntar API_URL al backend de prueba');

const cargar = new Function(`${fuente}\nreturn { Domain, makeInitialState, cargarTurnos, callApi, turnoFromApi, HTTP };`);
const F = cargar();
const D = F.Domain;

const resumenError = (r) => (r.error ? { code: r.error.code, http: r.error.http, message: r.error.message } : null);

// Ejecuta los efectos de UI (auditoría, email simulado) para detectar errores de ejecución en el frontend.
function correrEfectos(state, r) {
  if (!r.effects) return null;
  const s = { ...state, turnos: r.turnos ?? state.turnos, audit: [], emails: [], seq: 100 };
  try { r.effects(s); return { audit: s.audit.length, emails: s.emails.length }; } catch (e) { return { excepcion: String(e) }; }
}

(async () => {
  const out = {};
  const state = F.makeInitialState();
  const actor = (id) => state.users.find((u) => u.id === id);
  const dia = (n) => { let t = new Date(); t.setHours(0, 0, 0, 0); let left = n; while (left > 0) { t.setDate(t.getDate() + 1); if (t.getDay() >= 1 && t.getDay() <= 5) left--; } return t.getTime(); };

  out.listaInicial = (await F.cargarTurnos(state.users)).length;

  // 1) Todos los slots que ofrece la UI para cada servicio, ¿los acepta el backend? (reloj, grilla, zona horaria)
  out.slotsOfrecidos = [];
  let n = 2;
  for (const svc of state.services) {
    const { slots } = D.computeSlots(state, svc.id, dia(n));
    const cliente = n % 2 ? 'c1' : 'c3';
    for (const slot of [slots[0], slots[slots.length - 1]]) {
      const r = await D.bookAppointment(state, { actor: actor(cliente), clienteId: cliente, servicioId: svc.id, inicio: slot.inicio });
      out.slotsOfrecidos.push({
        servicio: svc.id, ok: !!r.ok, error: resumenError(r),
        finCorrecto: r.ok ? r.data.turno.fin - r.data.turno.inicio === svc.duracionMin * 60000 : null,
        inicioIgual: r.ok ? r.data.turno.inicio === slot.inicio : null,
        efectos: correrEfectos(state, r),
      });
    }
    n += 1;
  }

  // 2) Reserva de un turno para el resto de los escenarios (dentro de ~10 días hábiles)
  const slot = D.computeSlots(state, 's1', dia(10)).slots[0];
  const reserva = await D.bookAppointment(state, { actor: actor('c1'), clienteId: 'c1', servicioId: 's1', inicio: slot.inicio });
  out.reserva = { ok: !!reserva.ok, turnosEnLista: reserva.turnos?.length, estado: reserva.data?.turno.estado };
  const id = reserva.data.turno.id;

  // 3) Errores de dominio tal como los muestra la UI
  const doble = await D.bookAppointment(state, { actor: actor('c3'), clienteId: 'c3', servicioId: 's1', inicio: slot.inicio });
  out.dobleReserva = resumenError(doble);
  const slotOtro = D.computeSlots(state, 's3', dia(11)).slots[0];
  const noVerificado = await D.bookAppointment(state, { actor: actor('c2'), clienteId: 'c2', servicioId: 's3', inicio: slotOtro.inicio });
  out.emailNoVerificado = resumenError(noVerificado);
  const fueraDeGrilla = await D.bookAppointment(state, { actor: actor('c3'), clienteId: 'c3', servicioId: 's1', inicio: slot.inicio + 7 * 60000 });
  out.fueraDeGrilla = resumenError(fueraDeGrilla);

  // 4) Cancelaciones
  out.cancelAjeno = resumenError(await D.cancelAppointment(state, { actor: actor('c3'), turnoId: id, motivo: '' }));
  out.cancelAdminSinMotivo = resumenError(await D.cancelAppointment(state, { actor: actor('admin'), turnoId: id, motivo: '' }));
  const cancel = await D.cancelAppointment(state, { actor: actor('c1'), turnoId: id, motivo: '' });
  const cancelado = cancel.turnos?.find((t) => t.id === id);
  out.cancelOk = { ok: !!cancel.ok, estado: cancelado?.estado, canceladoPor: cancelado?.canceladoPor, canceladoRol: cancelado?.canceladoRol,
    efectos: correrEfectos(state, cancel) };
  out.cancelRepetido = resumenError(await D.cancelAppointment(state, { actor: actor('c1'), turnoId: id, motivo: '' }));
  out.cancelInexistente = resumenError(await D.cancelAppointment(state, { actor: actor('c1'), turnoId: 'no-existe', motivo: '' }));

  // 5) Backend caído: la UI debe poder mostrar un error claro
  const caido = new Function(`${fuente.replace(JSON.stringify(backend), JSON.stringify('http://127.0.0.1:9'))}\nreturn { callApi };`)();
  out.backendCaido = resumenError(await caido.callApi('/api/turnos', {}));

  // 6) El estado final que leería la UI
  out.listaFinal = (await F.cargarTurnos(state.users)).length;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
