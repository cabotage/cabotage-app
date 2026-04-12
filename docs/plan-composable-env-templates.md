# Composable Environment Variable Templates

## Context

Backing service configs create individual env vars (`MAIN_PGHOST`, `MAIN_PGPASSWORD`, etc.) via Consul/Vault. Users want to compose these into a single `DATABASE_URL` without duplicating secrets as static strings. The current template system (`${shared.SECRET}`) can only use secrets as whole values (for envconsul key renaming), not interpolate them into larger strings.

**Root cause**: envconsul's `exec.env.custom` is a static JSON array — no variable expansion. And secrets can't be resolved at build time because they'd be baked into the release config.

**Solution**: Add a thin shell wrapper to the entrypoint that evaluates env var references in designated config values, AFTER envconsul has populated all individual vars from Consul/Vault.

---

## Design

### New template syntax: `${env.VAR_NAME}`

A new pattern `${env.VAR_NAME}` that means "substitute the value of env var `VAR_NAME` at runtime." This differs from existing patterns:

- `${app_slug.url}` — resolved at **build time**, result placed in `exec.env.custom`
- `${shared.VAR}` — non-secret resolved at **build time**; secret used for envconsul key renaming
- `${env.VAR}` — **NEW**: resolved at **container startup**, after envconsul populates the environment

### Example usage

Resource creates these EnvironmentConfigurations:
```
MAIN_PGHOST     = "cluster-rw.namespace.svc"   (non-secret, Consul)
MAIN_PGPORT     = "5432"                        (non-secret, Consul)
MAIN_PGDATABASE = "app"                         (non-secret, Consul)
MAIN_PGUSER     = "app"                         (non-secret, Consul)
MAIN_PGPASSWORD = "s3cret"                      (secret, Vault)
MAIN_DATABASE_URL = "postgresql://${env.MAIN_PGUSER}:${env.MAIN_PGPASSWORD}@${env.MAIN_PGHOST}:${env.MAIN_PGPORT}/${env.MAIN_PGDATABASE}?sslmode=verify-full"
```

At runtime, after envconsul loads all individual vars, the entrypoint evaluates `${env.*}` references using the populated environment, producing:
```
MAIN_DATABASE_URL=postgresql://app:s3cret@cluster-rw.namespace.svc:5432/app?sslmode=verify-full
```

### Implementation

#### 1. Template pattern recognition (`cabotage/utils/config_templates.py`)

Add a new regex:
```python
ENV_REF_PATTERN = re.compile(r"\$\{env\.([a-zA-Z_][a-zA-Z0-9_]*)\}")
```

Update `has_template_variables()` to also match `${env.*}`.

Add `has_env_references(value)` — returns True if value contains `${env.*}` patterns.

Add `convert_env_refs_to_shell(value)` — converts `${env.VAR}` to `$VAR` for shell eval:
```python
def convert_env_refs_to_shell(value):
    return ENV_REF_PATTERN.sub(r'$\1', value)
```

#### 2. envconsul config generation (`cabotage/server/models/projects.py` ~line 955)

