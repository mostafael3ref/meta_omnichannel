const STRINGS = {
  en: {
    inbox: "Inbox",
    all: "All",
    facebook: "Facebook",
    instagram: "Instagram",
    whatsapp: "WhatsApp",
    unread: "Unread",
    assigned_to_me: "Assigned to me",
    unassigned: "Unassigned",
    search: "Search conversations",
    sync: "Sync Facebook",
    send: "Send",
    composer_placeholder: "Write a message…",
    empty_list: "No conversations yet",
    empty_chat: "Select a conversation",
    customer: "Customer",
    assign_me: "Assign to me",
    create_lead: "Create Lead",
    status: "Status",
    notes: "Internal notes",
    add_note: "Add note",
    loading: "Loading…",
    pending_channel: "Channel pending connector API",
    open_crm_lead: "Open Lead",
    open_contact: "Open Contact",
  },
  ar: {
    inbox: "الوارد",
    all: "الكل",
    facebook: "فيسبوك",
    instagram: "إنستغرام",
    whatsapp: "واتساب",
    unread: "غير مقروء",
    assigned_to_me: "مسند إليّ",
    unassigned: "غير مسند",
    search: "بحث في المحادثات",
    sync: "مزامنة فيسبوك",
    send: "إرسال",
    composer_placeholder: "اكتب رسالة…",
    empty_list: "لا توجد محادثات",
    empty_chat: "اختر محادثة",
    customer: "العميل",
    assign_me: "أسند إليّ",
    create_lead: "إنشاء عميل محتمل",
    status: "الحالة",
    notes: "ملاحظات داخلية",
    add_note: "إضافة ملاحظة",
    loading: "جارٍ التحميل…",
    pending_channel: "القناة بانتظار واجهة الموصل",
    open_crm_lead: "فتح العميل المحتمل",
    open_contact: "فتح جهة الاتصال",
  },
};

export function t(key) {
  const lang = (window.omnichannelBoot?.lang || "en").toLowerCase();
  const table = lang.startsWith("ar") ? STRINGS.ar : STRINGS.en;
  return table[key] || STRINGS.en[key] || key;
}

export function isRtl() {
  const lang = (window.omnichannelBoot?.lang || "en").toLowerCase();
  return lang.startsWith("ar");
}
