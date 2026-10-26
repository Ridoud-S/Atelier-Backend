# Atelier — Modelo de datos V1

Este documento es el diccionario de datos y el registro de decisiones del esquema V1.
La fuente de verdad de la estructura es **Alembic** (`backend/migrations/`); este texto
describe esa estructura y el porqué. El diagrama está en [`erd.mmd`](erd.mmd).
Un test (`tests/test_docs_consistency.py`) comprueba que las tablas y columnas aquí listadas
coinciden con las del esquema real. [`schema.sql`](schema.sql) es un script generado desde
Alembic para inspeccionar o recrear el esquema (ver `database-setup.md`).

## 0. Convenciones

| Tema | Regla |
|---|---|
| Claves primarias | `uuid`, `DEFAULT gen_random_uuid()` (nativo desde PostgreSQL 13). Excepción: `profiles.id` es el id del usuario en Supabase Auth. |
| Nombres | `snake_case`, tablas en plural, código en inglés. |
| Fechas locales | Tipo `date`, sin zona. Ocurrencias de hábitos y misiones, fechas límite de tareas, `award_date` de XP. |
| Instantes | `timestamptz` (se guarda en UTC). Nunca se usa `timestamp` sin zona. |
| `created_at` / `updated_at` | En toda tabla mutable. `updated_at` lo mantiene el ORM (`onupdate`); un `UPDATE` SQL manual no lo toca. Las tablas append-only solo tienen `created_at`. |
| Estados y catálogos | `varchar` + `CHECK (col IN (...))`, **no** tipos `ENUM` nativos: añadir un valor es un cambio de `CHECK`, no un `ALTER TYPE`. |
| Pertenencia | Toda tabla de usuario lleva `user_id`. Las relaciones entre entidades del mismo usuario usan **claves foráneas compuestas** `(hijo_id, user_id) → padre(id, user_id)`, de modo que la base de datos rechaza referenciar datos de otro usuario. |
| Borrado | `ON DELETE RESTRICT` por defecto. `CASCADE` solo en dos tablas de "partes" sin historial propio (ver §7). |
| Nombres de restricciones | Convención de Alembic (`pk_`, `fk_`, `uq_`, `ck_`, `ix_`); las compuestas llevan nombre explícito. |

## 1. Decisiones explícitas (las diez preguntas del encargo)

**1. Hábitos, misiones y tareas sin modelos duplicados.** Son tres conceptos distintos y se
mantienen separados, pero comparten el vocabulario (`goal_type`, `target_value`, `unit`, `status`
de registro, `category`) para que no haya tres formas de describir "una meta":

- *Hábito*: compromiso recurrente y abierto del usuario. Tiene versiones (§4).
- *Misión*: asignación **acotada a una semana** que el sistema genera desde una plantilla dentro de un plan. Su ciclo de vida es distinto (se adapta cada semana), por eso no es un hábito con una bandera.
- *Tarea*: hecho puntual, con ciclo `todo → done`, no recurrente.

No hay tabla genérica "actividad". Las métricas distinguen la fuente (`habit_logs`, `mission_logs`, `tasks`) y nunca suman la misma ocurrencia dos veces porque cada tabla tiene su propia clave única de ocurrencia.

**2. Horarios y ocurrencias sin perder historial.** Un hábito es una identidad estable (`habits`) con **versiones** (`habit_versions`) que describen frecuencia y meta. Cada versión rige desde `effective_from` hasta el día anterior a la siguiente versión; **no se guarda fecha de fin**. Así no pueden existir solapamientos ni huecos (§4). Las ocurrencias no se pre-generan: solo existen filas en `habit_logs` cuando el usuario registra algo; lo "esperado" se calcula a partir de la versión vigente.

**3. No completar dos veces la misma ocurrencia.** `UNIQUE (habit_id, occurrence_date)` en `habit_logs` y `UNIQUE (mission_id, occurrence_date)` en `mission_logs`. Registrar de nuevo es una actualización (*upsert*), no una segunda fila.

**4. XP idempotente.** Tres capas independientes (§6): `UNIQUE (user_id, idempotency_key)`; `UNIQUE (<fuente>_id, event_type)` por cada fuente; y una transacción que serializa por usuario con `SELECT … FOR UPDATE` sobre `user_game_profiles`. El historial es append-only por *trigger*.

**5. Integridad entre usuarios.** Claves foráneas compuestas con `user_id` (§0), más validación en el backend que deriva el usuario del token (ver `security.md`).

**6. Evaluaciones y planes antiguos ante cambios de plantilla.** `assessment_versions` + `assessment_questions` son el cuestionario; cada `assessment` apunta a su versión y cada respuesta a una pregunta *de esa misma versión* (FK compuesta). Las plantillas (`habit_templates`) son **inmutables por convención**: un cambio crea otra fila con `version + 1`. Una misión apunta a la fila exacta de plantilla con la que se creó y lleva su propia meta adaptada. Nada se reinterpreta retroactivamente.

