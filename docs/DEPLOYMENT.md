# Deployment

## Staging

1. Deploy app source to Bench `apps/meta_omnichannel`
2. `bench --site <site> install-app meta_omnichannel` (first time) or `migrate`
3. `bench build --app meta_omnichannel && bench restart`
4. Configure Omnichannel Settings with staging tenant API key
5. Restrict egress to connector host/IPs
6. Run manual checklist in `docs/TESTING.md`

## Production

Do **not** promote until:

- Live Facebook sync/send verified
- Role matrix reviewed
- Egress controls in place
- Backup/restore of Omnichannel DocTypes tested
- Instagram/WhatsApp/OAuth either disabled (current) or fully contracted

## Monitoring

- Omnichannel Sync Log DocType
- Frappe error log / worker failures for `meta_omnichannel.jobs.*`
- Connector rate-limit (429) frequency
