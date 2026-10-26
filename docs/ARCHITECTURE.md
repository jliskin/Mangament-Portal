# Control Platform C.M / Yosef Golan — Arquitectura técnica

Documento de referencia para las secciones 1–3 del encargo: stack y arquitectura,
esquema de base de datos y algoritmos centrales (parser bancario, conciliación,
facturación, finanzas del activo).

---

## 1. Stack recomendado y arquitectura

### 1.1 Decisión

| Capa | Elección | Motivo |
|---|---|---|
| Backend | **Python 3.13 + FastAPI** | Ecosistema líder para parsing de datos (CSV/Excel), fuzzy matching (RapidFuzz) y finanzas; tipado; rendimiento sobrado. |
| Base de datos | **PostgreSQL 16** | Integridad transaccional; *exclusion constraints* (sin contratos superpuestos), índices parciales (facturación idempotente), triggers (sin sobre-aplicación de pagos), JSONB para auditoría. |
| UI | **HTML server-rendered (Jinja2) + HTMX** | Interactividad de 1 clic sin SPA: sin build de frontend, sin API pública que mantener, un solo despliegue. |
| Jobs | **APScheduler** en contenedor propio | Facturación mensual y alertas diarias, idempotentes. |
| Despliegue | **Docker Compose** + **Caddy** (TLS automático) | Un solo host; `docker compose up -d`. |

**Alternativa evaluada y descartada: Next.js + API separada.** Para un edificio con 1–3
usuarios internos, una SPA duplica la superficie (dos apps, dos despliegues, contrato de
API, autenticación por tokens, build de JS) sin beneficio funcional: la pantalla más
interactiva (conciliación) se resuelve con fragmentos HTML vía HTMX. El costo de
mantenimiento es el factor dominante del TCO de un sistema interno de este tamaño; por eso
se prioriza la menor cantidad de piezas móviles. Si en el futuro hiciera falta un portal
de inquilinos o app móvil, la capa `services/` ya es independiente de la web y puede
exponerse como API JSON sin reescribir lógica.

### 1.2 Capas

```
            Navegador (HTML + HTMX, sin JS de terceros)
                         │ HTTPS (Caddy, TLS)
┌────────────────────────▼───────────────────────────────────────┐
│ app/web/*        Rutas, formularios, CSRF, roles, plantillas   │
│ app/services/*   Casos de uso con transacciones y auditoría    │
│                  billing · reconciliation · lease · reports    │
│                  storage · bank_feed (webhook HMAC)            │
│ app/domain/*     Lógica PURA, sin BD, 100 % testeada           │
│                  bank_parser · matching · allocation · billing │
│                  leases · financials · money · text            │
└────────────────────────┬───────────────────────────────────────┘
                         │ SQLAlchemy 2 / psycopg 3
              PostgreSQL 16  (+ volumen /data/uploads para PDFs)
   scheduler (contenedor aparte) ─┘   backup (pg_dump nocturno) ─┘
```

La separación `domain` / `services` / `web` es deliberada: los algoritmos que deciden
dinero (match, asignación FIFO, prorrateo, NOI) son funciones puras verificables sin base
de datos ni red.

### 1.3 Manejo seguro de datos

Clasificación: **PII de inquilinos** (nombre, documento, teléfono) y **datos bancarios**
(movimientos). Controles implementados:

| Riesgo | Control (archivo) |
|---|---|
| Robo de credenciales | Argon2id (`security.py`), mínimo 10 caracteres, throttle 10 intentos/15 min por IP (`web/auth.py`), rotación de sesión al login (anti *fixation*). |
| Sesión | Cookie firmada `HttpOnly`, `SameSite=Lax`, `Secure` en producción, expira en 8 h. |
| CSRF | Token por sesión verificado en **todo** POST (formulario o cabecera `X-CSRF-Token` para HTMX). |
| XSS / clickjacking | CSP `default-src 'self'` sin scripts inline ni CDNs (HTMX vendorizado), `X-Frame-Options: DENY`, autoescape Jinja. |
| Permisos | Roles `admin` / `manager` / `viewer`; las rutas de escritura exigen `manager`+. |
| Archivos subidos | Tipo por *magic bytes* (no por extensión), lista blanca PDF/JPG/PNG/XLSX/CSV, tope de tamaño, nombre aleatorio en disco (sin *path traversal*), SHA-256 guardado. |
| Datos bancarios | Solo últimos 4 dígitos de cuenta; nunca credenciales del banco (ver §1.6). |
| Trazabilidad | `audit_log` de toda acción que mueve dinero o contratos (quién, cuándo, qué). |
| Integridad contable | Restricciones en BD, no solo en la app: ver §2.2. |
| Secretos | `.env` fuera de git; la app se niega a arrancar en producción con `CP_SECRET_KEY` por defecto. |
| Base de datos | Sin puerto publicado; solo accesible desde la red interna de Compose. |

