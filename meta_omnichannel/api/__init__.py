"""Whitelisted APIs.

- meta_omnichannel.api.connector.test_connector_connection
- meta_omnichannel.api.inbox.list_conversations / list_messages / mark_conversation_read / send_message
- meta_omnichannel.api.inbox.sync_facebook_inbox (list + bounded messages)
- meta_omnichannel.api.inbox.sync_instagram_inbox
- meta_omnichannel.api.inbox.sync_inbox (Facebook coordinator + Instagram list)
- meta_omnichannel.facebook_messenger.api.notify_facebook_inbound (guest HMAC)
- meta_omnichannel.facebook_messenger.api.rotate_inbound_notification_secret
"""
