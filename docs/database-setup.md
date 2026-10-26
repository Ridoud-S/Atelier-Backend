# Atelier — Instalación, migración y verificación de la base de datos

Requisitos: **PostgreSQL 13 o superior** (probado con 16.15), **Python 3.12**. Los comandos asumen Linux/macOS
y que estás en la raíz del repositorio. En Windows usa WSL o adapta las rutas del entorno virtual.

> Alembic es la **fuente de verdad** del esquema. `docs/schema.sql` es solo un script generado a partir de Alembic.

## 1. Crear la base de datos local

Con un superusuario de PostgreSQL (p. ej. `sudo -u postgres psql`). Cambia la contraseña:

```sql
CREATE ROLE atelier WITH LOGIN PASSWORD 'cambia-esto' CREATEDB;   -- CREATEDB lo necesita la prueba de migración
CREATE DATABASE atelier_dev  OWNER atelier;
CREATE DATABASE atelier_test OWNER atelier;                       -- aislada, solo para pruebas
```

El rol **no** es superusuario: el esquema no requiere extensiones (ni `btree_gist` ni `pgcrypto`;
`gen_random_uuid()` es nativo desde PostgreSQL 13).

## 2. Entorno Python y variables de entorno

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt

cp backend/.env.example backend/.env      # edita las contraseñas; .env NO se versiona
```

`backend/.env.example` no contiene credenciales reales. Variables mínimas para la base de datos:
`DATABASE_URL` (desarrollo) y `TEST_DATABASE_URL` (pruebas; el nombre **debe** terminar en `_test`).
Las variables `SUPABASE_*` solo se necesitan para ejecutar la API con autenticación real.

## 3. Ejecutar las migraciones

```bash
cd backend
export DATABASE_URL='postgresql+psycopg://atelier:cambia-esto@localhost:5432/atelier_dev'

../.venv/bin/alembic upgrade head       # crea el esquema desde una base vacía
../.venv/bin/python -m app.db.seed      # reglas de XP, cuestionario v1, plantillas (idempotente)
```

La migración `0001` comprueba al empezar que el servidor es PostgreSQL ≥ 13 y falla con un mensaje claro si no.

## 4. Verificar el esquema

```bash
../.venv/bin/alembic current            # debe mostrar: 0001 (head)
../.venv/bin/alembic check              # debe mostrar: No new upgrade operations detected
                                        #   (modelos SQLAlchemy y base de datos coinciden)

psql -h localhost -U atelier -d atelier_dev -c '\dt'   # 21 tablas + alembic_version
```

(`psql` no entiende el prefijo `+psycopg`; usa `postgresql://atelier:…@localhost/atelier_dev`.)

Comprobaciones útiles dentro de `psql`:

```sql
-- los 7 triggers de integridad
SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1;

-- nada escribe en el historial de XP: si la tabla tiene filas, debe fallar con "xp_transactions is append-only"
UPDATE xp_transactions SET amount = amount;

-- el total cacheado coincide con el historial (debe devolver 0 filas)
SELECT g.user_id, g.total_xp, COALESCE(SUM(x.amount), 0) AS rebuilt
FROM user_game_profiles g LEFT JOIN xp_transactions x USING (user_id)
GROUP BY g.user_id, g.total_xp HAVING g.total_xp <> COALESCE(SUM(x.amount), 0);
```

## 5. Reiniciar la base de datos de desarrollo

**Destruye todos los datos de `atelier_dev`.** Nunca lo ejecutes contra una base con datos reales.

```bash
cd backend
../.venv/bin/alembic downgrade base      # elimina tablas, triggers y funciones
../.venv/bin/alembic upgrade head
../.venv/bin/python -m app.db.seed
```

Alternativa total: `DROP DATABASE atelier_dev; CREATE DATABASE atelier_dev OWNER atelier;`.

## 6. Script SQL reproducible

`docs/schema.sql` se genera en modo offline (sin conectarse a ninguna base):

```bash
scripts/generate-schema-sql.sh
```

Para usarlo en una base **vacía**: `psql -d atelier_dev -f docs/schema.sql`.
Se comprobó que produce un esquema idéntico al de Alembic (mismo volcado `pg_dump -s`, salvo el
token aleatorio `\restrict` que añade `pg_dump`). Regénéralo tras cada migración nueva.

## 7. Ejecutar las pruebas

```bash
cd backend
export TEST_DATABASE_URL='postgresql+psycopg://atelier:cambia-esto@localhost:5432/atelier_test'
../.venv/bin/python -m pytest -q
```

Medidas de aislamiento:

- Sin `TEST_DATABASE_URL` la suite **se detiene** (no hay valor por defecto).
- Si el nombre de la base no termina en `_test`, la suite **se detiene** antes de tocar nada.
- Al empezar, la suite recrea el esquema `public` de la base de pruebas y aplica la migración real
  (no usa `create_all`). Entre pruebas hace `TRUNCATE`. **Todo el contenido de `atelier_test` se pierde.**
- La prueba de migración crea y elimina sus propias bases temporales `atelier_mig_<id>_test`
  (por eso el rol necesita `CREATEDB`).

Subconjuntos útiles: `pytest tests/test_xp.py`, `pytest -k concurrency`, `pytest -x -q`.

## 8. Arrancar la API (opcional en esta fase)

```bash
cd backend
# necesita SUPABASE_JWT_ISSUER y SUPABASE_JWKS_URL (o SUPABASE_JWT_SECRET) en .env:
# sin esa configuración el servidor se niega a arrancar (a propósito).
../.venv/bin/uvicorn app.main:app --reload
# GET /health es público; el resto exige  Authorization: Bearer <token de Supabase>
```

## 9. Notas para desplegar en Supabase (no verificadas en este entorno)

1. **Cierra la API REST de Supabase**: ejecuta `docs/supabase-hardening.sql` después de migrar. Sin él, las
   tablas del esquema `public` pueden quedar accesibles con la clave pública. Ver `docs/security.md`.
2. Conecta el backend con un rol **propietario de las tablas** (p. ej. `postgres`), que no está sujeto a RLS.
3. Detrás de un *pooler* en modo transacción (puerto 6543 en Supabase) las sentencias preparadas del lado del
   servidor no funcionan: pon `DATABASE_DISABLE_PREPARED_STATEMENTS=true` (psycopg usa `prepare_threshold=None`).
   Ese ajuste está probado en local, pero **no contra el pooler real**. Para ejecutar migraciones usa la conexión
   directa o en modo sesión.
4. `profiles.id` puede enlazarse a `auth.users(id)` con una migración propia (no se hace en la base porque
   `auth.users` no existe en una instalación local de PostgreSQL).

## 10. Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| `TEST_DATABASE_URL is not set` | Exporta la variable (sección 7). |
| `Refusing to run destructive tests on database …` | El nombre no termina en `_test`. Es una protección intencionada. |
| `permission denied to create database` en la prueba de migración | Falta `CREATEDB` en el rol: `ALTER ROLE atelier CREATEDB;` |
| `Atelier needs PostgreSQL 13 or newer` | Actualiza el servidor. |
| `password authentication failed` | La contraseña de la URL no coincide con la del rol. |
| `retroactive version change rejected` | Intentas crear una versión de hábito con fecha anterior a días ya registrados (diseño intencionado, ver `data-model.md` §4). |
