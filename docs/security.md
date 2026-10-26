# Atelier — Seguridad y aislamiento multiusuario (V1)

## 1. Arquitectura

```
Navegador / PWA ──(Bearer JWT de Supabase)──▶ FastAPI ──(único cliente)──▶ PostgreSQL
        │                                        │
        └── NO tiene credenciales de la BD       └── verifica el token, deriva user_id, aplica reglas
```

En la V1 **FastAPI es el único acceso a PostgreSQL**. El frontend no usa el cliente de datos de Supabase y no
recibe credenciales de la base. Supabase Auth solo emite identidad (tokens).

## 2. Cuatro capas de aislamiento

| # | Capa | Qué garantiza | Dónde |
|---|---|---|---|
| 1 | Identidad | El `user_id` sale **solo** del token verificado, nunca del cuerpo, la URL ni cabeceras | `core/security.py`, `api/deps.py` |
| 2 | Consultas con propietario | Toda lectura/escritura de una entidad pasa por `get_owned(session, Modelo, id, user_id)`; una entidad ajena y una inexistente dan el **mismo** `404` (no se puede sondear qué ids existen) | `services/ownership.py` |
| 3 | Entrada estricta | Los esquemas Pydantic usan `extra="forbid"`: un `user_id` en el cuerpo da `422` (ataque de asignación masiva) | `api/v1/schemas.py` |
| 4 | Claves foráneas compuestas | Aunque un error del backend se saltara la capa 2, la base **rechaza escrituras** que referencien datos de otro usuario (`(hijo_id, user_id) → padre(id, user_id)`) | migración `0001` |

## 3. Verificación de tokens (`TokenVerifier`)

Se comprueban siempre todas estas condiciones: firma, `exp` (obligatorio), `aud`, `iss`, `sub` presente y con
formato UUID, y `role = authenticated`. Detalles:

- **Lista de algoritmos fija por configuración**, nunca la de la cabecera del token. Con claves asimétricas
  solo `ES256`/`RS256`; con el secreto heredado solo `HS256`. Esto bloquea `alg=none` y la **confusión de
  algoritmo** (HS256 firmado con la clave pública).
- **JWKS**: la clave se obtiene del endpoint de Supabase (con caché) según el `kid` del token; un `kid` desconocido
  o un JWKS inaccesible implica **rechazo**, nunca aceptación.
- Los tokens `anon` y `service_role` (sin `sub` / otro `role`) se rechazan.
- El mensaje de error nunca devuelve el token ni el motivo detallado al cliente (la API responde `401` genérico).
- **Probado** con claves locales (ES256 y HS256) y con un servidor JWKS HTTP local: expirado, aún no válido,
  audiencia/emisor incorrectos, otra clave, payload manipulado, `alg=none`, confusión de algoritmo, basura.
  **No probado** contra un proyecto real de Supabase.

## 4. Otras protecciones

- Sin secretos en el repositorio: `.env` ignorado por git; `.env.example` sin credenciales reales.
- Las tablas no almacenan contraseñas ni correo (solo `profiles.id`, el `sub` del token). Probado.
- Los errores de integridad de la base se registran en el servidor y el cliente recibe un `409` sin detalles del esquema.
- XP: historial *append-only* por *trigger*, idempotencia y una sola recompensa por fuente (ver `data-model.md` §6).

## 5. Limitaciones de dejar RLS fuera de la V1

Row Level Security (RLS) añadiría una barrera **dentro de la base de datos** que protege incluso de errores del
backend. No se incluye en la V1 porque el único cliente es FastAPI, que conecta con un rol propietario (que
ignora RLS), y las políticas basadas en `auth.uid()` solo tienen sentido si el cliente consulta con el JWT del
usuario. Consecuencias que conviene conocer:

1. **Las lecturas dependen del código.** Las claves foráneas compuestas bloquean referencias cruzadas en *escritura*,
   pero un `SELECT` sin filtro por `user_id` no está bloqueado por la base. La defensa es `get_owned` y las pruebas.
   Un endpoint nuevo que olvide el filtro filtraría datos. **Regla del equipo:** ninguna consulta de una entidad
   de usuario sin `user_id` en el `WHERE` (o `get_owned`).
2. **Quien tenga las credenciales de la base lo ve todo** (administradores, copias de seguridad, herramientas de BI).
   Protege `DATABASE_URL` como el secreto más sensible.
3. **Exposición por la API REST de Supabase.** Las tablas de `public` se exponen por defecto a `anon` y
   `authenticated`. Si no se hace nada, **cualquiera con la clave pública podría leer o escribir tablas
   saltándose FastAPI**. Ejecuta `docs/supabase-hardening.sql` tras cada migración (activa RLS sin políticas y
   retira privilegios). Verificado con roles simulados en PostgreSQL local; **pendiente de verificar en un proyecto
   real de Supabase**. Alternativa: desactivar la Data API del proyecto.
4. **Sin auditoría por usuario** a nivel de base de datos.
5. **Datos sensibles sin cifrado a nivel de aplicación** (cuestionarios, y en el futuro diario y bienestar): están
   protegidos por el cifrado en reposo del proveedor, no por claves propias.
6. **Un token robado es válido hasta su `exp`.** No hay lista de revocación en la V1 (la sesión la gestiona Supabase).
7. **No hay limitación de tasa** (*rate limiting*) en la API.
8. Tres tablas "parte" (`habit_versions`, `habit_version_days`, `task_checklist_items`) no llevan `user_id`: su
   pertenencia se comprueba a través del padre.

### Cuándo y cómo añadir RLS

- **Obligatorio** si algún cliente (PWA, otra herramienta) accede a Supabase con el JWT del usuario.
- **Recomendado** al implementar el diario, el ánimo y los archivos multimedia (datos más sensibles).
- Plantilla de política, válida para toda tabla con `user_id`:

```sql
ALTER TABLE habits ENABLE ROW LEVEL SECURITY;
CREATE POLICY habits_owner ON habits FOR ALL TO authenticated
    USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
```

- Las tres tablas "parte" necesitarían una política con `EXISTS` sobre el padre, o añadirles `user_id` (las
  claves compuestas ya están pensadas para ello).
- Cada tabla nueva debe incluir sus políticas **en la misma migración** y una prueba que intente leer datos de otro usuario.
- Si se activa RLS mientras FastAPI sigue conectando como propietario, FastAPI no se ve afectado; para que RLS le
  proteja también a él habría que usar `FORCE ROW LEVEL SECURITY` y fijar el usuario por transacción
  (`SET LOCAL request.jwt.claims …`).

## 6. Lista de comprobación para producción

- [ ] `SUPABASE_JWT_ISSUER` y `SUPABASE_JWKS_URL` configurados (el servidor se niega a arrancar si falta la configuración de claves; probado).
- [ ] `docs/supabase-hardening.sql` ejecutado y comprobado con la clave pública (debe dar error de permisos).
- [ ] `DATABASE_URL` solo en el entorno del backend; rol dedicado con los privilegios mínimos que permita el proveedor.
- [ ] Copias de seguridad cifradas y probadas.
- [ ] Limitación de tasa delante de la API.
