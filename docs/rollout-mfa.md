# Rollout Guide: Multi-Factor Authentication

This document covers deploying the MFA feature, including pre-deployment requirements, configuration, migration steps, and rollback procedures.

---

## Overview

This PR adds:
- Mandatory MFA via WebAuthn (security keys/passkeys) and TOTP (authenticator apps)
- Mandatory recovery codes with verification
- GitHub OAuth 2FA enforcement
- "Trust this browser" cookies (30-day validity)
- Login IP tracking via ProxyFix
- Unified security settings page at `/account/security`

## Pre-Deployment Checklist

### 1. Configuration (Required)

These environment variables **must** be set before deploying:

| Variable | Action | Why |
|---|---|---|
| `CABOTAGE_SECRET_KEY` | Set to a strong random value | Signs session cookies, CSRF tokens, and trust cookies. Changing this after deployment invalidates all sessions. |
| `CABOTAGE_SECURITY_PASSWORD_SALT` | Set to a strong random value | Used for password reset and confirmation tokens. |
| `CABOTAGE_SECURITY_TOTP_SECRETS` | Set to `{1: "<generated>"}` | Encrypts TOTP secrets at rest. Generate with: `python3 -c "from passlib.totp import generate_secret; print(generate_secret())"` |
| `CABOTAGE_EXT_PREFERRED_URL_SCHEME` | Set to `https` | Required for WebAuthn origin verification. If this is `http` in production, passkey registration/authentication will fail. |
| `CABOTAGE_EXT_SERVER_NAME` | Set to your production hostname | Used for GitHub OAuth callback URL and email links. No scheme, no trailing slash (e.g., `cabotage.example.com`). |
| `CABOTAGE_PROXY_FIX_NUM_PROXIES` | Set to match your proxy chain | Usually `1` for a single nginx/traefik ingress. Required for correct IP tracking and WebAuthn origin. |

### 2. Configuration (Optional)

| Variable | Default | Consider Changing If |
|---|---|---|
| `CABOTAGE_REQUIRE_MFA` | `True` | Set to `False` if you want to roll out MFA gradually (opt-in instead of mandatory). Users can still set up MFA from the settings page. |
| `CABOTAGE_SECURITY_TWO_FACTOR_ALWAYS_VALIDATE` | `False` | Set to `True` to disable "Trust this browser" entirely — 2FA on every login. |
| `CABOTAGE_SECURITY_TWO_FACTOR_LOGIN_VALIDITY` | `30 days` | Reduce for higher security (e.g., `7 days`, `1 day`). |
| `CABOTAGE_SECURITY_MULTI_FACTOR_RECOVERY_CODES_N` | `10` | Increase if users are likely to use recovery codes frequently. |
| `CABOTAGE_SECURITY_TOTP_ISSUER` | `cabotage` | Set to your organization name — this appears in authenticator apps. |

### 3. Dependencies

New Python packages added:
- `argon2-cffi` — password hashing (Flask-Security 5.7.1 default)
- `webauthn` — WebAuthn/passkey support
- `flask-security-too` bumped from `>=5.3.3` to `>=5.5.0`
- `pytest` added to dev dependencies

Run `uv sync --frozen` to install.

### 4. Verify Proxy Configuration

WebAuthn is sensitive to the origin URL. Before deploying:

1. Confirm your reverse proxy sends `X-Forwarded-Proto` and `X-Forwarded-For` headers
2. Confirm `CABOTAGE_PROXY_FIX_NUM_PROXIES` matches the number of proxy hops
3. Confirm `CABOTAGE_EXT_PREFERRED_URL_SCHEME` is `https`

If origin verification fails, users will see: *"Could not verify passkey: Unexpected client data origin"*

---

## Deployment Steps

### Step 1: Deploy the code

Deploy the new code with the configuration above. The application will start but existing users without MFA will be redirected to set it up on their next request (if `REQUIRE_MFA=True`).

### Step 2: Run database migrations

```bash
flask db upgrade
```

This creates the `webauthn` table for storing security key credentials. The migration is additive — no existing tables are modified.