**7. Fechas, zonas horarias y cambio de horario.** El backend convierte "ahora" a la fecha local con la zona de `user_settings.timezone` y guarda `date`. Como una `date` no tiene hora, los cambios de horario de verano no afectan a las ocurrencias (§8).

**8. Archivar vs. eliminar.** Lo que tiene historial se archiva; solo se elimina físicamente lo que no lo tiene (§7).

**9. Estadísticas.** Todas se calculan bajo demanda desde `habit_logs`, `mission_logs` y `tasks`. La única cifra persistida es `user_game_profiles.total_xp`, una caché reconstruible (§6). No hay tabla de rachas: se derivan de los registros.

**10. V1 vs. posponer.** V1: 21 tablas (§2). Pospuestas: agenda, diario/ánimo/multimedia, Pomodoro, entrenamiento, mediciones corporales (§9), además de rachas persistidas, categorías definidas por el usuario y reversos de XP.

## 2. Diccionario de datos

Leyenda: **PK** clave primaria · **FK** clave foránea · **UQ** única · **N** admite `NULL`.
Todas las columnas son `NOT NULL` salvo que se marque **N**.

### 2.1 Usuarios y configuración

#### `profiles`
Un perfil por usuario de Supabase Auth. Sin contraseñas ni correo (los gestiona Supabase).

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK. Es el `sub` del token de Supabase. Sin default. |
| `display_name` | varchar(100) **N** | |
| `created_at`, `updated_at` | timestamptz | default `now()` |

Con Supabase en la misma base se puede añadir `FOREIGN KEY (id) REFERENCES auth.users(id)`; no se hace en la migración base porque `auth.users` no existe en una base PostgreSQL local.

#### `user_settings` (1:1 con `profiles`)
| Columna | Tipo | Notas |
|---|---|---|
| `user_id` | uuid | PK, FK → `profiles.id` RESTRICT |
| `timezone` | varchar(64) | default `'UTC'`. Nombre IANA; validado con `zoneinfo` en el backend (un `CHECK` no puede consultar `pg_timezone_names`). |
| `language` | varchar(10) | default `'es'`. `CHECK language ~ '^[a-z]{2}(-[A-Z]{2})?$'` |
| `theme` | varchar(10) | default `'system'`. `CHECK IN ('light','dark','system')` |
| `week_start_day` | smallint | default `1`. ISO: 1=lunes … 7=domingo. `CHECK BETWEEN 1 AND 7` |
| `time_format` | varchar(3) | default `'24h'`. `CHECK IN ('12h','24h')` |
| `notifications_enabled` | boolean | default `true` |
| `daily_reminder_time` | time **N** | hora local del recordatorio |
| `created_at`, `updated_at` | timestamptz | |

#### `user_game_profiles` (1:1 con `profiles`)
| Columna | Tipo | Notas |
|---|---|---|
| `user_id` | uuid | PK, FK → `profiles.id` RESTRICT |
| `total_xp` | bigint | default `0`, `CHECK >= 0`. **Caché** de `SUM(xp_transactions.amount)`. |
| `created_at`, `updated_at` | timestamptz | |

El **nivel no se almacena**: se deriva de `total_xp` con la fórmula configurable de §6.

### 2.2 Hábitos

#### `habits`
Identidad estable del hábito. Frecuencia y meta viven en `habit_versions`.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `user_id` | uuid | FK → `profiles.id` RESTRICT |
| `name` | varchar(120) | |
| `description` | text **N** | |
| `category` | varchar(20) | `CHECK IN ('sleep','hydration','nutrition','activity','focus','wellbeing','other')` |
| `origin` | varchar(10) | default `'user'`. `CHECK IN ('user','system')` |
| `source_template_id` | uuid **N** | FK → `habit_templates.id` RESTRICT. `CHECK (origin = 'system' OR source_template_id IS NULL)` |
| `end_date` | date **N** | fin natural opcional; no admite registros posteriores |
| `status` | varchar(10) | default `'active'`. `CHECK IN ('active','archived')` |
| `archived_at` | timestamptz **N** | `CHECK ((status = 'archived') = (archived_at IS NOT NULL))` |
| `created_at`, `updated_at` | timestamptz | |

- UQ `(id, user_id)` (destino de FKs compuestas).
- UQ parcial `(user_id, lower(name)) WHERE status = 'active'`.
- Índice `(user_id, status)`; índice en `source_template_id`.
- **La fecha de inicio no es columna**: es `MIN(habit_versions.effective_from)` (evita duplicar un dato derivable y que ambos discrepen).
- Todo hábito debe tener al menos una versión al confirmar la transacción (*constraint trigger* diferido).

#### `habit_versions`
Define cómo se interpreta el hábito desde `effective_from`.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `habit_id` | uuid | FK → `habits.id` RESTRICT |
| `effective_from` | date | primer día en que rige |
| `goal_type` | varchar(10) | `CHECK IN ('binary','quantity','duration')` |
| `target_value` | numeric(12,3) **N** | `binary` ⇒ `NULL`; los demás ⇒ `> 0` |
| `unit` | varchar(32) **N** | `binary` ⇒ `NULL`; `quantity` ⇒ obligatoria; `duration` ⇒ `'minutes'` |
| `frequency_type` | varchar(16) | `CHECK IN ('daily','weekdays','times_per_week')` |
| `weekly_target_count` | smallint **N** | solo `times_per_week`, entre 1 y 7; en los demás `NULL` |
| `created_at`, `updated_at` | timestamptz | |

