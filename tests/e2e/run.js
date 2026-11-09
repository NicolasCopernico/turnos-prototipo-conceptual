// Recorrido de punta a punta del frontend contra el backend real (spec.md §14, escenario 15).
// Lo ejecuta tests/test_e2e_navegador.py con una base temporal ya cargada con seed_demo.py. Código de salida 0 = todo OK.
const L = require('./lib');

const res = [];
const check = (n, ok, d = '') => { res.push({ n, ok }); console.log((ok ? 'OK    ' : 'FALLA ') + n + (d ? '  → ' + d : '')); };
const turnos = async (q = '') => (await L.api('/api/turnos' + q)).body;
const norm = (iso) => iso.replace(/\.\d+Z$/, 'Z');
const exacto = (hhmm) => new RegExp('^' + hhmm + '$');

(async () => {
  const b = await L.lanzar();
  const antes = await turnos();
  const esNuevo = (t) => !antes.some((a) => a.id === t.id);

  // 1. Carga inicial desde la API
  const p = await L.abrir(b);
  check('1. La página pide GET /api/turnos al cargar', p.reqs.includes('GET /api/turnos'));
  check('1. «Próximos (4)» coincide con los 4 turnos CONFIRMADO de c1 en la base', /Próximos \(4\)/.test(await L.texto(p)));
  check('1. Sin errores de JavaScript en la consola', p.errs.length === 0, JSON.stringify(p.errs));

  // 2. Reservar desde la UI → POST /api/turnos → persiste → F5
  const dia = await L.proximoDiaHabil(p);
  await L.clickTexto(p, /^Consulta general/); await L.elegirFecha(p, dia);
  const sl = await L.slots(p);
  check('2. La grilla ofrece horarios libres', sl.length >= 2, `${sl.length} horarios`);
  const [primero, otro] = [sl[0], sl[sl.length - 1]];
  await L.clickTexto(p, exacto(primero)); await L.clickTexto(p, /^Confirmar turno$/);
  check('2. Al confirmar, la API pasa de 15 a 16 turnos', await L.esperarApi(async () => (await turnos()).length === antes.length + 1));
  check('2. El navegador envió POST /api/turnos', p.reqs.includes('POST /api/turnos'));
  const nuevo = (await turnos()).find(esNuevo);
  const esperado = await p.evaluate((d, h) => new Date(`${d}T${h}`).toISOString(), dia, primero);
  check('2. El turno guardado es de c1, servicio s1, profesional p1, CONFIRMADO', !!nuevo && nuevo.cliente_id === 'c1' && nuevo.servicio_id === 's1' && nuevo.profesional_id === 'p1' && nuevo.estado === 'CONFIRMADO', nuevo && JSON.stringify(nuevo));
  check('2. El inicio guardado (UTC) coincide con el horario elegido en pantalla', !!nuevo && norm(nuevo.inicio) === norm(esperado), `${nuevo && nuevo.inicio} vs ${esperado}`);
  check('2. «Próximos» sube a 5', await L.esperarTexto(p, /Próximos \(5\)/));
  await p.reload({ waitUntil: 'domcontentloaded' });
  await p.waitForFunction(() => document.body.innerText.includes('Elegí un servicio')); await L.sleep(1500);
  check('2. Tras F5 el turno sigue ahí (Próximos 5, leído de SQLite)', /Próximos \(5\)/.test(await L.texto(p)));
  await L.clickTexto(p, /^Consulta general/); await L.elegirFecha(p, dia);
  check('2. El horario reservado ya no se ofrece', !(await L.slots(p)).includes(primero));

  // 3. Conflicto 409 SLOT_UNAVAILABLE: dos pantallas ven libre el mismo horario
  const A = await L.abrir(b); const B = await L.abrir(b);
  for (const q of [A, B]) { await L.clickTexto(q, /^Consulta general/); await L.elegirFecha(q, dia); }
  await B.select('select[aria-label="Cliente activo"]', 'c3'); await L.sleep(600);
  await L.clickTexto(B, /^Consulta general/); await L.elegirFecha(B, dia);
  await L.clickTexto(B, exacto(otro)); await L.clickTexto(B, /^Confirmar turno$/);
  check('3. B (Sofía) reserva el horario y queda guardado', await L.esperarApi(async () => (await turnos()).some((t) => esNuevo(t) && t.cliente_id === 'c3' && t.servicio_id === 's1' && t.estado === 'CONFIRMADO')));
  const antes409 = (await turnos()).length;
  await L.clickTexto(A, exacto(otro)); await L.clickTexto(A, /^Confirmar turno$/);
  check('3. A (Lucía, pantalla desactualizada) recibe SLOT_UNAVAILABLE', await L.esperarTexto(A, /SLOT_UNAVAILABLE/));
  check('3. La alerta muestra el motivo que devolvió el backend', /ya tiene un turno confirmado que se solapa/.test(await L.texto(A)));
  check('3. El 409 no creó ningún turno', (await turnos()).length === antes409);
  check('3. Tras el rechazo, el horario deja de ofrecerse (la lista se releyó)', !(await L.slots(A)).includes(otro));
  await A.close(); await B.close();

  // 4. Cancelación válida (≈6 h) → POST /cancelar → persiste → F5
  const t11 = (await turnos('?cliente_id=c1&estado=CONFIRMADO')).find((t) => t.servicio_id === 's2');
  await L.clickTexto(p, /^Cancelar$/); await L.clickTexto(p, /^Sí, cancelar$/);
  check('4. El turno de ~6 h queda CANCELADO en la base', await L.esperarApi(async () => (await turnos()).find((t) => t.id === t11.id).estado === 'CANCELADO'));
  const c11 = (await turnos()).find((t) => t.id === t11.id);
  check('4. La base guarda quién canceló (c1) y el navegador envió POST /cancelar', c11.cancelado_por_id === 'c1' && p.reqs.includes(`POST /api/turnos/${t11.id}/cancelar`));
  await p.reload({ waitUntil: 'domcontentloaded' });
  await p.waitForFunction(() => document.body.innerText.includes('Elegí un servicio')); await L.sleep(1500);
  check('4. Tras F5 «Próximos» baja a 4 (lo cancelado sigue cancelado)', /Próximos \(4\)/.test(await L.texto(p)));

  // 5. 409 CANCELLATION_WINDOW_EXPIRED: el reloj del navegador va 90 min atrasado y la UI ofrece cancelar un turno que ya está dentro de las 2 h
  const t8 = (await turnos('?cliente_id=c1&estado=CONFIRMADO')).find((t) => Date.parse(t.inicio) > Date.now() && Date.parse(t.inicio) - Date.now() < 2 * 3600e3);
  check('5. (preparación) existe un turno de c1 a menos de 2 h', !!t8, t8 && `${Math.round((Date.parse(t8.inicio) - Date.now()) / 60000)} min`);
  const R = await L.abrir(b, { skewMs: -90 * 60000 });
  await L.clickTexto(R, /^Cancelar$/); await L.clickTexto(R, /^Sí, cancelar$/);
  check('5. La UI muestra CANCELLATION_WINDOW_EXPIRED', await L.esperarTexto(R, /CANCELLATION_WINDOW_EXPIRED/));
  check('5. La alerta muestra el motivo del backend («hasta 2 horas antes»)', /hasta 2 horas antes/.test(await L.texto(R)));
  check('5. El turno tardío sigue CONFIRMADO en la base', !!t8 && (await turnos()).find((t) => t.id === t8.id).estado === 'CONFIRMADO');
  await R.close();

  // 6. Vista Administrador
  const AD = await L.abrir(b); await L.clickTexto(AD, /^Vista Administrador$/); await L.sleep(1500);
  const tx = await L.texto(AD);
  check('6. La vista Administrador muestra turnos reales (clientes de la base)', /Lucía Fernández/.test(tx) && /Sofía Ruiz/.test(tx));
  check('6. Sin errores de JavaScript en la vista Administrador', AD.errs.length === 0, JSON.stringify(AD.errs));
  await AD.close();

  // 7. Backend caído (las peticiones se rechazan como si uvicorn estuviera apagado)
  p.bloquearApi = true; await L.clickTexto(p, /^Recargar datos$/);
  check('7. Con el backend caído la UI avisa «No se pudo conectar con el backend»', await L.esperarTexto(p, /No se pudo conectar con el backend/));
  check('7. Sin errores de JavaScript no controlados', p.errs.length === 0, JSON.stringify(p.errs));

  await b.close();
  const f = res.filter((r) => !r.ok);
  console.log(`\nRESULTADO: ${res.length - f.length}/${res.length} comprobaciones OK` + (f.length ? ' | FALLAN: ' + f.map((x) => x.n).join(' ; ') : ''));
  process.exit(f.length ? 1 : 0);
})().catch((e) => { console.error('ERROR DEL SCRIPT:', e.stack || e.message); process.exit(2); });
