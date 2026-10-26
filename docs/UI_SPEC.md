# Especificación UI/UX

Principios: **una pantalla = una decisión**; el estado del dinero se lee por color
(verde pagado · ámbar parcial · rojo vencido · gris en plazo); toda acción que mueve dinero
es reversible (`Deshacer`) y queda auditada. Responsive (desktop primero, usable en móvil),
modo claro/oscuro automático, sin dependencias JS externas.

Navegación lateral: **Panel · Unidades · Inquilinos · Contratos · Cobranza ·
Banco / Conciliación · Gastos · Reportes · Configuración** (esta última solo admin).

Capturas reales en [`docs/screenshots/`](screenshots/).

---

## 1. Panel principal (`/`)

(Valores ilustrativos del dataset demo.)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Edificio C.M — Yosef Golan            [Importar extracto / conciliar] [Gasto]│
│ Situación al 26/10/2026 · USD                                                │
├────────────┬────────────┬────────────┬────────────┬────────────┬─────────────┤
│ NOI MES    │ NOI AÑO    │ COBRANZA   │ DEUDA      │ OCUPACIÓN  │ PENDIENTE   │
│ 13,339.45  │ 77,416.70  │ 64%        │ VENCIDA ●  │ 88%        │ CONCILIAR ● │
│ margen 91% │ ingr/gastos│ 9,4k/14,7k │ 9,390.00   │ econ. 90%  │ 8 → /bank   │
├────────────┴────────────┴──────┬─────┴────────────┴────────────┴─────────────┤
│ Ingresos vs NOI (12 meses)     │ Alertas de contratos                        │
│  ▆▇ ▆▇ ▆▇ ▆▇ ▆▇ ▆▇  (SVG)       │ [Crítica] U201: preaviso de renovación 1 día│
│                                │ Vencen pronto: 201 Pedro Ruiz 26/12 [ámbar] │
├────────────────────────────────┴─────────────────────────────────────────────┤
│ Antigüedad de deuda              [Exportar CSV]                               │
│ Unidad Inquilino   Al día  1–30   31–60  61–90  +90   Total                  │
│ 201    Pedro Ruiz         1,680  1,680  1,680         5,040                  │
│ …                                                       Σ por columna        │
└──────────────────────────────────────────────────────────────────────────────┘
```

| Componente | Datos | Interacción |
|---|---|---|
| KPI tiles (6) | `reports_service.dashboard()` | Borde rojo si hay deuda o pendientes; el tile "Pendiente de conciliar" enlaza a `/bank` |
| Gráfico 12 meses | `monthly_noi_series()` — barras gris (ingresos) y azul (NOI); NOI negativo en rojo bajo la línea base | `title` con el valor exacto al pasar el mouse |
| Alertas | `expiry_alerts()` — hitos 90/60/30/7/0 días y plazo de preaviso de la opción de renovación (la más costosa de olvidar) | Clic → contrato |
| Aging | `aging_report()` con días de gracia | Ordenado por deuda vencida; CSV |

## 2. Conciliación bancaria (`/bank`) — pantalla central

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Conciliación bancaria   7 con sugerencia · 1 sin candidato · 0 conciliados   │
│              [✓ Confirmar 5 de alta confianza] [Recalcular] [Débitos → gastos]│
├──────────────────────────────────────────────────────────────────────────────┤
│ ▸ Importar extracto (CSV/XLSX): [Cuenta ▾] [Archivo…] [Importar y conciliar] │
├──────────────────────────────────────────────────────────────────────────────┤
│ Pendientes | Conciliados | Ignorados | Todos                                 │
│ FECHA  PAGADOR / DESCRIPCIÓN        IMPORTE  SUGERENCIA                ACCIÓN │
│ 02/10  Jose Perez Gomez             1,630.00 [Alta] 100 Unidad 101    [✓ Conf]│
│        TRANSF. RECIBIDA … CM-101             ·Código CM-101 ·Nombre   [Asig▾] │
│                                              ·Importe = deuda total   [Ignor] │
│ 05/10  Pedro Ruiz                   1,000.00 [Media] 68 Unidad 201    [✓ Conf]│
│                                              ·Nombre 100% ·Pago parcial       │
│ 07/10  DEPOSITO EN EFECTIVO         1,630.00 [Sin candidato]          [Asig▾] │
├──────────────────────────────────────────────────────────────────────────────┤
│ Últimas importaciones: filas · nuevas · duplicadas · errores · columnas      │
│ detectadas · separador decimal · orden de fecha · [ver errores por fila]     │
└──────────────────────────────────────────────────────────────────────────────┘
```

