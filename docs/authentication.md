# Authentication & Multi-Factor Authentication

Cabotage supports password-based login, GitHub OAuth, and mandatory multi-factor authentication (MFA) using WebAuthn security keys/passkeys and TOTP authenticator apps.

All configuration is via environment variables with the `CABOTAGE_` prefix.

---

## Login Methods

### Password Login

Enabled by default. Users sign in with username/email and password via Flask-Security's Unified Sign-In.

| Variable | Default | Description |
|---|---|---|
| `CABOTAGE_SECURITY_REGISTERABLE` | `True` | Allow new user registration |
| `CABOTAGE_SECURITY_CONFIRMABLE` | `True` | Require email confirmation after registration |
| `CABOTAGE_SECURITY_RECOVERABLE` | `True` | Allow password reset via email |
| `CABOTAGE_SECURITY_CHANGEABLE` | `True` | Allow authenticated users to change password |
| `CABOTAGE_SECURITY_USERNAME_ENABLE` | `True` | Enable username field on registration |
| `CABOTAGE_SECURITY_USERNAME_MIN_LENGTH` | `2` | Minimum username length |

### GitHub OAuth

Enables "Sign in with GitHub" alongside or instead of password login.

| Variable | Default | Description |
|---|---|---|
| `CABOTAGE_GITHUB_APP_CLIENT_ID` | `None` | GitHub OAuth App client ID. If unset, GitHub login is disabled entirely. |
| `CABOTAGE_GITHUB_APP_CLIENT_SECRET` | `None` | GitHub OAuth App client secret |
| `CABOTAGE_GITHUB_OAUTH_ONLY` | `False` | When `True`, disables password registration (`REGISTERABLE`), password recovery (`RECOVERABLE`), and password change (`CHANGEABLE`). Only GitHub login is available. |
| `CABOTAGE_GITHUB_OAUTH_ALLOWED_ORGS` | `None` | Comma-separated list of GitHub organization slugs. If set, only active members of at least one listed org can log in. |

**Notes:**
- GitHub OAuth users are created with an unusable password (`!`) and cannot change or reset their password.
- If a GitHub OAuth user requests a password reset, they receive an email reminding them to sign in with GitHub instead. The response is identical to a normal reset request to prevent user enumeration.
- GitHub OAuth users with MFA configured must complete a 2FA challenge after OAuth authorization, before being fully logged in. The trust cookie ("Trust this browser for 30 days") is honored for GitHub OAuth logins.
- The OAuth callback URL is `{EXT_PREFERRED_URL_SCHEME}://{EXT_SERVER_NAME}/auth/github/callback`. Register this in your GitHub OAuth App settings.
- OAuth scopes requested: `user:email read:org`.

---

## Multi-Factor Authentication

MFA is mandatory by default. After logging in, users without MFA are redirected to set it up. Users must also generate and verify recovery codes before accessing the application.

| Variable | Default | Description |
|---|---|---|
| `CABOTAGE_REQUIRE_MFA` | `True` | When `True`, all users must configure MFA and recovery codes before accessing the application. When `False`, MFA is available but optional — users can set it up from the security settings page at their own pace. 2FA is still enforced at login for users who have it configured. |

### Enforcement Flow (when `REQUIRE_MFA` is `True`)

1. **No MFA configured** → User is redirected to the setup page (`/tf-setup`) to enroll a security key or authenticator app
2. **MFA configured, no recovery codes** → User is redirected to generate recovery codes (`/mf-recovery-codes`) and must verify one by entering it back (the verified code is consumed)
3. **MFA + recovery codes** → Full access to the application

This enforcement applies to every request via a `before_request` guard. Only MFA setup endpoints, static files, and logout are accessible before setup is complete.

### TOTP (Authenticator App)

