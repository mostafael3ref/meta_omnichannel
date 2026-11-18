"""Frappe CRM integration module.

Target CRM: Frappe CRM v1.91.1 on Frappe Framework v15.

Planned responsibilities (not implemented yet):
- Resolve Meta channel identities to CRM Contact / Lead / Deal records
- Create or update CRM Activities / Communications from channel messages
- Expose mapping DocTypes and sync status for operators
- Register doc_events / overrides in hooks.py when sync rules are defined

Do not mutate CRM documents until mapping rules and idempotency are designed.
"""
