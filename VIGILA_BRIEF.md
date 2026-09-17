# Vigila — Brief de construcción para Claude Code

## 1. Qué es el producto

**Vigila** es un micro-SaaS B2C español que vigila el **Tablón Edictal Único del BOE (TEU)** — donde la
Administración publica las notificaciones de multas de tráfico que no pudo entregar por correo — y avisa
al usuario en cuanto aparece una notificación a su nombre, antes de que se dé por notificada
automáticamente (20 días naturales tras la publicación) y el plazo de alegación empiece a correr sin que
lo sepa.

- **No es un despacho de abogados.** No tramitamos recursos ni damos asesoría legal. Solo vigilamos y avisamos.
- **Modelo:** primera comprobación gratis (lead magnet) → suscripción de pago para vigilancia automática.
- **Precio:** 3,99 €/mes individual · 6,99 €/mes plan familiar (hasta 4 DNIs/matrículas).

Ya existe una landing de validación (HTML/CSS/JS estático, sin backend) adjunta a este brief
(`landing.html`) con el copy y el diseño aprobados. **Reutiliza ese frontend como base** — la lógica de
comprobación ahí está simulada (`setTimeout` + regla aleatoria); hay que sustituirla por una llamada real
al backend que se describe abajo.

## 2. La pieza técnica ya validada (no hace falta redescubrirla)

Se validó en vivo, navegando con un browser tool, contra `boe.es` real. Esto es lo confirmado:

### 2.1 Endpoint de búsqueda — sin login, sin CAPTCHA
```
GET https://www.boe.es/notificaciones/notificaciones.php
```
Parámetros GET (se pueden construir directamente, es un formulario plano):
```
campo[0]=DOC          # texto libre: NIF/DNI/NIE/CIF/matrícula/nombre
dato[0]=<VALOR>        # el DNI o la matrícula del usuario, sin espacios o con espacios — ambos funcionan
operador[0]=and
campo[1]=DEM
dato[1]=
operador[1]=and
campo[2]=MATERIA
dato[2]=43              # 43 = "TRÁFICO, CIRCULACIÓN Y SEGURIDAD VIAL" (opcional, filtra ruido)
operador[2]=and
campo[3]=NBO
dato[3]=
operador[4]=and
campo[4]=FPU
dato[4][0]=
dato[4][1]=
page_hits=50
sort_field[0]=FPU
sort_order[0]=desc
sort_field[1]=id
sort_order[1]=asc
accion=Buscar
```
- Confirmado con 5+ consultas reales (2 matrículas, 1 DNI en dos formatos, otra matrícula) → todas
  devolvieron `"No se han encontrado documentos que satisfagan sus criterios de búsqueda"` de forma
  estable, sin fricción ni bloqueo.
- Confirmado con un caso positivo: filtrando `MATERIA=43` sin dato personal, aparecen decenas de PDFs
  reales (uno por provincia/boletín). Se cogió una matrícula real de dentro de uno de esos PDFs y se buscó
  por `DOC` → **1 resultado de 1**, exactamente ese PDF. Esto demuestra que el buscador **indexa el texto
  completo del PDF**, no solo el título del anuncio — el mismo endpoint sirve para localizar al usuario.

### 2.2 Qué hay dentro de un resultado positivo
Cada resultado es un **PDF colectivo por provincia** (ref. tipo `BOE-N-2026-703982`), de varias páginas
(el de ejemplo de Albacete tenía 14), con una tabla de decenas de expedientes. Columnas:

```
EXPEDIENTE | DENUNCIADO/A | IDENTIF (DNI/NIF) | LOCALIDAD | FECHA | MATRÍCULA | CUANTÍA EUROS | PRECEPTO | ART° | PTOS | REQ
```

El PDF se referencia así (patrón observado):
```
https://www.boe.es/boe_n/dias/<YYYY>/<MM>/<DD>/not.php?id=BOE-N-<YYYY>-<NNNNNN>
```
(la ruta exacta del PDF descargable hay que confirmarla en el HTML de resultados — el link "PDF (Referencia
BOE-N-...)" en la página de resultados apunta al recurso real; extraerlo con el parser HTML).

### 2.3 Pipeline técnico completo (ya sin incógnitas)
1. Cron (cada 12–24h) recorre todos los DNIs/matrículas activos en la base de datos.
2. Para cada uno, `GET` al endpoint de arriba con `dato[0]=<DNI o matrícula>`.
3. Parsear el HTML de respuesta:
   - `"No se han encontrado documentos..."` → sin novedad, no hacer nada.
   - Si hay resultados → extraer la(s) referencia(s) `BOE-N-YYYY-NNNNNN` y la URL del PDF.
4. Descargar el PDF y localizar la fila cuyo DNI/matrícula coincide (extracción de tabla con
   `pdfplumber` o `camelot`, Python).
5. Si es una notificación nueva (no vista antes para ese usuario) → guardar y disparar alerta
   (email obligatorio; SMS/WhatsApp opcional vía Twilio en fase 2).
6. Marcar como "vista" para no re-notificar en el siguiente ciclo.

No hace falta headless browser ni Selenium — son peticiones HTTP simples + parseo de HTML/PDF.

## 3. Alcance del MVP (v1)

**Dentro:**
- Fuente única: TEU del BOE, filtrado a tráfico (`MATERIA=43` cubre también las multas municipales de
  tráfico como zona SER/radares de ayuntamientos, porque también se notifican vía TEU — confirmado).
- Alta: email + contraseña (o magic link), pago (Stripe), DNI/NIE (obligatorio), matrícula(s) (opcional,
  repetible para el plan familiar), nombre (opcional).