- UQ `(habit_id, effective_from)` — impide dos versiones el mismo día.
- UQ `(id, habit_id)` — destino de la FK compuesta desde `habit_logs`.
- **Inmutable una vez referenciada** por algún `habit_logs` (*trigger*).

#### `habit_version_days`
Días ISO de la semana de una versión `weekdays`.

| Columna | Tipo | Notas |
|---|---|---|
| `habit_version_id` | uuid | PK (1/2), FK → `habit_versions.id` **CASCADE** |
| `weekday` | smallint | PK (2/2). `CHECK BETWEEN 1 AND 7` |

Tabla de "parte" (no tiene vida propia); se bloquea su modificación cuando la versión ya tiene registros. Que una versión `weekdays` tenga al menos un día lo valida el backend.

#### `habit_logs`
Un registro por hábito y fecha local de ocurrencia.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `user_id` | uuid | FK → `profiles.id` RESTRICT |
| `habit_id` | uuid | FK compuesta `(habit_id, user_id)` → `habits(id, user_id)` RESTRICT |
| `habit_version_id` | uuid | FK compuesta `(habit_version_id, habit_id)` → `habit_versions(id, habit_id)` RESTRICT |
| `occurrence_date` | date | fecha **local** del usuario |
| `status` | varchar(10) | `CHECK IN ('completed','partial','skipped')` |
| `value` | numeric(12,3) **N** | `CHECK (value IS NULL OR value >= 0)`; `skipped` ⇒ `NULL` |
| `created_at`, `updated_at` | timestamptz | |

- UQ `(habit_id, occurrence_date)`; UQ `(id, user_id)`.
- Índices: `(user_id, occurrence_date)`, `habit_version_id`.
- *Trigger*: `habit_version_id` debe ser **la versión vigente** para `occurrence_date`, y la fecha no puede superar `habits.end_date`.

**Sobre la instantánea de la meta (evaluación pedida).** Se guarda `habit_version_id` y **no** se copian `target_value`/`unit` al registro. Razones: (a) las versiones referenciadas son inmutables por *trigger*, así que el registro siempre se interpreta con la meta que tenía ese día; (b) el resultado ya está almacenado en `status`, por lo que nada se recalcula con la meta actual; (c) copiar la meta crearía dos fuentes de verdad que podrían discrepar. Si en el futuro se purgaran versiones antiguas, habría que añadir la instantánea en ese momento.

### 2.3 Tareas, checklists y proyectos

#### `projects`
| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `user_id` | uuid | FK → `profiles.id` RESTRICT |
| `name` | varchar(120) | |
| `description` | text **N** | |
| `status` | varchar(10) | default `'active'`. `CHECK IN ('active','completed','archived')` |
| `archived_at` | timestamptz **N** | `CHECK ((status = 'archived') = (archived_at IS NOT NULL))` |
| `created_at`, `updated_at` | timestamptz | |

UQ `(id, user_id)`; UQ parcial `(user_id, lower(name)) WHERE status <> 'archived'`; índice `(user_id, status)`.

#### `tasks`
| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `user_id` | uuid | FK → `profiles.id` RESTRICT |
| `project_id` | uuid **N** | FK compuesta `(project_id, user_id)` → `projects(id, user_id)` RESTRICT. Con `NULL` la FK no se evalúa: tarea independiente. |
| `title` | varchar(200) | |
| `description` | text **N** | |
| `priority` | varchar(10) | default `'medium'`. `CHECK IN ('low','medium','high')` |
| `status` | varchar(12) | default `'todo'`. `CHECK IN ('todo','in_progress','done','cancelled')` |
| `scheduled_date` | date **N** | fecha local en que se planea hacerla (alimenta la checklist diaria) |
| `due_date` | date **N** | `CHECK (due_date IS NULL OR scheduled_date IS NULL OR due_date >= scheduled_date)` |
| `completed_at` | timestamptz **N** | `CHECK ((status = 'done') = (completed_at IS NOT NULL))` |
| `created_at`, `updated_at` | timestamptz | |

UQ `(id, user_id)`. Índices: `(user_id, scheduled_date)`, `(user_id, status)`, `project_id`.

#### `task_checklist_items`
| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `task_id` | uuid | FK → `tasks.id` **CASCADE** |
| `title` | varchar(200) | |
| `is_done` | boolean | default `false` |
| `position` | integer | `CHECK >= 0`; UQ `(task_id, position)` **diferible** (permite reordenar en una transacción) |
| `created_at`, `updated_at` | timestamptz | |

Los ítems no otorgan XP; solo la tarea.

### 2.4 Evaluación inicial y mejora