| Variable | Default | Description |
|---|---|---|
| `CABOTAGE_SECURITY_TWO_FACTOR` | `True` | Enable TOTP-based two-factor authentication |
| `CABOTAGE_SECURITY_TWO_FACTOR_ENABLED_METHODS` | `["authenticator"]` | Enabled 2FA methods. Only `authenticator` (TOTP) is currently used. |
| `CABOTAGE_SECURITY_TOTP_SECRETS` | `{1: "..."}` | **Must be changed in production.** Dictionary mapping key IDs to TOTP encryption secrets. Generate a secret with `python3 -c "from passlib.totp import generate_secret; print(generate_secret())"`. |
| `CABOTAGE_SECURITY_TOTP_ISSUER` | `"cabotage"` | Issuer name displayed in authenticator apps when scanning the QR code |

### WebAuthn (Security Keys & Passkeys)

| Variable | Default | Description |
|---|---|---|
| `CABOTAGE_SECURITY_WEBAUTHN` | `True` | Enable WebAuthn security key / passkey support |
| `CABOTAGE_SECURITY_WAN_ALLOW_AS_FIRST_FACTOR` | `True` | Allow passkeys for passwordless sign-in (primary authentication) |
| `CABOTAGE_SECURITY_WAN_ALLOW_AS_MULTI_FACTOR` | `True` | Allow security keys as a second factor after password login |

**Notes:**
- Security keys are the recommended primary MFA method and are presented first in the setup flow.
- Users can register multiple keys.
- The last remaining MFA method cannot be removed — users must set up an alternative first. This is enforced server-side (returns 403) in addition to the UI.
- Key types are displayed as "Synced passkey" (multi-device credentials, e.g. iCloud Keychain, Google Password Manager) or "Hardware key" (single-device credentials, e.g. YubiKey).
- WebAuthn origin verification uses `request.host_url` (respecting `ProxyFix`), with a fallback to `EXT_PREFERRED_URL_SCHEME` if the proxy doesn't forward `X-Forwarded-Proto`.

### Recovery Codes

| Variable | Default | Description |
|---|---|---|
| `CABOTAGE_SECURITY_MULTI_FACTOR_RECOVERY_CODES` | `True` | Enable recovery codes |
| `CABOTAGE_SECURITY_MULTI_FACTOR_RECOVERY_CODES_N` | `10` | Number of recovery codes generated per set |

**Notes:**
- Recovery codes are mandatory. Users cannot access the application until they generate codes and verify one.
- During generation, codes are displayed in a grid. Users must copy or download them before proceeding. The "I've Saved These Codes" button is disabled until they use the Copy or Download action.
- After clicking "I've Saved These Codes," the codes are removed from the DOM and the user must enter one back to prove they saved it. This code is consumed server-side.
- Each code can only be used once (for login recovery or for the initial verification).
- The remaining code count (e.g., "9/10") is shown on the security settings page. A warning appears when 2 or fewer codes remain.
- Downloaded recovery code files include the site hostname and username for identification.

### Trust Browser ("Remember This Device")

| Variable | Default | Description |
|---|---|---|
| `CABOTAGE_SECURITY_TWO_FACTOR_ALWAYS_VALIDATE` | `False` | When `True`, always require 2FA on every login. When `False`, enable the "Trust this browser" option. |
| `CABOTAGE_SECURITY_TWO_FACTOR_LOGIN_VALIDITY` | `"30 days"` | Duration a trusted browser cookie remains valid |

When `TWO_FACTOR_ALWAYS_VALIDATE` is `False`, a "Trust this browser for 30 days" checkbox appears below the submit button on all 2FA verification pages:
- TOTP code entry
- WebAuthn security key prompt
- Recovery code entry
- Multi-method selection page (tf_select)

If checked and 2FA succeeds, a signed cookie is set that skips 2FA on subsequent logins for the configured duration.

The trust cookie is:
- Cryptographically signed with the application's `SECRET_KEY`
- Bound to the user's `fs_uniquifier` (cannot be transferred between accounts)
- Automatically invalidated if the user's `fs_uniquifier` changes (e.g., password change)
- Checked during both password login and GitHub OAuth login

---

## Login Tracking

| Variable | Default | Description |
|---|---|---|
| `CABOTAGE_SECURITY_TRACKABLE` | `True` | Track login timestamps and IP addresses |

When enabled, the following fields are updated on each login:
- `current_login_at` / `last_login_at` — login timestamps
- `current_login_ip` / `last_login_ip` — client IP addresses
- `login_count` — total number of logins

