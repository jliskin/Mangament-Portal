# Control Platform C.M / Yosef Golan

Sistema de gestión de propiedades y activos para un edificio en alquiler. Reemplaza la
planilla Excel y el cruce manual con el extracto bancario: emite los cargos mensuales,
importa el extracto, **propone qué pago corresponde a qué inquilino** (confirmación en 1
clic), administra contratos digitales (fechas, extensiones, PDF) y calcula NOI,
rentabilidad por unidad y morosidad por antigüedad.

| Documento | Contenido |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | §1 Stack, seguridad, local vs nube, estrategia bancaria · §2 Esquema e invariantes · §3 Algoritmos (parser, conciliación, FIFO, facturación, NOI) |
| [backend/db/migrations/001_initial_schema.sql](backend/db/migrations/001_initial_schema.sql) | DDL completo de PostgreSQL |
| [docs/UI_SPEC.md](docs/UI_SPEC.md) | Wireframes y componentes del panel y de la conciliación |
| [docs/screenshots/](docs/screenshots/) | Capturas reales de la aplicación con datos demo |

## Flujo mensual de trabajo

1. **Día 1** (automático): el sistema emite alquiler + expensas de cada contrato vigente.
2. **Al cierre o cuando quiera**: *Banco → Importar extracto* (CSV o XLSX tal como lo
   descarga del banco).
3. Revisar sugerencias: **Confirmar N de alta confianza** y resolver a mano las de
   confianza media o sin candidato. Cada confirmación crea el pago, lo aplica a la deuda
   más antigua y aprende el nombre del pagador.
4. *Débitos → gastos*: clasificar los egresos del mismo extracto.
5. Panel y Reportes ya están actualizados (NOI, cobranza, morosidad, alertas de vencimiento).

**Recomendación operativa de mayor retorno:** comunicar a cada inquilino su *código de
pago* (p. ej. `CM-301`, visible en la ficha del contrato) para que lo incluya en la
transferencia. Es la señal de identificación más fuerte del motor de conciliación.

## Puesta en marcha (producción, Docker)

```bash
cp .env.example .env              # completar POSTGRES_PASSWORD, CP_SECRET_KEY, CP_DOMAIN, CP_TIMEZONE
docker compose up -d --build      # db + app + scheduler + caddy (TLS) + backup
docker compose exec app python -m app.cli create-user --email you@dominio.com --name "Yosef Golan" --role admin
```

Luego, en la web: *Configuración* → datos del edificio y moneda → cuenta bancaria →
*Unidades* → *Inquilinos* → *Contratos*.

**Migración desde Excel:** cargar unidades, inquilinos y contratos; emitir cargos solo
desde el mes de arranque (`python -m app.cli backfill --from 2026-10 --to 2026-10`) y
registrar como saldo pendiente únicamente la deuda real existente, para no generar
morosidad ficticia de meses ya cobrados.

## Desarrollo local

```bash
cd backend
pip install -r requirements-dev.txt
export CP_DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/control_platform" \
       CP_ENVIRONMENT=development CP_SESSION_COOKIE_SECURE=false CP_STORAGE_DIR=./uploads
python -m app.cli migrate
python -m app.cli seed-demo                         # edificio demo de 8 unidades
python -m app.cli create-user --email admin@example.com --name Admin --role admin
python -m app.cli demo-statement --out extracto_demo.csv   # extracto para probar la conciliación
uvicorn app.main:app --reload
```

### Tests

```bash
cd backend
pytest tests/test_domain.py                                   # lógica pura, sin BD
CP_TEST_DATABASE_URL="postgresql+psycopg://…/cp_test" pytest  # + integración y HTTP (BD descartable)
```

67 tests: parser (formatos ES/US/XLSX, preámbulos, errores por fila, deduplicación),
conciliación (código, nombre, alias, parcial, ambigüedad, libro virtual), FIFO,
prorrateo, estados de contrato, NOI/CapEx, prorrateo por m², aging, restricciones de la
base de datos (solapamiento, sobre-aplicación), flujo web completo, CSRF, roles y webhook
HMAC.

## Estructura

```
backend/
  app/domain/      lógica pura: bank_parser, matching, allocation, billing, leases, financials
  app/services/    casos de uso transaccionales: billing, reconciliation, lease, reports, storage, bank_feed
  app/web/         rutas FastAPI + plantillas Jinja/HTMX
  app/cli.py       migrate · create-user · run-daily · generate-charges · backfill · seed-demo
  app/scheduler.py job diario (facturación idempotente)
  db/migrations/   SQL (fuente de verdad del esquema)
  tests/
deploy/Caddyfile   TLS y proxy inverso
docker-compose.yml
```

## Límites conocidos / próximos pasos

* **Backups off-site**: el contenedor genera dumps diarios locales; falta elegir destino y
  cifrado (restic/rclone) — ver ARCHITECTURE §1.5.
* **Open Banking**: interfaz y webhook firmados listos; falta elegir agregador según el
  país del banco (ARCHITECTURE §1.6).
* **Notificaciones**: las alertas se muestran en el panel; envío por email/WhatsApp no
  implementado.
* **Recargos por mora**: el tipo de cargo `late_fee` existe y se aplica primero en el
  FIFO, pero no hay regla automática (depende del contrato y la legislación local).
* **Indexación de alquileres**: los ajustes se registran como extensión/adenda con nuevo
  importe; no hay índice automático.
* Umbrales del motor de conciliación: razonados, **no calibrados** aún con datos propios;
  medir con la consulta de ARCHITECTURE §3.3 tras 2–3 meses.