#### `assessment_versions`
Versión publicada del cuestionario. Append-only por convención (se publica otra versión, no se edita).

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `version` | integer | UQ, `CHECK >= 1` |
| `title` | varchar(120) | |
| `is_active` | boolean | default `false`. Índice único parcial: **a lo sumo una** versión activa. |
| `created_at` | timestamptz | |

#### `assessment_questions`
| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `assessment_version_id` | uuid | FK → `assessment_versions.id` RESTRICT |
| `question_key` | varchar(64) | UQ `(assessment_version_id, question_key)` |
| `domain` | varchar(20) | `CHECK IN ('sleep','hydration','nutrition','activity','focus','wellbeing','goals','availability')` |
| `prompt` | text | |
| `answer_type` | varchar(16) | `CHECK IN ('scale','number','single_choice','multi_choice','text')` |
| `options` | jsonb **N** | solo configuración estática de la pregunta (rango de escala, lista de opciones). No son datos del usuario. |
| `position` | integer | UQ `(assessment_version_id, position)` |
| `is_required` | boolean | default `true` |
| `created_at` | timestamptz | |

UQ `(id, assessment_version_id)` (destino de FK compuesta desde las respuestas).

#### `assessments`
| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `user_id` | uuid | FK → `profiles.id` RESTRICT |
| `assessment_version_id` | uuid | FK → `assessment_versions.id` RESTRICT |
| `status` | varchar(12) | default `'in_progress'`. `CHECK IN ('in_progress','completed','abandoned')` |
| `started_at` | timestamptz | default `now()` |
| `completed_at` | timestamptz **N** | `CHECK ((status = 'completed') = (completed_at IS NOT NULL))` |
| `created_at`, `updated_at` | timestamptz | |

UQ `(id, user_id)`; UQ `(id, assessment_version_id)`; UQ parcial `(user_id) WHERE status = 'in_progress'` (una evaluación abierta por usuario); índice `(user_id, started_at)`.

#### `assessment_answers`
| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `user_id` | uuid | |
| `assessment_id` | uuid | |
| `assessment_version_id` | uuid | |
| `question_id` | uuid | |
| `value_number` | numeric(12,3) **N** | |
| `value_text` | text **N** | |
| `value_options` | jsonb **N** | lista de opciones elegidas (`multi_choice`) |
| `created_at`, `updated_at` | timestamptz | |

- `CHECK (num_nonnulls(value_number, value_text, value_options) = 1)`.
- UQ `(assessment_id, question_id)`.
- FK compuesta `(assessment_id, user_id)` → `assessments(id, user_id)`.
- FK compuesta `(assessment_id, assessment_version_id)` → `assessments(id, assessment_version_id)`.
- FK compuesta `(question_id, assessment_version_id)` → `assessment_questions(id, assessment_version_id)` ⇒ **la pregunta pertenece a la versión de la evaluación**.
- `assessment_version_id` aquí es una desnormalización deliberada: la FK compuesta impide que discrepe de la de la evaluación (§10).

#### `habit_templates`
Catálogo del sistema (no pertenece a ningún usuario). Inmutable por convención.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `code` | varchar(64) | UQ `(code, version)` |
| `version` | integer | `CHECK >= 1` |
| `title` | varchar(120) | |
| `description` | text **N** | |
| `category` | varchar(20) | mismos valores que `habits.category` |
| `goal_type`, `target_value`, `unit` | — | mismas reglas que `habit_versions` |
| `default_weekly_target_count` | smallint | `CHECK BETWEEN 1 AND 7` |
| `difficulty` | smallint | `CHECK BETWEEN 1 AND 5`; base de la progresión gradual |
| `is_active` | boolean | default `true`; las plantillas se desactivan, no se borran |
| `created_at` | timestamptz | |

#### `improvement_plans`
| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `user_id` | uuid | FK → `profiles.id` |
| `assessment_id` | uuid | FK compuesta `(assessment_id, user_id)` → `assessments(id, user_id)`; UQ (un plan por evaluación) |
| `status` | varchar(10) | default `'active'`. `CHECK IN ('active','completed','archived')` |
| `started_on` | date | |
| `ended_on` | date **N** | `CHECK ((status = 'active') = (ended_on IS NULL))` y `CHECK (ended_on IS NULL OR ended_on >= started_on)` |
| `created_at`, `updated_at` | timestamptz | |

UQ `(id, user_id)`; UQ parcial `(user_id) WHERE status = 'active'` (un plan activo por usuario).