This applies to both password logins and GitHub OAuth logins. Correct IP recording requires proper proxy configuration (see below).

---

## Proxy Configuration

| Variable | Default | Description |
|---|---|---|
| `CABOTAGE_PROXY_FIX_NUM_PROXIES` | `1` | Number of trusted reverse proxy hops. Controls Werkzeug's `ProxyFix` for `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`, and `X-Forwarded-Prefix`. Set to `0` to disable. |
| `CABOTAGE_EXT_PREFERRED_URL_SCHEME` | `"http"` | URL scheme for external-facing URLs. **Set to `"https"` in production.** Also used as a fallback for WebAuthn origin verification. |
| `CABOTAGE_EXT_SERVER_NAME` | `"cabotage-app:8000"` | External hostname (without scheme) for URL generation. Used in GitHub OAuth callbacks and email links. |

**Notes:**
- `PROXY_FIX_NUM_PROXIES` should match the number of trusted proxy hops between the client and the application. For a single nginx/traefik ingress, use `1`.
- Without correct proxy configuration, `request.remote_addr` returns the proxy's IP instead of the client's, and `request.scheme` may be `http` instead of `https`.
- WebAuthn verification will fail if the origin scheme doesn't match what the browser sends. Ensure `EXT_PREFERRED_URL_SCHEME` is `"https"` in production.

---

## Session & Security

| Variable | Default | Description |
|---|---|---|
| `CABOTAGE_SECRET_KEY` | **Must change** | Secret key for session cookie signing, CSRF tokens, and trust cookies. Must be a strong random value in production. |
| `CABOTAGE_SECURITY_PASSWORD_SALT` | **Must change** | Salt for password-related token generation (reset links, confirmation links). Must be a strong random value in production. |

### Freshness

Flask-Security's freshness model requires recent authentication for sensitive operations like registering new security keys, changing TOTP settings, and managing recovery codes. If a user's session is older than the freshness window, they are prompted to re-authenticate before proceeding.

| Variable | Default (Flask-Security) | Description |
|---|---|---|
| `CABOTAGE_SECURITY_FRESHNESS` | `timedelta(days=1)` | Maximum age of authentication for sensitive operations |
| `CABOTAGE_SECURITY_FRESHNESS_GRACE_PERIOD` | `timedelta(hours=1)` | After a freshness check passes, skip further checks for this duration |

These are Flask-Security defaults and are not explicitly set in Cabotage's config. Override them via environment variables if needed.

---

## Email

Password reset, email confirmation, and GitHub-user reminder emails require a configured mail server.

| Variable | Default | Description |
|---|---|---|
| `CABOTAGE_MAIL_SERVER` | `"app.debugmail.io"` | SMTP server hostname |
| `CABOTAGE_MAIL_PORT` | `25` | SMTP port |
| `CABOTAGE_MAIL_USE_TLS` | `False` | Use STARTTLS |
| `CABOTAGE_MAIL_USE_SSL` | `False` | Use implicit TLS |
| `CABOTAGE_MAIL_USERNAME` | — | SMTP username |
| `CABOTAGE_MAIL_PASSWORD` | — | SMTP password |
| `CABOTAGE_MAIL_DEFAULT_SENDER` | `"noreply@localhost"` | Default From address |
| `CABOTAGE_SECURITY_EMAIL_SENDER` | `"noreply@localhost"` | From address for security-related emails (password reset, confirmation, etc.) |

**Note:** `CABOTAGE_TESTING=True` suppresses all email sending via Flask-Mail. This is set by default in the docker-compose development environment.

---

## Security Settings Page

Authenticated users manage their MFA configuration at `/account/security`, accessible from the user dropdown menu as "Two-Factor Auth".

The page provides:
- **Security Keys** — Register new keys (via modal), view registered keys with type and last-used time, remove keys (with confirmation modal)
- **Authenticator App** — Set up via modal (QR code + verification), or disable (with confirmation)
- **Recovery Codes** — View remaining count out of total (e.g., "9/10"), regenerate codes, low-code warning

After initial MFA setup, users are redirected to the home page. When managing MFA from settings, they stay on the settings page.