| Elemento | Comportamiento |
|---|---|
| Badge de confianza + score 0–100 | Alta (verde) / Media (ámbar) / Sin candidato (rojo) |
| Chips de razones | Explican **por qué** se sugiere (código, nombre %, regla de importe). El usuario no confía en una caja negra. |
| Alternativas | Hasta 2 contratos más con su score |
| `✓ Confirmar` | `hx-post` → crea pago, aplica FIFO, aprende alias; **reemplaza solo esa fila** con "Conciliado → Unidad X [Deshacer]" sin recargar |
| `Asignar a…` | Selector de todos los contratos vigentes, para corregir o resolver "sin candidato" |
| `Ignorar` | Con confirmación; para ingresos que no son alquiler |
| `Confirmar N de alta confianza` | Lote; solo filas que cumplen score + margen + identidad |
| Importación | Mensaje con lo detectado (columnas, decimal, fechas) para validación humana; reimportar el mismo archivo se rechaza; extractos superpuestos no duplican |

Débitos (`/bank/debits`): cada egreso del extracto → `[Categoría ▾] [Unidad/Edificio ▾]
[Descripción] [Registrar]` o `Ignorar`. Así el NOI se alimenta del mismo extracto.

## 3. Cobranza (`/charges`) — reemplazo directo de la planilla Excel

Matriz **unidad × últimos 6 meses**, un punto por celda (verde/ámbar/rojo/gris) con el
saldo debajo cuando no está pagado. Botón `Emitir cargos del mes` (idempotente).

## 4. Contrato (`/leases/{id}`)

* Tiles: saldo pendiente, saldo a favor, cuota mensual (desglose), depósito.
* Ficha: firma, inicio, entrada, vencimiento (días restantes), salida, día de pago + gracia,
  opción de renovación y preaviso, partes, PDF del contrato.
* Acciones (plegables): pago manual (efectivo/cheque), extender/renovar (nuevo
  vencimiento, nuevos importes, adenda PDF), rescindir (anula cargos futuros, devolución
  de depósito).
* Cuenta corriente: cada cargo con pagado/pendiente/estado; pagos con aplicado y saldo a
  favor; historial de extensiones.

## 5. Otras pantallas

| Pantalla | Contenido |
|---|---|
| Unidades | Tabla con m², renta de lista, expensas, inquilino actual y estado del contrato; vacantes con acceso directo a "+ contrato" |
| Inquilinos | Búsqueda; ficha con contacto de emergencia y **alias de pagador** (aprendidos/manuales) |
| Gastos | Filtro por mes/categoría, total y desglose por categoría, comprobante adjunto, proveedor; CapEx marcado (no afecta NOI) |
| Reportes | Estado de resultados devengado y caja lado a lado (EGI → OpEx → NOI → CapEx → flujo), P&L por unidad con prorrateo por m², aging; exportación CSV |
| Configuración | Edificio (moneda, día de emisión, gracia, ventana "vence pronto"), cuentas bancarias (solo últimos 4 dígitos), usuarios y roles |

## 6. Estados y accesibilidad

* Vacíos explícitos ("No hay ingresos pendientes", "Vacante").
* Errores de formulario devuelven el formulario con mensaje (HTTP 422), nunca una página
  en blanco.
* Etiquetas `<label for>` en todos los campos, `aria-label` en selectores sin rótulo,
  contraste AA en ambos temas, números tabulares alineados a la derecha.