#### `missions`
Una misión = una plantilla asignada **para una semana** de un plan.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `user_id` | uuid | FK → `profiles.id` |
| `plan_id` | uuid | FK compuesta `(plan_id, user_id)` → `improvement_plans(id, user_id)` |
| `habit_template_id` | uuid | FK → `habit_templates.id` RESTRICT (fila exacta con la que se creó) |
| `week_start` | date | primer día de la semana (según `week_start_day`); la semana termina en `week_start + 6` (derivado, no se guarda) |
| `weekly_target_count` | smallint | `CHECK BETWEEN 1 AND 7`; adaptable cada semana |
| `target_value` | numeric(12,3) **N** | `CHECK (target_value IS NULL OR target_value > 0)`; meta adaptada |
| `unit` | varchar(32) **N** | |
| `status` | varchar(12) | default `'active'`. `CHECK IN ('active','completed','abandoned','superseded')` |
| `adapted_from_mission_id` | uuid **N** | FK compuesta `(adapted_from_mission_id, user_id)` → `missions(id, user_id)` (autorreferencia) |
| `created_at`, `updated_at` | timestamptz | |

UQ `(plan_id, habit_template_id, week_start)`; UQ `(id, user_id)`; índices `(user_id, week_start)`, `(user_id, status)`, `plan_id`, `habit_template_id`.

#### `mission_logs`
| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `user_id` | uuid | |
| `mission_id` | uuid | FK compuesta `(mission_id, user_id)` → `missions(id, user_id)` RESTRICT |
| `occurrence_date` | date | *trigger*: dentro de `[week_start, week_start + 6]` de su misión |
| `status` | varchar(10) | `CHECK IN ('completed','partial','skipped')` |
| `value` | numeric(12,3) **N** | `CHECK >= 0`; `skipped` ⇒ `NULL` |
| `perceived_difficulty` | smallint **N** | `CHECK BETWEEN 1 AND 5` |
| `created_at`, `updated_at` | timestamptz | |

UQ `(mission_id, occurrence_date)`; UQ `(id, user_id)`; índice `(user_id, occurrence_date)`.

#### `weekly_reviews`
| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `user_id` | uuid | |
| `plan_id` | uuid | FK compuesta `(plan_id, user_id)` → `improvement_plans(id, user_id)` |
| `week_start` | date | UQ `(plan_id, week_start)` |
| `perceived_difficulty` | smallint | `CHECK BETWEEN 1 AND 5` |
| `energy_level` | smallint | `CHECK BETWEEN 1 AND 5` |
| `notes` | text **N** | |
| `created_at`, `updated_at` | timestamptz | |

El porcentaje de cumplimiento de la semana **no** se guarda: se calcula de `mission_logs`.

### 2.5 Gamificación

#### `xp_rules`
Reglas de recompensa, versionadas por fecha. Una regla se **cambia insertando otra fila** con `effective_from` posterior; no se edita (las transacciones apuntan a la fila usada).

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `rule_code` | varchar(40) | p. ej. `habit.completed`, `mission.completed`, `task.completed.high` |
| `effective_from` | date | UQ `(rule_code, effective_from)` |
| `source_type` | varchar(12) | `CHECK IN ('habit_log','mission_log','task')` |
| `xp_amount` | integer | `CHECK >= 0` |
| `daily_cap` | integer **N** | máximo de XP por usuario y día local **para este `rule_code`**; `NULL` = sin tope; `CHECK >= 0` |
| `created_at` | timestamptz | |

#### `xp_transactions`
Historial **append-only**: un `UPDATE` o `DELETE` lanza error (*trigger*).

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid | PK |
| `user_id` | uuid | FK → `profiles.id` RESTRICT |
| `xp_rule_id` | uuid | FK → `xp_rules.id` RESTRICT |
| `event_type` | varchar(16) | default `'completion'`. `CHECK IN ('completion')` en V1 (ver §6.4) |
| `habit_log_id` | uuid **N** | FK compuesta `(habit_log_id, user_id)` → `habit_logs(id, user_id)` RESTRICT |
| `mission_log_id` | uuid **N** | FK compuesta → `mission_logs(id, user_id)` RESTRICT |
| `task_id` | uuid **N** | FK compuesta → `tasks(id, user_id)` RESTRICT |
| `amount` | integer | `CHECK >= 0`. Puede ser 0 cuando el tope diario ya se agotó (consume la recompensa). |
| `award_date` | date | fecha local usada para el tope diario |
| `idempotency_key` | varchar(120) | UQ `(user_id, idempotency_key)` |
| `created_at` | timestamptz | |

- `CHECK (num_nonnulls(habit_log_id, mission_log_id, task_id) = 1)` — exactamente una fuente (alternativa a una relación polimórfica sin integridad referencial: tres FKs reales y un `CHECK`).
- UQ `(habit_log_id, event_type)`, UQ `(mission_log_id, event_type)`, UQ `(task_id, event_type)`.
- Índices: `(user_id, award_date)`, `(user_id, created_at)`, `xp_rule_id`.

## 3. Estados válidos y transiciones

La base de datos garantiza los **valores** (`CHECK`) y las coherencias estado↔marca de tiempo. Las **transiciones** las valida el backend (`app/domain/transitions.py`) y están cubiertas por pruebas.