### Step 3: Verify

1. **Login page** — should show "Sign in with GitHub" button (if GitHub OAuth configured) and password form
2. **After login** — user should be redirected to MFA setup page
3. **Security key registration** — should trigger browser prompt
4. **TOTP setup** — should show QR code in modal
5. **Recovery codes** — should generate 10 codes, require copy/download + verification
6. **Subsequent login** — should prompt for 2FA
7. **Trust cookie** — checking "Trust this browser for 30 days" should skip 2FA on next login
8. **GitHub OAuth** — should require 2FA after OAuth if MFA is configured

---

## Gradual Rollout Strategy

If you want to introduce MFA without forcing all users at once:

### Phase 1: Opt-in MFA
```
CABOTAGE_REQUIRE_MFA=False
```
- MFA features are available but optional
- Users can set up MFA from the "Two-Factor Auth" menu
- Users with MFA configured will be prompted for 2FA at login
- Users without MFA can use the app normally

### Phase 2: Mandatory MFA
```
CABOTAGE_REQUIRE_MFA=True
```
- All users must configure MFA and recovery codes
- Users without MFA are redirected to setup on every request
- Consider sending an advance notice email before enabling

### Phase 3: Strict mode (optional)
```
CABOTAGE_REQUIRE_MFA=True
CABOTAGE_SECURITY_TWO_FACTOR_ALWAYS_VALIDATE=True
```
- 2FA required on every login, no "Trust this browser" option
- Maximum security, maximum friction

---

## Rollback

### If MFA is causing login issues

**Quick fix — disable mandatory MFA:**
```
CABOTAGE_REQUIRE_MFA=False
```
Users can still log in without MFA. Existing MFA configurations remain but aren't enforced.

### If WebAuthn is broken (origin mismatch, etc.)

1. Check `CABOTAGE_EXT_PREFERRED_URL_SCHEME` is `https`
2. Check `CABOTAGE_PROXY_FIX_NUM_PROXIES` matches your proxy setup
3. If unfixable, users can fall back to TOTP (authenticator app)

### Full rollback

If you need to fully revert:

1. Deploy the previous code version
2. **Do NOT** run `flask db downgrade` unless necessary — the `webauthn` table is harmless if unused
3. If you must downgrade the database: `flask db downgrade feab2675055d` (removes the webauthn table)

**Note:** Rolling back after users have configured MFA means:
- Users with TOTP configured will still have `tf_primary_method` set in the database, but the old code won't enforce 2FA
- WebAuthn credentials in the `webauthn` table will be orphaned but harmless
- Recovery codes in `mf_recovery_codes` will be orphaned but harmless
- No data loss occurs

---

## Monitoring

Things to watch after deployment:

- **Login success rate** — a drop may indicate MFA setup issues
- **Error logs** — look for `"Unexpected client data origin"` (WebAuthn origin mismatch) or `"webauthn is required for WEBAUTHN"` (missing dependency)
- **Support requests** — users locked out of their accounts (need admin to reset MFA)
- **Recovery code usage** — if many users are using recovery codes to log in, they may be having issues with their primary MFA method

### Admin actions

Currently, MFA state can be managed via the Flask-Admin panel at `/admin/`:
- Reset a user's `tf_primary_method` to `None` and `tf_totp_secret` to `None` to disable TOTP
- Delete entries from the `webauthn` table to remove security keys
- Set `mf_recovery_codes` to `None` to force recovery code regeneration

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
| `create_datetime` | DateTime | When registered |
| `lastuse_datetime` | DateTime | Last authentication time |
| `name` | String(64) | User-provided nickname |
| `usage` | String(64) | `first` or `secondary` |

### Modified columns (from prior Flask-Security migration)

The following columns already exist on the `users` table (added in a previous migration) and are now used:
- `tf_primary_method` — `"authenticator"` when TOTP is configured
- `tf_totp_secret` — encrypted TOTP secret
- `fs_webauthn_user_handle` — WebAuthn user handle
- `mf_recovery_codes` — comma-separated recovery codes