In `envconsul_configurations`, configs with `${env.*}` references should NOT be written to Consul/Vault (they'd be meaningless there). Instead they get collected into a separate list of "runtime-evaluated" vars.

In the existing flow:
```python
for c in config_objects:
    if has_template_variables(c.value):
        # existing template handling...
    else:
        stmt = c.envconsul_statement
        ...
```

Add handling for `${env.*}`:
```python
runtime_eval_env = []
for c in config_objects:
    if has_env_references(c.value):
        # Convert ${env.VAR} to $VAR for shell eval
        runtime_eval_env.append(f"{c.name}={convert_env_refs_to_shell(c.value)}")
    elif has_template_variables(c.value):
        # existing handling...
    else:
        # existing handling...
```

The `runtime_eval_env` list gets written into the envconsul HCL as a new field that the entrypoint will process.

#### 3. Entrypoint modification (`cabotage/utils/release_build_context.py`)

Current entrypoint:
```sh
#!/bin/sh
export VAULT_TOKEN=$(cat /var/run/secrets/vault/vault-token)
export CONSUL_TOKEN=$(cat /var/run/secrets/vault/consul-token)
exec "${@}"
```

The challenge: the entrypoint runs BEFORE envconsul, so it can't do the eval. envconsul is `"${@}"`. The eval needs to happen AFTER envconsul populates the env but BEFORE the app starts.

envconsul execs the command specified in `exec { command = "..." }`. Currently that's `/bin/sh` for the shell process, and the actual process command for named processes. The runtime eval needs to happen IN that command.

**Approach**: Change the exec command to a wrapper that evaluates the runtime vars, then execs the real command.

For the envconsul HCL, instead of:
```hcl
exec {
  command = "/bin/sh"
  env = { custom = [...] }
}
```

Generate:
```hcl
exec {
  command = "/bin/sh"
  args = ["-c", "export MAIN_DATABASE_URL=\"postgresql://$MAIN_PGUSER:$MAIN_PGPASSWORD@$MAIN_PGHOST:$MAIN_PGPORT/$MAIN_PGDATABASE?sslmode=verify-full\"; exec /bin/sh"]
  env = { custom = [...] }
}
```

Wait — envconsul's `exec.command` doesn't take args like that. Let me check...

Actually, looking at the deploy code more carefully:

```python
args=args,  # ["envconsul", "-kill-signal=SIGTERM", "-config=/etc/cabotage/envconsul-{process}.hcl"]
```

And the HCL has:
```hcl
exec {
  command = "/bin/sh"
}
```

envconsul execs `/bin/sh` which gets the process-specific args as stdin/args. Actually no — looking at the process-specific configs, the command IS the actual process command (e.g., `gunicorn ...`).

**Simpler approach**: Write the runtime-eval vars to a file in the envconsul config, and source it from a wrapper script.

**Simplest approach**: Just put them as `eval`-able exports in `exec.env.custom` and change the exec command to evaluate them. Since `exec.env.custom` items are set as plain env vars by envconsul, we can store the TEMPLATE as the custom var value, then have a wrapper script that re-evaluates env vars that contain shell variable references.

Actually, the cleanest approach:

#### Revised approach: Wrapper script

Add a second script `/eval-env.sh` to the release image:

```sh
#!/bin/sh
# Evaluate runtime environment variable templates.
# Variables listed in CABOTAGE_EVAL_ENV get shell-expanded
# against the current environment (populated by envconsul).
if [ -n "$CABOTAGE_EVAL_ENV" ]; then
  for var in $CABOTAGE_EVAL_ENV; do
    eval "export $var=\"\$(eval echo \"\$$var\")\""
  done
  unset CABOTAGE_EVAL_ENV
fi
exec "$@"
```

Then in the envconsul HCL, change:
```hcl
exec {
  command = "/bin/sh"
}
```
to:
```hcl
exec {
  command = "/eval-env.sh"
  args = ["/bin/sh"]
}
```

And add the template vars to `exec.env.custom` with shell variable syntax:
```
custom = ["MAIN_DATABASE_URL=postgresql://$MAIN_PGUSER:$MAIN_PGPASSWORD@$MAIN_PGHOST:$MAIN_PGPORT/$MAIN_PGDATABASE?sslmode=verify-full", "CABOTAGE_EVAL_ENV=MAIN_DATABASE_URL"]
```

Wait, this won't work either — `exec.env.custom` sets the value literally, no expansion. So `$MAIN_PGPASSWORD` stays as the literal string `$MAIN_PGPASSWORD`.

**The actual solution**: The wrapper script needs to know which vars to re-evaluate, and the TEMPLATE must be stored somewhere it can be read with expansion.

Let me think about this differently. After envconsul runs:
1. All individual vars are in the environment (`MAIN_PGHOST=cluster.svc`, `MAIN_PGPASSWORD=secret`, etc.)
2. Custom vars are set literally (`MAIN_DATABASE_URL=postgresql://$MAIN_PGUSER:$MAIN_PGPASSWORD@...`)
3. The exec command runs

If the exec command is a shell wrapper that does `eval export MAIN_DATABASE_URL`, the shell will expand `$MAIN_PGUSER` etc. from the environment that envconsul already populated.

So the flow:
1. envconsul loads vars from Consul/Vault
2. envconsul sets custom vars (including templates with `$VAR` syntax) 
3. envconsul execs `/eval-env.sh`
4. `/eval-env.sh` does `eval export` on designated vars, expanding `$VAR` references
5. `/eval-env.sh` execs the real command

#### Final implementation plan

**`cabotage/utils/config_templates.py`**:
- Add `ENV_REF_PATTERN = re.compile(r"\$\{env\.([a-zA-Z_][a-zA-Z0-9_]*)\}")` 
- Add `has_env_references(value)` 
- Add `convert_env_refs_to_shell(value)` — converts `${env.VAR}` → `$VAR`
- Update `has_template_variables()` to also detect `${env.*}`

**`cabotage/server/models/projects.py`** (`envconsul_configurations`):
- Detect configs with `${env.*}` references
- Convert them to shell syntax and add to `exec.env.custom`
- Track their names in a `CABOTAGE_EVAL_ENV` custom var (space-separated list)
- Change exec command from the actual command to `/eval-env.sh` wrapping the actual command when runtime-eval vars exist

**`cabotage/utils/release_build_context.py`**:
- Add `EVAL_ENV_SCRIPT` — the `/eval-env.sh` wrapper
- Include it in the Dockerfile COPY and the configmap

**`cabotage/celery/tasks/resources.py`**:
- Change `_postgres_config_entries` to emit `DATABASE_URL` as a `${env.*}` template instead of a static secret string
- Same for `_redis_config_entries` with `REDIS_URL`

**`cabotage/server/user/views.py`** and templates:
- The `${env.*}` pattern should be recognized as a template variable in the UI (so it doesn't get written to Consul/Vault)

---

## Files to modify

| File | Change |
|------|--------|
| `cabotage/utils/config_templates.py` | Add `ENV_REF_PATTERN`, `has_env_references()`, `convert_env_refs_to_shell()`, update `has_template_variables()` |
| `cabotage/server/models/projects.py` | Update `envconsul_configurations` to handle `${env.*}` vars |
| `cabotage/utils/release_build_context.py` | Add `/eval-env.sh` script to release image |
| `cabotage/celery/tasks/resources.py` | Change `DATABASE_URL` and `REDIS_URL` to use `${env.*}` templates |

## Verification

1. Create a postgres resource, verify individual env configs created
2. Verify `DATABASE_URL` is stored as `postgresql://${env.MAIN_PGUSER}:${env.MAIN_PGPASSWORD}@${env.MAIN_PGHOST}:${env.MAIN_PGPORT}/${env.MAIN_PGDATABASE}?sslmode=verify-full`
3. Build a release — verify envconsul HCL has the shell template in `exec.env.custom` and `CABOTAGE_EVAL_ENV` is set
4. Deploy — verify the running container has `DATABASE_URL` with expanded values
5. Existing non-template configs should be unaffected