### 1.4 Local vs. nube

| Criterio | Mini-PC en la oficina | VPS pequeño (2 vCPU / 2–4 GB) |
|---|---|---|
| Costo directo | Hardware único | Bajo costo mensual (rango típico de un VPS de entrada) |
| Disponibilidad | Depende de luz/Internet del local | Alta; acceso desde cualquier lugar |
| Riesgo físico | Robo, incendio, disco | Del proveedor |
| Backups off-site | Hay que montarlos | Igual de necesarios (snapshot ≠ backup) |
| Acceso remoto | VPN / túnel | Nativo con TLS |

**Recomendación: VPS** en una región cercana, con TLS (Caddy) y backups cifrados fuera
del proveedor. El volumen de datos es mínimo (decenas de unidades, cientos de movimientos
por mes); el criterio decisivo es disponibilidad y recuperación ante desastre, no
capacidad. La opción local es válida si el requisito es que los datos no salgan del
edificio: el mismo `docker-compose.yml` funciona sin cambios (`CP_DOMAIN` = nombre LAN).

### 1.5 Backups y recuperación

El servicio `backup` genera cada noche `pg_dump` (formato custom) + tar de los PDFs,
retención 30 días en `./backups`. **Pendiente operativo (no automatizable sin elegir
proveedor):** cifrar y sincronizar fuera del host, p. ej.
`restic -r s3:… backup ./backups` o `rclone sync` con cifrado. Probar una restauración
real al menos una vez por trimestre:
`pg_restore -d control_platform --clean backups/db_YYYY-MM-DD.dump`.

### 1.6 Integración bancaria: API vs. archivo

**Fase 1 (implementada, recomendada): importación de extractos CSV/XLSX.** Funciona con
cualquier banco, costo cero, sin terceros que vean los datos.

**Fase 2 (capa lista, proveedor por elegir): Open Banking vía agregador licenciado.**
Los bancos no exponen APIs directas a particulares; se accede a través de un agregador
(según país: Plaid, GoCardless Bank Account Data, Belvo, Pluggy, o un AISP licenciado
bajo el régimen local). Implementado:

* `services/bank_feed.py`: interfaz `BankFeedProvider.fetch_transactions()` para
  *pull* programado y normalización a la misma estructura que el CSV.
* `POST /api/bank-feed/webhook`: *push* del agregador, autenticado con
  **HMAC-SHA256** sobre `timestamp.body`, ventana anti-replay de 5 min, idempotente por
  `external_id`.
* Ambos caminos terminan en el mismo pipeline: deduplicación → matching → confirmación
  humana.

**Criterio económico:** con un solo edificio la importación mensual toma minutos; el
agregador agrega costo recurrente, dependencia de un tercero con acceso de lectura a la
cuenta y consentimientos que vencen (típicamente cada 90–180 días). Activarlo solo si la
frecuencia deseada de conciliación es diaria o el número de unidades crece.

---

## 2. Esquema de base de datos

Fuente de verdad: [`backend/db/migrations/001_initial_schema.sql`](../backend/db/migrations/001_initial_schema.sql)
(aplicado por `python -m app.cli migrate`, transaccional y con lock).

### 2.1 Modelo

```
properties 1─* units 1─* leases *─* tenants (lease_parties: titular / co-inquilino / garante)
                           │  └─* lease_extensions (historial de renovaciones)
                           ├─* charges (cargos mensuales) *─* payments (vía payment_allocations)
                           └─  documents (contrato PDF)          │
bank_accounts 1─* bank_imports 1─* bank_transactions 1─0..1 payments
                                        │  └─* reconciliation_suggestions
                                        └─0..1 expenses *─1 expense_categories (is_capex)
tenants 1─* tenant_payer_aliases (nombres con que llega su dinero; aprendidos)
users · audit_log · vendors
```