| Entidad | Estados | Transiciones permitidas |
|---|---|---|
| Hábito | `active`, `archived` | `active ↔ archived` |
| Proyecto | `active`, `completed`, `archived` | `active → completed`, `completed → active`, `active\|completed → archived`, `archived → active` |
| Tarea | `todo`, `in_progress`, `done`, `cancelled` | `todo → in_progress\|done\|cancelled`; `in_progress → todo\|done\|cancelled`; `done → todo\|in_progress` (reabrir); `cancelled → todo` |
| Evaluación | `in_progress`, `completed`, `abandoned` | `in_progress → completed\|abandoned`; los otros dos son terminales |
| Plan | `active`, `completed`, `archived` | `active → completed\|archived`; `completed → archived`; `archived` terminal |
| Misión | `active`, `completed`, `abandoned`, `superseded` | `active → completed\|abandoned\|superseded`; los demás son terminales |
| Registro de hábito/misión | `completed`, `partial`, `skipped` | Cualquiera → cualquiera (son correcciones de un hecho que el usuario registró). El XP **no** se revoca ni se vuelve a conceder (§6). |

Reglas de decisión del estado de un registro: hábito `binary` → `completed` si el usuario lo marca; `quantity`/`duration` → `completed` si `value >= target_value` de la versión vigente, `partial` si `0 < value < target_value`; `skipped` solo por acción explícita.

## 4. Versiones de hábitos: reglas

Modelo: una versión rige desde `effective_from` hasta el día anterior a la siguiente versión del mismo hábito (o indefinidamente si es la última y el hábito no tiene `end_date`).

- **Sin solapamientos ni huecos por construcción**: no hay fecha de fin que pueda contradecir a la siguiente versión; `UNIQUE (habit_id, effective_from)` basta. No se necesita `EXCLUDE`, y por tanto **la migración no depende de `btree_gist`** (en el entorno de desarrollo está disponible, v1.7, pero no se usa).
- **Sin hueco inicial**: la fecha de inicio *es* la primera `effective_from`; un hábito sin versiones no puede confirmarse.
- **Versión futura**: se permite insertar una versión con `effective_from` posterior a hoy; entra en vigor sola ese día. Mientras no tenga registros puede editarse o borrarse.
- **Cambio retroactivo**: una versión nueva cuyo `effective_from` sea **igual o anterior al último día con registros** del hábito se **rechaza** (*trigger*), porque reinterpretaría días ya registrados. Los registros mantienen la versión con la que se hicieron; no se recalculan con la meta actual. Si el usuario necesita corregir días pasados, la operación futura prevista es "reasignar un registro a otra versión" de forma explícita y auditada, nunca automática.
- **Inmutabilidad**: una versión referenciada por algún registro no puede modificar frecuencia, meta, unidad ni `effective_from`, ni sus días.
- **Concurrencia**: crear versiones bloquea la fila del hábito (`FOR UPDATE`) y registrar un día la bloquea en modo compartido (`FOR SHARE`), de modo que no se puede colar un registro mientras se crea una versión.
- **Cambios de horario del usuario**: no crean versiones; la frecuencia se evalúa en fechas locales.

## 5. Progreso semanal de `times_per_week`

Semana = 7 días desde `week_start_day` (por defecto lunes). Para una semana `W` y un hábito:

1. **Ventana elegible**: días de `W` con `día >= primera effective_from` y `día <= end_date` (si existe). Si la ventana está vacía, el hábito no cuenta esa semana.
2. **Esperado por día** según la versión vigente ese día: `daily` → 1; `weekdays` → 1 si el día está programado, 0 si no; `times_per_week` → `weekly_target_count / 7`.
3. **Meta de la semana** = `ceil(Σ esperado)`, acotada a `[1, días de la ventana]`. Con esto una **semana parcial** (el hábito empieza el jueves) o una semana con **cambio de configuración** se prorratean de forma determinista. Ejemplos con meta 3/semana: semana completa → 3; empieza el jueves (4 días) → `ceil(3·4/7) = 2`; versión A (2/sem) lun–mié + versión B (5/sem) jue–dom → `ceil(2·3/7 + 5·4/7) = 4`.
4. **Cumplimientos válidos**: filas `habit_logs` con `status = 'completed'` en fechas locales **distintas** dentro de la ventana (la unicidad `(habit_id, occurrence_date)` garantiza fechas distintas). `partial` y `skipped` no cuentan ni reducen la meta. En `weekdays`, solo cuentan los días programados; los demás se muestran como extra.
5. **Métricas**: `progress_ratio = válidos / meta` (**sin tope**, puede ser > 1); `compliance_pct = min(progress_ratio, 1) × 100` (**tope 100 %**); `extra_count = max(válidos − meta, 0)` más los completados en días no programados.
6. La semana en curso usa la meta completa (no se trunca en "hoy"), de modo que muestra avance hacia el objetivo.

Cumplimiento diario: se calcula solo para hábitos `daily` y `weekdays` programados ese día; los `times_per_week` aparecen en métricas semanales y no en el denominador diario.

### 5.1 Racha de actividad (`services/streaks.py`)

Un día es **activo** si el usuario **completó** (`status = 'completed'`) al menos un hábito —propio o generado por
el sistema— o una misión ese día. Las tareas no cuentan, ni `partial` ni `skipped`. Cada fecha cuenta una vez
aunque haya varios cumplimientos, de modo que hábitos y misiones **no se suman dos veces**.

