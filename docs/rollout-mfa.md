# Rollout Guide: Multi-Factor Authentication

This document covers deploying MFA support, including pre-deployment requirements, configuration, migration steps, rollback procedures, and user communication.

---

## What's Changing

- WebAuthn (security keys/passkeys) and TOTP (authenticator apps) as MFA methods
- Recovery codes with mandatory generation and verification
- GitHub OAuth 2FA enforcement for users with MFA configured
- "Trust this browser" cookies (configurable validity, default 30 days)
- Login IP and timestamp tracking via ProxyFix
- Unified security settings page at `/account/security`
- Password reset interception for GitHub OAuth users

---

## Pre-Deployment Checklist

### 1. Verify Existing Secrets

These settings are not new but **must not be dev defaults** in production. Verify they are set to strong random values:

| Variable | Verify |
|---|---|
| `CABOTAGE_SECRET_KEY` | Must be a strong random value. Changing it invalidates all sessions and trust cookies. |
| `CABOTAGE_SECURITY_PASSWORD_SALT` | Must be a strong random value. Used for reset/confirmation tokens. |

### 2. New Configuration (Required)

| Variable | Action | Why |
|---|---|---|
| `CABOTAGE_EXT_PREFERRED_URL_SCHEME` | Set to `https` | WebAuthn origin verification requires the correct scheme. If `http` in production, passkey registration will fail with *"Unexpected client data origin"*. |
| `CABOTAGE_EXT_SERVER_NAME` | Set to production hostname | Used for GitHub OAuth callback URL and email links. No scheme, no trailing slash (e.g., `cabotage.example.com`). |
| `CABOTAGE_PROXY_FIX_NUM_PROXIES` | Set to match proxy chain | Usually `1`. Required for correct IP tracking and WebAuthn origin. |

