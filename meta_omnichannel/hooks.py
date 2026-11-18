app_name = "meta_omnichannel"
app_title = "Meta Omnichannel CRM"
app_publisher = "Meta Omnichannel"
app_description = (
	"Meta Omnichannel CRM — Facebook Messenger, Instagram, and WhatsApp "
	"integration for Frappe CRM"
)
app_email = "dev@meta-omnichannel.local"
app_license = "mit"
# Version source of truth: meta_omnichannel/__init__.py (__version__)

required_apps = ["crm"]

app_include_css = "/assets/meta_omnichannel/css/omnichannel_desk.css"
app_include_js = "/assets/meta_omnichannel/js/omnichannel_desk.js"

after_install = "meta_omnichannel.install.after_install"
after_migrate = "meta_omnichannel.install.after_migrate"
after_uninstall = "meta_omnichannel.install.after_uninstall"

add_to_apps_screen = [
	{
		"name": "meta_omnichannel",
		"logo": "/assets/meta_omnichannel/images/omnichannel.svg",
		"title": "Omnichannel",
		"route": "/omnichannel",
		"has_permission": "meta_omnichannel.permissions.user_is_omnichannel_agent",
	}
]

website_route_rules = [
	{"from_route": "/omnichannel/<path:app_path>", "to_route": "omnichannel"},
	{"from_route": "/omnichannel", "to_route": "omnichannel"},
]

scheduler_events = {
	"hourly_long": [
		"meta_omnichannel.jobs.sync_jobs.scheduled_facebook_sync",
	],
}

permission_query_conditions = {
	"Omnichannel Conversation": "meta_omnichannel.permissions.conversation_query",
	"Omnichannel Message": "meta_omnichannel.permissions.message_query",
}

has_permission = {
	"Omnichannel Conversation": "meta_omnichannel.permissions.has_conversation_permission",
}
