# Legal Pages

Every Cabotage deployment has a different operator, so the policies it links to
(terms, privacy, acceptable use) are configured per deployment. There are two
ways to do it; set one or the other, not both.

With neither configured, Cabotage serves no legal pages and renders no legal
links in the footer or on the About page.

## Link to policies you host elsewhere

Most operators already publish policies somewhere else. Point Cabotage at them:

| Variable | Description |
|----------|-------------|
| `CABOTAGE_LEGAL_TERMS_URL` | Terms of use |
| `CABOTAGE_LEGAL_PRIVACY_URL` | Privacy notice |
| `CABOTAGE_LEGAL_ACCEPTABLE_USE_URL` | Acceptable use policy |
| `CABOTAGE_LEGAL_URL` | Optional index page, linked as "Legal" in the footer |

Each is optional; only the ones you set are linked. Values must be absolute
`http(s)` URLs. They open in a new tab, and `/legal/` is not served.

For example, with the Terraform module:

```hcl
cabotage_app_extra_config = {
  CABOTAGE_LEGAL_URL                = "https://policies.example.org/hosting/"
  CABOTAGE_LEGAL_TERMS_URL          = "https://policies.example.org/hosting/terms/"
  CABOTAGE_LEGAL_PRIVACY_URL        = "https://policies.example.org/hosting/privacy/"
  CABOTAGE_LEGAL_ACCEPTABLE_USE_URL = "https://policies.example.org/hosting/acceptable-use/"
}
```

## Serve bundled pages

| Variable | Description |
|----------|-------------|
| `CABOTAGE_LEGAL_PROFILE` | Profile whose pages are served under `/legal/` |

The only bundled profile is `cabotage`, the hosted service run by Cabotage
Deploys, LLC. Don't use it for your own deployment: it describes that
company's service, contacts, and obligations.

A profile is a directory of templates at
`cabotage/client/templates/main/legal/<profile>/`. The name must be lowercase
letters, digits, `-`, or `_`. It needs:

| Template | Purpose |
|----------|---------|
| `_nav.html` | Sets `legal_nav`: the policy pages in reading order, each with `endpoint`, `label`, `icon`, and `summary` |
| `index.html` | `/legal/`; extends `main/legal/_landing.html` |
| `terms.html` | `/legal/terms/`; extends `main/legal/_document.html` |
| `privacy.html` | `/legal/privacy/`; extends `main/legal/_document.html` |
| `acceptable_use.html` | `/legal/acceptable-use/`; extends `main/legal/_document.html` |

## Startup checks

Cabotage refuses to start if both a profile and URLs are set, if a URL isn't an
absolute `http(s)` URL, or if the profile name is invalid or any of its
templates is missing.