**TOTP encryption key** — `CABOTAGE_SECURITY_TOTP_SECRETS` must be configured in the application config (not as an env var, since it's a Python dict). The default is a dev placeholder. Generate a secret with:
```bash
python3 -c "from passlib.totp import generate_secret; print(generate_secret())"
```
Set in config as: `SECURITY_TOTP_SECRETS = {1: "your-generated-secret"}`

### 3. New Configuration (Optional)

| Variable | Default | Description |
|---|---|---|
| `CABOTAGE_REQUIRE_MFA` | `True` | Set to `False` to make MFA optional (opt-in). See [Gradual Rollout](#gradual-rollout-strategy). |
| `CABOTAGE_SECURITY_TWO_FACTOR_ALWAYS_VALIDATE` | `False` | Set to `True` to require 2FA on every login (no trust cookies). |
| `CABOTAGE_SECURITY_TWO_FACTOR_LOGIN_VALIDITY` | `30 days` | Trust cookie duration. |
| `CABOTAGE_SECURITY_MULTI_FACTOR_RECOVERY_CODES_N` | `10` | Recovery codes per set. |
| `CABOTAGE_SECURITY_TOTP_ISSUER` | `cabotage` | Name shown in authenticator apps. Set to your organization name. |

### 4. Dependencies

New packages: `argon2-cffi`, `webauthn`. `flask-security-too` bumped to `>=5.5.0`.

### 5. Verify Proxy Headers

Before deploying, confirm your reverse proxy sends:
- `X-Forwarded-Proto` (required for WebAuthn origin)
- `X-Forwarded-For` (required for IP tracking)
- `X-Forwarded-Host` (required for correct URL generation)

---

## Deployment Steps

**Order matters. Run migrations before the new code serves traffic.**

### Step 1: Run database migrations

```bash
flask db upgrade
```

This creates the `webauthn` table. The migration is additive — no existing tables or columns are modified. Safe to run while the old code is still serving.

### Step 2: Deploy the new code

Deploy with the configuration above. On first request after deployment:
- If `REQUIRE_MFA=True`: existing users without MFA will be redirected to set it up
- If `REQUIRE_MFA=False`: no user-facing changes until users opt in via settings

### Step 3: Verify

1. **Login page** — shows "Sign in with GitHub" (if configured) and password form
2. **After login** — redirected to MFA setup (if `REQUIRE_MFA=True` and no MFA configured)
3. **Security key registration** — browser prompt triggers, key is saved
4. **TOTP setup** — QR code appears in modal, code verification works
5. **Recovery codes** — generates correct number, copy/download works, verification burns a code
6. **Subsequent login** — 2FA challenge appears
7. **Trust cookie** — "Trust this browser" checkbox skips 2FA on next login
8. **GitHub OAuth** — requires 2FA after OAuth (for users with MFA configured)
9. **GitHub OAuth (no MFA)** — logs in directly, then redirected to MFA setup

---

## Gradual Rollout Strategy

### Phase 1: Deploy with opt-in MFA
```
CABOTAGE_REQUIRE_MFA=False
```
- MFA features are available but optional
- Users can set up MFA from the "Two-Factor Auth" menu
- Users who configure MFA will be prompted for 2FA at login
- Users without MFA can use the app normally
- **Recommended:** deploy this first and let early adopters test

### Phase 2: Communicate to users

Before making MFA mandatory:
- Notify all users that MFA will be required on a specific date
- Link to the security settings page (`/account/security`)
- Recommend setting up a security key or passkey as the primary method
- Remind users to save their recovery codes securely

### Phase 3: Enable mandatory MFA
```
CABOTAGE_REQUIRE_MFA=True
```
- All users must configure at least one MFA method and generate recovery codes
- Users without MFA are blocked from all app functionality until setup is complete

### Phase 4: Strict mode (optional)
```
CABOTAGE_SECURITY_TWO_FACTOR_ALWAYS_VALIDATE=True
```
- Disables "Trust this browser" — 2FA required on every login
- Maximum security

---

## Rollback

### Disable mandatory MFA (no downtime)

```
CABOTAGE_REQUIRE_MFA=False
```

Users can log in without MFA. Existing MFA configurations remain intact and are still enforced at login for users who have it. No data loss.

### WebAuthn not working

If users see *"Unexpected client data origin"*:
1. Verify `CABOTAGE_EXT_PREFERRED_URL_SCHEME` is `https`
2. Verify `CABOTAGE_PROXY_FIX_NUM_PROXIES` matches your proxy setup
3. Verify the proxy sends `X-Forwarded-Proto: https`
4. Users with TOTP configured can use their authenticator app instead

### Full code rollback

1. Deploy the previous code version
2. **Do not** run `flask db downgrade` — the `webauthn` table is harmless if unused
3. If you must downgrade the database: identify the migration revision before the webauthn table was added and downgrade to it

**After rollback, existing user data is preserved:**
- `tf_primary_method` and `tf_totp_secret` remain in the `users` table but the old code ignores them
- `webauthn` table credentials are orphaned but harmless
- `mf_recovery_codes` remain but are unused
- No data loss or corruption occurs

---

## Monitoring

### What to watch

- **Login success rate** — a sudden drop may indicate MFA setup or verification issues
- **Error logs** — search for:
  - `"Unexpected client data origin"` — WebAuthn origin mismatch (proxy config issue)
  - `"webauthn is required for WEBAUTHN"` — missing `webauthn` Python package
  - `"Failed to send GitHub user password reset email"` — mail delivery issue
- **Support requests** — users locked out (lost authenticator + recovery codes)
- **Recovery code usage** — high usage may indicate users are having trouble with their primary method

### Helping locked-out users

If a user loses access to all MFA methods and recovery codes, an administrator can reset their MFA via the Flask-Admin panel (`/admin/`, requires admin privileges):

1. Find the user in the Users admin view
2. Set `tf_primary_method` to empty/None (disables TOTP)
3. Set `tf_totp_secret` to empty/None
4. Set `mf_recovery_codes` to empty/None
5. Delete the user's entries from the `webauthn` table (WebAuthn admin view)
6. The user will be prompted to set up MFA again on their next login

---

## Database Changes

### New table: `webauthn`

| Column | Type | Description |
|---|---|---|
| `id` | UUID (PK) | Primary key |
| `user_id` | UUID (FK → users) | Owner |
| `credential_id` | LargeBinary(1024) | WebAuthn credential ID (unique index) |
| `public_key` | LargeBinary | Credential public key |
| `sign_count` | Integer | Signature counter (replay protection) |
| `transports` | AsaList | Transport types (usb, ble, nfc, internal) |
| `backup_state` | Boolean | Whether credential is backed up |
| `device_type` | String(64) | `single_device` or `multi_device` |
| `extensions` | String(255) | WebAuthn extensions |
| `create_datetime` | DateTime | When credential was registered |
| `lastuse_datetime` | DateTime | Last authentication time |
| `name` | String(64) | User-provided nickname |
| `usage` | String(64) | `first` (passwordless) or `secondary` (2FA) |

### Existing columns now used

The following columns already exist on the `users` table (from a prior Flask-Security migration) and are now actively used:

| Column | Type | Usage |
|---|---|---|
| `tf_primary_method` | String(64) | Set to `"authenticator"` when TOTP is enabled |
| `tf_totp_secret` | String(255) | Encrypted TOTP secret |
| `fs_webauthn_user_handle` | String(64) | WebAuthn user handle (set on first key registration) |
| `mf_recovery_codes` | AsaList | List of recovery codes (entries removed as used) |