### 2.2 Invariantes garantizadas por la base de datos

| Invariante | Mecanismo |
|---|---|
| Dinero exacto | `BIGINT` en centavos; nunca `FLOAT`. |
| Una unidad no tiene dos contratos solapados | `EXCLUDE USING gist (unit_id =, daterange &&)` |
| La facturación no duplica cargos | índice único parcial `(lease_id, charge_type, period)` |
| Un pago no se aplica por más de su importe, ni un cargo recibe más que su monto | trigger diferido `payment_allocations_guard` |
| Un movimiento bancario genera como máximo un pago o un gasto | `UNIQUE (bank_transaction_id)` |
| Reimportar un extracto no duplica movimientos | `UNIQUE (bank_account_id, fingerprint)` + `UNIQUE file_sha256` |
| Estado de pago y de contrato nunca se desincronizan | **derivados**, no almacenados (`v_charge_balances`, `v_lease_status`) |

---

## 3. Lógica central

### 3.1 Parser de extractos (`domain/bank_parser.py`)

Cada banco exporta distinto; el parser **detecta** en lugar de asumir, y reporta lo que
detectó para que el usuario lo valide:

1. **Lectura**: XLSX (openpyxl) o CSV; codificación probada en orden
   `utf-8-sig → cp1252 → cp1255 (hebreo) → latin-1`; delimitador por `csv.Sniffer`.
2. **Fila de encabezado**: escanea las primeras 30 filas y elige la que más columnas
   reconoce (salta logos, número de cuenta, período, etc.).
3. **Mapeo de columnas** por sinónimos ES/PT/EN/HE: fecha, fecha valor, concepto,
   importe único **o** crédito/débito separados, columna de signo (D/C), saldo,
   referencia, ordenante.
4. **Separador decimal** por votación sobre toda la columna (`1.234,56` vs `1,234.56`;
   un valor aislado `1.234` es ambiguo, la columna no).
5. **Orden de fecha** por evidencia: si algún primer campo > 12 → día primero; si algún
   segundo campo > 12 → mes primero; si no hay evidencia, día primero (convención no-US).
6. **Pagador**: columna propia o extraído del texto (`TRANSF. RECIBIDA DE …`,
   `PIX RECEBIDO DE …`, `ACH CREDIT FROM …`).
7. **Errores por fila** se reportan sin abortar; pies de página (`Saldo final`) se
   ignoran.
8. **Fingerprint** SHA-256 de (cuenta, fecha, importe, concepto normalizado, referencia,
   saldo, n.º de ocurrencia): idéntico al reimportar; dos movimientos legítimamente
   iguales el mismo día se conservan.

### 3.2 Conciliación automática (`domain/matching.py`)

Para cada par (ingreso, contrato) se calculan evidencias en [0, 1]:

| Señal | Qué mide |
|---|---|
| `reference` | Código de pago del contrato (`CM-301`, tolera `cm 301`, `CM301`) = 1.0; mención de la unidad (`apto 301`) = 0.9 |
| `name` | Similitud difusa pagador ↔ titulares/co-inquilinos (token_set_ratio, sin acentos ni palabras bancarias); alias aprendido exacto = 1.0 |
| `amount` | Deuda total = 1.0; cubre exactamente los *k* cargos más antiguos = 0.95; cuota mensual = 0.85; un cargo = 0.8; parcial ∝ proporción |
| `timing` | Cercanía a un vencimiento |

```
identity = min(1, max(ref, name) + 0.25 · min(ref, name))     # sustitutos + corroboración
score    = 0.60 · identity + 0.35 · amount + 0.05 · timing
```

**Por qué el importe no domina:** en un edificio donde varias unidades pagan lo mismo,
"importe exacto" encaja con N contratos a la vez: es evidencia no discriminante. Por eso
la confianza **alta** exige las tres condiciones: `score ≥ 0.75`, margen ≥ 0.15 sobre la
segunda opción, e identidad ≥ 0.8 (nombre o código). Un importe sin identidad puntúa como
máximo 0.40 → "media" en el mejor caso, nunca auto-confirmable.

