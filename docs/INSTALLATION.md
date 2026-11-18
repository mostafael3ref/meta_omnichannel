# Installation (Linux Frappe Bench)

## Prerequisites

- Frappe Framework v15
- Frappe CRM v1.91.1 (`crm` app installed on the site)
- Python 3.10+
- This repository available on the Bench host

Do **not** run these commands on the Windows development checkout.

## Install

```bash
cd /path/to/frappe-bench
# Option A
bench get-app /path/or/git/url/to/meta_omnichannel
# Option B: copy tree into apps/meta_omnichannel then:
bench setup requirements
bench --site <site> install-app meta_omnichannel
bench --site <site> migrate
```

## Frontend assets

A no-build inbox bundle ships under `meta_omnichannel/public/frontend/`.

Optional Vue production build (when Node/npm available):

```bash
cd apps/meta_omnichannel/frontend
npm ci
npm run build
bench build --app meta_omnichannel
bench restart
```

```bash
bench build --app meta_omnichannel
bench restart
```

These commands were **not** executed in the Windows development environment.

## Post-install

1. Assign roles: **Omnichannel Administrator** and/or **Omnichannel Agent**
2. Open **Omnichannel Settings** → set Base URL, Tenant ID, API Key
3. **Test Connection**
4. Open `/omnichannel` inbox
5. **Sync Facebook**

## Uninstall

```bash
bench --site <site> uninstall-app meta_omnichannel
```

Review data retention for Omnichannel DocTypes before uninstalling.
