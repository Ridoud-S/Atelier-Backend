# Atelier

Aplicación personal de organización, hábitos, tareas y mejora progresiva de rutinas (PWA).
Este repositorio contiene, por ahora, **la base de datos V1 y el backend mínimo que la ejerce**.

| Capa | Tecnología |
|---|---|
| Base de datos | PostgreSQL 13+ |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic |
| Autenticación | Supabase Auth (el backend verifica los tokens) |
| Frontend | React + TypeScript + Vite (pendiente) |

## Estructura

```
backend/
  app/core/        configuración y verificación de tokens
  app/db/          base declarativa, sesión, datos iniciales (seed)
  app/models/      modelos SQLAlchemy (21 tablas)
  app/domain/      enumeraciones y transiciones de estado
  app/services/    reglas de negocio (hábitos, tareas, misiones, XP, progreso)
  app/api/v1/      endpoints mínimos (usados para probar seguridad y reintentos)
  migrations/      Alembic (fuente de verdad del esquema)
  tests/           pruebas automatizadas
docs/
  data-model.md    diccionario de datos y decisiones de diseño
  erd.mmd          diagrama entidad-relación (Mermaid)
  database-setup.md  instalar, migrar, reiniciar, verificar
  security.md      aislamiento multiusuario y limitaciones (RLS)
  schema.sql       script generado desde Alembic
  supabase-hardening.sql  cierra la API REST de Supabase
scripts/           generate-schema-sql.sh
```

## Empezar

Consulta [`docs/database-setup.md`](docs/database-setup.md). Resumen:

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
cd backend
export DATABASE_URL='postgresql+psycopg://atelier:***@localhost:5432/atelier_dev'
../.venv/bin/alembic upgrade head && ../.venv/bin/python -m app.db.seed
export TEST_DATABASE_URL='postgresql+psycopg://atelier:***@localhost:5432/atelier_test'
../.venv/bin/python -m pytest -q
```

## Fuera de la V1

Agenda, diario/ánimo/multimedia, Pomodoro, entrenamiento, mediciones corporales, generación de planes y
recomendaciones, pantallas del frontend y RLS. El modo de ampliarlo está en `docs/data-model.md` §9.