**Asignación global:** algoritmo voraz sobre un *libro virtual*: se compromete el par más
confiable, se descuenta su importe de la deuda de ese contrato y se recalcula el resto.
Así dos transferencias iguales no reclaman el mismo mes.

**Aprendizaje:** al confirmar, el nombre del pagador (si difiere del titular) se guarda
como alias → el mes siguiente el mismo pagador es identidad 1.0.

**Flujo de usuario:** `Confirmar` (1 clic, sugerencia principal) · `Asignar a…` (otro
contrato) · `Ignorar` (no es alquiler) · `Deshacer` (borra el pago y re-calcula) ·
`Confirmar N de alta confianza` (lote).

### 3.3 Calibración: los umbrales son *priors*, no verdades

Los pesos y umbrales son razonados, no ajustados con datos (aún no existen datos
etiquetados de este edificio). El sistema registra en `audit_log` qué sugirió el motor y
qué confirmó el usuario, lo que permite medir la precisión real por banda:

```sql
SELECT details->>'suggested_confidence'                                         AS banda,
       count(*)                                                                 AS n,
       avg(((details->>'suggested_lease_id') = (details->>'lease_id'))::int)    AS precision
FROM audit_log
WHERE action = 'reconcile.confirm' AND details->>'suggested_confidence' IS NOT NULL
GROUP BY 1;
```

Advertencias metodológicas:

* **Tamaño muestral.** Con 50 confirmaciones "alta" y 0 errores, el límite inferior de
  Wilson (95 %) es ≈ 92.9 %, no 100 %. Para afirmar ≥ 98 % con cero errores hacen falta
  ≈ 190 casos. Con ~10–30 unidades eso son varios meses de operación.
* **Sesgo de automatización.** Las confirmaciones en lote no son revisión independiente:
  inflan la precisión medida. Durante los primeros 2–3 meses conviene confirmar fila a
  fila (o auditar una muestra) antes de confiar en el lote.
* **Ajuste:** si la banda "alta" muestra errores, subir `HIGH_THRESHOLD` o `MIN_MARGIN`
  en `matching.py` (cubierto por tests).

### 3.4 Aplicación de pagos (`domain/allocation.py`)

FIFO por fecha de vencimiento (dentro del mismo mes: recargos → expensas → alquiler).
Es la convención estándar de cobranza y minimiza la antigüedad de la deuda remanente.
El excedente queda como **saldo a favor** y se consume automáticamente cuando se emiten
los cargos del mes siguiente.

### 3.5 Motor de facturación (`domain/billing.py`, `services/billing_service.py`)

* Job diario 06:00: si hoy ≥ `invoice_day` del edificio, emite alquiler + expensas del
  mes para cada contrato vigente. Idempotente (índice único): correrlo N veces = 1 vez.
* Prorrateo por días reales en el primer y último mes (configurable).
* La rescisión anula los cargos impagos de meses posteriores.
* `cli backfill --from --to` para cargar meses históricos al migrar desde Excel
  (no se hace automáticamente: generaría deuda ficticia de meses ya cobrados fuera del
  sistema).

### 3.6 Finanzas del activo (`domain/financials.py`)

| Métrica | Definición |
|---|---|
| EGI (ingresos) | Alquiler + expensas + otros; **devengado** (facturado) o **caja** (cobrado) |
| OpEx | Gastos operativos; **excluye CapEx** (categoría `is_capex`) y servicio de deuda |
| **NOI** | EGI − OpEx |
| Flujo antes de deuda | NOI − CapEx |
| P&L por unidad | Ingresos de la unidad − gastos directos − gastos comunes prorrateados por m² (resto mayor: los centavos suman exacto) |
| Tasa de cobranza | Cobrado sobre lo facturado del período |
| Ocupación física / económica | Unidades ocupadas / activas · Alquiler facturado / renta de lista potencial |
| Aging | Saldo por días de atraso (después de gracia): 1–30, 31–60, 61–90, +90 |

Se muestran **ambas bases** porque responden preguntas distintas: el devengado mide la
rentabilidad del activo; la caja mide liquidez. La brecha entre ambos es la morosidad.
