# Slack Audit Console — Documentación del Proyecto

**Fecha de generación:** 2026-06-30
**Generado con:** Claude Code (claude-sonnet-4-6) via Anthropic

---

## Prompt original

> Misión: Actúa como un Desarrollador Full-Stack Senior experto en Python (FastAPI), JavaScript y la API de Slack. Necesito que construyas una aplicación web de ejecución local ("Slack Audit Console") que permita a los usuarios monitorear el consumo de API Requests de sus aplicaciones de Slack (basadas en Deno/infraestructura nativa) mediante un modelo "Pull".
>
> La aplicación debe ejecutarse localmente en la máquina del usuario (ej: levantando un servidor local en http://127.0.0.1:8000).

---

## Stack tecnológico

| Capa | Tecnología |
|------|-----------|
| Backend | Python 3.11+, FastAPI, Uvicorn |
| HTTP Client | httpx (async) |
| Base de datos | SQLite (`database.db`, archivo local) |
| Config | `.env` local (python-dotenv) |
| Frontend | HTML5, Tailwind CSS v3 (CDN), JavaScript ES6 vanilla |
| Gráficos | Chart.js 4.4.2 (CDN) |
| Templates | Jinja2 |

---

## Archivos generados

### `main.py`
Servidor FastAPI principal. Contiene:
- Evento `startup` → inicializa la base de datos.
- `GET /` → sirve el frontend (`index.html`) con flag `token_set`.
- `POST /api/config/token` → valida y persiste el Slack Bot Token en `.env`.
- `GET /api/config/status` → verifica si el token está configurado y vigente.
- `GET /api/apps` → descubre apps instaladas en el workspace via `slack_client`.
- `POST /api/refresh` → pull de audit logs de Slack, inserta en SQLite evitando duplicados.
- `GET /api/metrics` → métricas agregadas (total, tasa de éxito, app top).
- `GET /api/timeline` → evolución diaria de llamadas para el gráfico de barras.
- `GET /api/distribution` → distribución por app para el donut chart.
- `GET /api/calls` → listado paginado de llamadas para la tabla.

Todos los endpoints de datos aceptan query params `?period=today|7d|30d|all` y `?app_id=`.

---

### `database.py`
Módulo SQLite. Contiene:
- `init_db()` → crea tablas `api_calls` y `sync_log` con índices.
- `insert_api_call()` → inserta registro; retorna `False` en duplicado (constraint `UNIQUE(app_id, ts, endpoint)`).
- `query_api_calls()` → consulta con filtros de fecha y app.
- `get_metrics()` → calcula total, tasa de éxito y app más activa.
- `get_timeline()` → agrupa llamadas por día.
- `get_distribution()` → cuenta llamadas por app (Counter).
- `log_sync()` / `last_sync()` → registro histórico de sincronizaciones.

**Schema SQLite:**
```sql
CREATE TABLE api_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      TEXT NOT NULL,
    app_name    TEXT NOT NULL,
    endpoint    TEXT,
    ts          TEXT NOT NULL,
    status      TEXT,
    raw_event   TEXT,
    UNIQUE(app_id, ts, endpoint)
);

CREATE TABLE sync_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at  TEXT NOT NULL,
    records_in INTEGER DEFAULT 0,
    success    INTEGER DEFAULT 1,
    message    TEXT
);
```

---

### `slack_client.py`
Cliente async para la API de Slack. Contiene:
- `validate_token(token)` → llama a `auth.test`, retorna info del workspace.
- `list_installed_apps(token)` → estrategia de 3 niveles:
  1. `admin.apps.approved.list` (requiere `admin:apps:read`)
  2. `audit/v1/logs?action=app_approved` (requiere `audit:read`, Enterprise Grid)
  3. Fallback: retorna la app del propio token via `auth.test`
- `fetch_audit_logs(token, app_ids, oldest, latest)` → descarga audit logs de Enterprise Grid; fallback a registros sintéticos "heartbeat" para workspaces estándar.
- Manejo de Rate Limits: respeta el header `Retry-After`, reintentos automáticos (`MAX_RETRIES=3`).
- Excepciones tipadas: `SlackAuthError`, `SlackRateLimitError`, `SlackError`.

**Endpoints Slack utilizados:**
```
https://slack.com/api/auth.test
https://slack.com/api/admin.apps.approved.list
https://api.slack.com/audit/v1/logs
https://api.slack.com/audit/v1/actions
```

---

### `templates/index.html`
SPA (Single Page Application) completa. Contiene:
- **Overlay de onboarding** (oculto por defecto): formulario de token con validación y spinner. Se muestra sólo si `token_set = false`.
- **Sidebar** con checkboxes de apps descubiertas, botón "Redescubrir apps", seleccionar todas/ninguna.
- **Top bar** con botón Refresh + indicador de última sincronización.
- **Filtros** de período (Hoy / 7 días / 30 días / Todo) y selector de app.
- **3 Metric Cards**: Total requests, Tasa de éxito, App más activa.
- **Gráfico de barras** (Chart.js): evolución diaria de API requests.
- **Donut chart** (Chart.js): distribución porcentual por app con paleta de colores.
- **Tabla** de últimas llamadas (timestamp, app, endpoint, status con color).
- **Toast notifications** (éxito/error/info) con auto-dismiss a los 4 segundos.
- **Estado global JS** (`state.period`, `state.appFilter`, `state.apps`, `state.selected`).

---

### `requirements.txt`
```
fastapi==0.111.0
uvicorn[standard]==0.29.0
httpx==0.27.0
python-dotenv==1.0.1
jinja2==3.1.4
```

### `start.bat`
Script de inicio para Windows: instala dependencias e inicia Uvicorn en `127.0.0.1:8000`.

---

## Cómo ejecutar

```bash
# Instalar dependencias
pip install -r requirements.txt

# Iniciar servidor
uvicorn main:app --reload --host 127.0.0.1 --port 8000

# O simplemente doble click en:
start.bat
```

Abrir navegador en: **http://127.0.0.1:8000**

---

## Scopes OAuth de Slack requeridos

| Scope | Nivel | Para qué |
|-------|-------|----------|
| `audit:read` | Enterprise Grid | Leer audit logs |
| `admin:apps:read` | Admin | Listar apps aprobadas |
| `channels:read` | Estándar | Fallback mínimo |

---

## Flujo de datos (modelo Pull)

```
Usuario
  └─ click "Refresh"
       └─ POST /api/refresh
            ├─ slack_client.fetch_audit_logs()
            │    └─ GET audit/v1/logs  (Slack API)
            ├─ database.insert_api_call()  [deduplica por UNIQUE constraint]
            └─ database.log_sync()

Dashboard
  └─ JS loadDashboard()
       ├─ GET /api/metrics   → cards
       ├─ GET /api/timeline  → gráfico de barras
       ├─ GET /api/distribution → donut chart
       └─ GET /api/calls     → tabla
```

---

## Decisiones de diseño relevantes

- **Sin dependencia de librerías Slack oficiales**: se usa `httpx` directamente para mantener el proyecto liviano y con control total del manejo de rate limits.
- **Deduplicación por constraint SQLite**: más eficiente que verificar existencia antes de insertar (approch INSERT-or-ignore).
- **Fallback en 3 niveles para descubrimiento de apps**: el token puede tener scopes limitados; la app siempre tiene algo que mostrar.
- **Registros sintéticos para workspaces no-Enterprise**: permite usar el dashboard aun sin acceso al Audit Logs API real.
- **Frontend vanilla sin framework**: reduce complejidad de build, la app corre sin Node.js instalado.
- **`.env` gestionado manualmente** (sin python-dotenv para escritura): se escribe directamente para evitar que python-dotenv sobreescriba claves existentes no relacionadas.
