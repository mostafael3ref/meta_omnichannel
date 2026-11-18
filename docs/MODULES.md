# Planned modules

Status: **documented only** — DocTypes and business logic are not shipped in the skeleton.

## Meta Omnichannel

Core product module. Holds site-wide settings and shared channel-account metadata that other modules reference.

## Facebook Messenger

Owns Messenger-specific DocTypes, webhook routes, and send/receive adapters. Uses `meta_connectors` for Graph API access and `crm_integration` for CRM writes.

## Instagram

Owns Instagram Messaging DocTypes and adapters. Same connector / CRM dependency rules as Messenger.

## WhatsApp

Owns WhatsApp Cloud API DocTypes and adapters (session messages, templates). Same connector / CRM dependency rules as Messenger.

## Meta Connectors

Shared Meta Platform services: Graph client, token storage DocTypes, webhook signature verification, rate-limit helpers. No CRM DocType writes.

## CRM Integration

Maps Meta identities and conversations onto Frappe CRM v1.91.1 entities (Contacts, Leads, Deals, Activities / Communications). Channel packages call into this module instead of writing CRM documents directly.