- **Racha actual**: días consecutivos hasta hoy (fecha local). Si hoy todavía no hay actividad, la racha no se
  rompe hasta que termine el día: se cuenta hacia atrás desde ayer.
- **Racha máxima**: la mayor secuencia histórica.
- Se calcula desde `habit_logs` y `mission_logs`; no hay tabla de rachas. Si el volumen lo exigiera, se podría
  paginar por fechas antes de persistir nada.
- Pendiente (no implementado): rachas por hábito (días consecutivos de un hábito `daily`/`weekdays`, o semanas
  consecutivas con `compliance_pct = 100` en `times_per_week`).

## 6. XP e idempotencia

### 6.1 Reglas y recompensa
- `xp_rules` define cuánto da cada fuente: `habit.completed`, `mission.completed`, `task.completed.low|medium|high`. La regla aplicable es la de mayor `effective_from <= award_date` para ese `rule_code`.
- **Solo `completed` da XP.** `partial` y `skipped` no.

### 6.2 Algoritmo transaccional (`app/services/xp.py::award_xp`)
En **una sola transacción**:
1. `INSERT … ON CONFLICT DO NOTHING` en `user_game_profiles` y `SELECT … FOR UPDATE` de esa fila → serializa todas las recompensas del mismo usuario (hace seguro el cálculo del tope).
2. Si ya existe una transacción con la misma `idempotency_key`, se devuelve esa (`created = False`) sin tocar nada.
3. Se elige la regla vigente y se calcula `amount = min(xp_amount, tope − ya_otorgado_ese_día_para_ese_rule_code)` (nunca negativo).
4. Se inserta la transacción (si una carrera llegara aquí, `UNIQUE` lo rechaza) y se hace `total_xp = total_xp + amount`.
5. `COMMIT`. Un fallo en cualquier punto revierte transacción y total juntos.

Clave de idempotencia: `habit_log:{id}:completion`, `mission_log:{id}:completion`, `task:{id}:completion`.

### 6.3 Reconstrucción y nivel
- `total_xp` siempre debe igualar `SELECT COALESCE(SUM(amount), 0) FROM xp_transactions WHERE user_id = …`. `reconcile_total_xp()` lo verifica y lo corrige (la corrección toca la caché, nunca el historial).
- Nivel: `xp_requerido(L) = round(LEVEL_BASE_XP · (L − 1) ^ LEVEL_EXPONENT)` con `L ≥ 1` (por defecto base 100, exponente 1,5; nivel 1 = 0 XP, nivel 2 = 100, nivel 3 ≈ 283, nivel 4 ≈ 520). El nivel es el mayor `L` con `xp_requerido(L) ≤ total_xp`. Ambos parámetros están en `Settings`.

### 6.4 Una tarea da XP una sola vez en su ciclo de vida
`UNIQUE (task_id, event_type)` y la clave `task:{id}:completion` hacen que completar, reabrir (`done → todo`) y volver a completar **no** genere otra recompensa. Tampoco se retira el XP al reabrir (retirarlo y volver a darlo sería incompatible con "una vez"; y no retirarlo no permite farmear).

**Recompensas adicionales en el futuro** (estrategia explícita): se añade un valor a `event_type` (p. ej. `'streak_bonus'`, `'first_of_week'`) ampliando el `CHECK`, y cada evento define su propia clave (`habit:{id}:streak_bonus:2026-W43`). La unicidad ya es por `(fuente, event_type)`, así que cada tipo de evento se concede una vez por fuente sin cambiar el esquema. Los eventos sin fuente única (bonos semanales) requerirían una fuente nueva (nueva columna FK o tabla de eventos) y se diseñarán cuando exista el requisito. Los **reversos** (corregir un XP mal otorgado) se harían con filas compensatorias enlazadas a la original; no existen en V1.

## 7. Archivar y eliminar

| Entidad | Política |
|---|---|
| Hábito | **Archivar** (`status = 'archived'`). No se elimina si tiene registros: `habit_logs → habits` es RESTRICT. Un hábito sin registros ni XP puede eliminarse (borrando antes sus versiones, que también se permite si no tienen registros). |
| Versión de hábito | Eliminable solo sin registros y sin ser la última versión. |
| Proyecto | **Archivar**. Sus tareas se conservan (`RESTRICT`). |
| Tarea | `cancelled`/`done` conservan el historial. Eliminable solo si nunca generó XP (`xp_transactions → tasks` RESTRICT). |
| Checklist ítem | Eliminable libremente. `CASCADE` al borrar la tarea. |
| Registro de hábito/misión | No se elimina si generó XP (RESTRICT); se corrige cambiando su `status`. |
| Plan / misión | **Archivar / cambiar estado**; no se elimina historial. |
| Evaluación y respuestas | Se conservan siempre. |
| Plantillas / reglas XP | Se desactivan o se versionan; nunca se borran si están referenciadas. |
| `xp_transactions` | Nunca se modifican ni eliminan. |
| Perfil | `RESTRICT`: no se puede borrar un usuario con datos. El borrado definitivo de cuenta (derecho de supresión) requerirá un procedimiento dedicado que elimine datos en orden y que se diseñará cuando se necesite. |