- Comprobación gratuita instantánea (sin cuenta) como lead magnet, igual que en `landing.html`.
- Dashboard: "todo en orden" o detalle de notificación con expediente, importe, artículo, días restantes
  para alegar.
- Alertas por email cuando aparece una notificación nueva.
- Rate limiting en la comprobación gratuita (por IP/fingerprint) + Turnstile/captcha si se supera el límite.

**Fuera de v1 (dejar para fase 2):**
- Seguridad Social / SEPE / tablones municipales no-tráfico / BOCM (cada uno tiene su propia fuente y
  estructura — no aporta al dolor principal, que es tráfico).
- Redacción/presentación automática de alegaciones (posible upsell futuro con despacho asociado).
- SMS/WhatsApp (Twilio) — solo email en v1.

## 4. Modelo de datos (sugerido)

```
users
  id, email, password_hash (o auth provider), created_at,
  stripe_customer_id, subscription_status, plan (individual|familiar)

monitored_ids
  id, user_id (FK), value (DNI/NIE/matrícula), label (nombre opcional), created_at

checks_free  -- para la comprobación gratuita sin cuenta
  id, ip_hash, fingerprint_hash, value_hash (no guardar el DNI en claro si no se convierte en suscripción),
  result (found|not_found), created_at

notifications
  id, monitored_id (FK), boe_ref (BOE-N-...), expediente, importe, fecha, precepto, articulo,
  plazo_alegacion_fin, seen_by_user (bool), notified_at, created_at

notification_runs  -- log de cada ciclo del cron, para debugging
  id, monitored_id (FK), ran_at, result (found|not_found|error), raw_response_snippet
```

**Privacidad:** en la comprobación gratuita, si el usuario NO se suscribe, no persistir el DNI/matrícula
en claro — procesarlo en memoria y descartarlo (o guardar solo un hash para rate-limiting/analítica). Si
se suscribe, ahí sí se guarda con su consentimiento explícito de alta.

## 5. Stack sugerido (libre, esto es solo una propuesta razonable)
- **Backend:** Node.js (Express/Fastify) o Python (FastAPI) — Python es cómodo por `pdfplumber`/`camelot`.
- **DB:** PostgreSQL (Supabase o similar simplifica auth + DB + cron en un mismo proveedor).
- **Cron:** job programado (Supabase Cron, GitHub Actions scheduled workflow, o un worker con
  `node-cron`/`APScheduler`) cada 12–24h.
- **Pagos:** Stripe Checkout + Customer Portal (evita construir gestión de tarjetas a mano).
- **Email transaccional:** Resend, Postmark o SES.
- **Frontend:** reutilizar `landing.html` como página pública; el dashboard puede ser una app aparte
  (Next.js/React) o servido desde el mismo backend con plantillas simples — a decidir según preferencia.

## 6. Pasos de construcción sugeridos (orden de milestones)

1. **Scraper aislado y probado**: script que reciba un DNI/matrícula, haga la petición GET, devuelva
   `{found: bool, refs: [...]}`. Probarlo contra casos reales conocidos (usar el filtro `MATERIA=43` para
   sacar una matrícula real de un PDF y confirmar que el propio script la encuentra).
2. **Parser de PDF**: dado un `BOE-N-...`, descargar el PDF y extraer la tabla completa a filas
   estructuradas. Confirmar que localiza correctamente la fila de un DNI/matrícula concreto.
3. **Modelo de datos + API mínima**: endpoints para alta de usuario, alta de DNI/matrícula, consulta de
   estado.
4. **Endpoint de comprobación gratuita**: conecta el formulario de `landing.html` (sustituyendo el
   `setTimeout` simulado) a `scraper + parser` en síncrono, con rate limiting.
5. **Cron + notificaciones**: recorre `monitored_ids`, ejecuta el pipeline, guarda `notifications` nuevas,
   dispara email.
6. **Stripe**: checkout, webhook de suscripción, portal de cliente.
7. **Dashboard de usuario**: ver DNIs/matrículas monitorizados, histórico de notificaciones, gestionar
   suscripción.

## 7. Variables de entorno necesarias (a definir durante la construcción)
```
DATABASE_URL=
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PRICE_ID_INDIVIDUAL=
STRIPE_PRICE_ID_FAMILIAR=
EMAIL_PROVIDER_API_KEY=
TURNSTILE_SITE_KEY=
TURNSTILE_SECRET_KEY=
```

## 8. Copy y diseño ya aprobados
Todo el copy (titular, subtítulo, CTAs, disclaimers) y el diseño visual están en `landing.html` adjunto —
úsalo tal cual como página pública; solo hay que:
1. Sustituir la simulación JS por una llamada `fetch()` real al endpoint de comprobación gratuita.
2. Conectar los formularios de email (tras el resultado) al alta real de usuario / lista de espera.
3. Mantener el disclaimer legal ("servicio independiente, no afiliado a la Administración") en todas las
   pantallas, incluido el dashboard.

## 9. Riesgos y notas legales a tener en cuenta durante la construcción
- Revisar el aviso legal de la sede electrónica del BOE por si limita el uso automatizado masivo — aplicar
  un rate limit razonable en el cron (no miles de peticiones simultáneas).
- El DNI es un dato identificativo sensible en términos de protección de datos — pedir consentimiento
  explícito (checkbox no premarcado) antes de la comprobación gratuita, y en el alta de pago.
- Dejar claro en todo momento que no se presta asesoría legal ni se gestionan recursos — la app **avisa**,
  no actúa por el usuario.