`CASCADE` solo existe en: `habit_version_days → habit_versions`, `task_checklist_items → tasks`. Son partes dependientes sin historial propio.

## 8. Fechas y zonas horarias

- El día termina a **medianoche en la zona horaria del usuario**. El backend calcula la fecha local con `zoneinfo.ZoneInfo(user_settings.timezone)`. El cliente envía la fecha de la ocurrencia.
- **Ventana de registro**: solo se puede registrar (o corregir) una ocurrencia de hábito o misión de hoy a `MAX_BACKFILL_DAYS` días atrás (por defecto **7**; `0` = solo hoy). Se evalúa en la fecha local del usuario. Fechas futuras se rechazan. Es una regla del backend (depende de la fecha actual, no puede ser un `CHECK`). Evita rellenar semanas para ganar XP o reparar una racha.
- La XP se concede con `award_date` = **hoy**, no la fecha rellenada, para que un día pasado no eluda el tope diario.
- `date` no lleva hora: DST no desplaza ocurrencias ni semanas.
- Si el usuario cambia de zona, **las fechas ya guardadas no se reescriben**; solo cambia qué día es "hoy" a partir de entonces. Efecto aceptado: un día puede quedar con dos registros posibles o ninguno en el día del cambio de huso; el `UNIQUE` evita duplicados.
- `award_date` guarda la fecha local en el momento de la recompensa para que los topes diarios no cambien si después se modifica la zona.

## 9. Módulos futuros (no implementados)

El patrón para añadirlos sin tocar lo existente: tabla nueva con `user_id`, `UNIQUE (id, user_id)` y FKs compuestas, migración Alembic propia.

| Módulo | Cómo extender |
|---|---|
| Agenda / eventos | `calendar_events (id, user_id, title, starts_at timestamptz, ends_at timestamptz, all_day, timezone, …)`. Una tarea puede enlazar `tasks.calendar_event_id` (FK compuesta, nullable). Eventos recurrentes: regla `RRULE` en columna + excepciones en tabla hija. |
| Diario, ánimo, multimedia | `journal_entries (user_id, entry_date, body, mood smallint)` con `UNIQUE (user_id, entry_date)` y `journal_media` (ruta del objeto en almacenamiento, no el binario). Datos sensibles: RLS recomendada desde el inicio de este módulo. |
| Pomodoro / concentración | `focus_sessions (user_id, started_at, ended_at, planned_minutes, task_id nullable)`; XP por un nuevo `event_type` o fuente. |
| Entrenamiento | `exercises` (catálogo), `workout_routines`, `workout_sessions`, `workout_sets (session_id, exercise_id, set_number, reps, weight)`. |
| Mediciones corporales | `body_measurements (user_id, measured_on, metric_type, value, unit)` con `UNIQUE (user_id, measured_on, metric_type)`. |

Si algún módulo necesitara apuntar a "cualquier entidad" (p. ej. adjuntos), usar **una tabla de enlace por tipo** (`task_attachments`, `journal_media`) en lugar de `entity_type + entity_id`, que no admite clave foránea. Esa es la alternativa documentada a las relaciones polimórficas.

## 10. Excepciones justificadas a 3FN y desnormalizaciones

| Dónde | Qué | Por qué es aceptable |
|---|---|---|
| `user_id` en tablas hijas (`habit_logs`, `tasks`, `missions`, …) | Es derivable del padre | Lo exige el aislamiento: las FK compuestas lo hacen **imposible de contradecir** y permiten filtrar por usuario sin *joins* y activar RLS más adelante con una política trivial. |
| `assessment_answers.assessment_version_id` | Derivable de la evaluación | Permite que la FK compuesta verifique que la pregunta pertenece a la versión de la evaluación. No puede discrepar. |
| `user_game_profiles.total_xp` | Agregado de `xp_transactions` | Caché por rendimiento (lectura O(1) del perfil). Se actualiza en la misma transacción que el historial y es reconstruible/verificable. |
| `missions.target_value/unit` | Copia adaptable de la plantilla | No es redundancia: es la meta **adaptada** de esa semana, distinta de la plantilla. |
| `xp_transactions.amount` | Podría derivarse de la regla | Con topes diarios el importe real puede ser menor que la regla; es un hecho histórico, no derivable. |
| `xp_transactions.award_date` | Derivable de `created_at` + zona | La zona puede cambiar; se fija el hecho. |
| `habit_logs.status` | Derivable de `value` y la meta | Registra la decisión del momento (y `skipped` es intención del usuario); evita recalcular con metas distintas. |
| Estado + marca de tiempo (`archived_at`, `completed_at`, `ended_on`) | Aparente duplicado | `CHECK` los mantiene coherentes; la marca guarda *cuándo*, el estado *qué*. |
