/**
 * Omnichannel Inbox runtime bundle (no-build fallback).
 * Preferred path on Linux Bench: build Vue sources from /frontend via `npm run build`.
 */
(function () {
  const boot = window.__OMNICHANNEL_BOOT__ || {};
  const rtl = String(boot.lang || "en").toLowerCase().startsWith("ar");
  const i18n = {
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
  const t = (k) => (rtl ? i18n.ar : i18n.en)[k] || i18n.en[k] || k;

  function csrf() {
    return boot.csrf_token || window.csrf_token || "";
  }

  async function call(method, args) {
    const body = new URLSearchParams();
    Object.entries(args || {}).forEach(([key, value]) => {
      if (value === undefined || value === null) return;
      body.append(key, typeof value === "object" ? JSON.stringify(value) : String(value));
    });
    const response = await fetch(`/api/method/${method}`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        Accept: "application/json",
        "X-Frappe-CSRF-Token": csrf(),
        "X-Requested-With": "XMLHttpRequest",
      },
      body,
    });
    const data = await response.json();
    if (data.exc || data._server_messages) {
      let message = data.exc || "Request failed";
      try {
        const msgs = JSON.parse(data._server_messages || "[]");
        if (msgs.length) message = JSON.parse(msgs[0]).message || message;
      } catch (_) {}
      throw new Error(message);
    }
    return data.message;
  }

  function nonce() {
    return crypto.randomUUID ? crypto.randomUUID() : `n-${Date.now()}`;
  }

  function fmt(v) {
    if (!v) return "";
    try {
      return new Date(v).toLocaleString();
    } catch (_) {
      return v;
    }
  }

  const state = {
    filter: "all",
    search: "",
    conversations: [],
    selected: null,
    messages: [],
    panel: null,
    draft: "",
    note: "",
    error: "",
    loadingList: false,
    loadingChat: false,
  };

  const root = document.getElementById("omnichannel-app");
  if (!root) return;

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "className") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
      else if (v !== false && v != null) node.setAttribute(k, v === true ? "" : v);
    });
    (children || []).forEach((c) => c && node.appendChild(c));
    return node;
  }

  async function loadList() {
    state.loadingList = true;
    state.error = "";
    render();
    try {
      const args = { limit: 40, offset: 0 };
      if (state.search) args.search = state.search;
      if (["Facebook", "Instagram", "WhatsApp"].includes(state.filter)) args.provider = state.filter;
      if (state.filter === "me") args.assigned = "me";
      if (state.filter === "unassigned") args.assigned = "unassigned";
      const data = await call("meta_omnichannel.api.inbox.list_conversations", args);
      let items = data.items || [];
      if (state.filter === "unread") items = items.filter((c) => (c.unread_count || 0) > 0);
      state.conversations = items;
    } catch (e) {
      state.error = e.message || String(e);
    } finally {
      state.loadingList = false;
      render();
    }
  }

  async function openConversation(row) {
    state.selected = row;
    state.loadingChat = true;
    state.error = "";
    render();
    try {
      const [msgData, customer] = await Promise.all([
        call("meta_omnichannel.api.inbox.list_messages", { conversation: row.name, limit: 100, offset: 0 }),
        call("meta_omnichannel.crm_integration.service.get_customer_panel", { conversation: row.name }),
      ]);
      state.messages = msgData.items || [];
      state.panel = customer;
      row.unread_count = 0;
    } catch (e) {
      state.error = e.message || String(e);
    } finally {
      state.loadingChat = false;
      render();
    }
  }

  async function sendMessage() {
    if (!state.selected || !state.draft.trim()) return;
    try {
      const result = await call("meta_omnichannel.api.inbox.send_message", {
        conversation: state.selected.name,
        text: state.draft,
        client_nonce: nonce(),
      });
      if (!result.ok) state.error = result.error || "Send failed";
      state.draft = "";
      await openConversation(state.selected);
      await loadList();
    } catch (e) {
      state.error = e.message || String(e);
      render();
    }
  }

  function render() {
    root.innerHTML = "";
    const shell = el("div", { className: "oc-shell", dir: rtl ? "rtl" : "ltr" });

    const side = el("aside", { className: "oc-side" }, [
      el("h1", { className: "oc-brand", text: t("inbox") }),
      el(
        "nav",
        { className: "oc-nav" },
        [
          ["all", t("all")],
          ["Facebook", t("facebook")],
          ["Instagram", t("instagram")],
          ["WhatsApp", t("whatsapp")],
          ["unread", t("unread")],
          ["me", t("assigned_to_me")],
          ["unassigned", t("unassigned")],
        ].map(([id, label]) =>
          el("button", {
            type: "button",
            className: state.filter === id ? "active" : "",
            text: label,
            onclick: () => {
              state.filter = id;
              loadList();
            },
          })
        )
      ),
    ]);

    const list = el("section", { className: "oc-list" });
    const toolbar = el("div", { className: "oc-list-toolbar" });
    const search = el("input", {
      type: "search",
      placeholder: t("search"),
      value: state.search,
      oninput: (e) => {
        state.search = e.target.value;
      },
      onkeydown: (e) => {
        if (e.key === "Enter") loadList();
      },
    });
    toolbar.appendChild(search);
    toolbar.appendChild(
      el("button", {
        className: "oc-btn secondary",
        type: "button",
        text: t("sync"),
        onclick: async () => {
          try {
            const result = await call("meta_omnichannel.api.inbox.sync_inbox", { limit: 50 });
            await loadList();
            if (result && result.ok === false) {
              state.error = result.error || "Sync failed";
              render();
            }
          } catch (e) {
            state.error = e.message || String(e);
            render();
          }
        },
      })
    );
    list.appendChild(toolbar);
    if (state.loadingList) list.appendChild(el("div", { className: "oc-empty", text: t("loading") }));
    else if (!state.conversations.length) list.appendChild(el("div", { className: "oc-empty", text: t("empty_list") }));
    else {
      state.conversations.forEach((c) => {
        list.appendChild(
          el(
            "button",
            {
              type: "button",
              className: "oc-conv" + (state.selected && state.selected.name === c.name ? " active" : ""),
              onclick: () => openConversation(c),
            },
            [
              el("div", { className: "oc-conv-top" }, [
                el("span", { text: c.participant_name || c.participant_id }),
                c.unread_count ? el("span", { className: "oc-badge", text: String(c.unread_count) }) : null,
              ]),
              el("div", { className: "channel-pill", text: `${c.provider} · ${fmt(c.last_message_at)}` }),
              el("div", { className: "oc-preview", text: c.last_message_preview || "" }),
            ]
          )
        );
      });
    }

    const chat = el("section", { className: "oc-chat" });
    chat.appendChild(
      el("header", { className: "oc-chat-header" }, [
        el("div", null, [
          el("strong", { text: (state.selected && state.selected.participant_name) || t("empty_chat") }),
          state.selected
            ? el("div", { className: "channel-pill", text: `${state.selected.provider} · ${state.selected.status}` })
            : null,
        ]),
      ])
    );
    if (state.error) chat.appendChild(el("div", { className: "oc-error", text: state.error }));
    if (!state.selected) chat.appendChild(el("div", { className: "oc-empty", text: t("empty_chat") }));
    else if (state.loadingChat) chat.appendChild(el("div", { className: "oc-empty", text: t("loading") }));
    else {
      const msgs = el("div", { className: "oc-messages" });
      state.messages.forEach((m) => {
        msgs.appendChild(
          el("div", { className: "oc-bubble " + (m.direction === "Outbound" ? "outbound" : "inbound") }, [
            el("div", { text: m.text_content || m.message_type }),
            el("div", {
              className: "oc-meta",
              text: `${fmt(m.sent_at)}${m.direction === "Outbound" ? " · " + m.delivery_status : ""}`,
            }),
          ])
        );
      });
      chat.appendChild(msgs);
    }
    const composer = el("div", { className: "oc-composer" });
    const ta = el("textarea", {
      rows: "3",
      placeholder: t("composer_placeholder"),
      value: state.draft,
      disabled: !state.selected || (state.selected && state.selected.provider !== "Facebook"),
      oninput: (e) => {
        state.draft = e.target.value;
      },
    });
    composer.appendChild(ta);
    composer.appendChild(
      el("button", {
        className: "oc-btn",
        type: "button",
        text: t("send"),
        disabled: !state.selected || !state.selected || state.selected.provider !== "Facebook",
        onclick: sendMessage,
      })
    );
    chat.appendChild(composer);
    if (state.selected && state.selected.provider !== "Facebook") {
      chat.appendChild(el("div", { className: "oc-empty", text: t("pending_channel") }));
    }

    const customer = el("aside", { className: "oc-customer" }, [el("h3", { text: t("customer") })]);
    if (!state.selected) customer.appendChild(el("div", { className: "oc-empty", text: t("empty_chat") }));
    else {
      customer.appendChild(el("div", { html: `<strong>${state.selected.participant_name || ""}</strong>` }));
      customer.appendChild(
        el("div", { className: "channel-pill", text: `${state.selected.provider} / ${state.selected.participant_id}` })
      );
      customer.appendChild(
        el("p", null, [
          el("button", {
            className: "oc-btn secondary",
            type: "button",
            text: t("assign_me"),
            onclick: async () => {
              await call("meta_omnichannel.crm_integration.service.assign_conversation", {
                conversation: state.selected.name,
                user: boot.user,
              });
              await openConversation(state.selected);
              await loadList();
            },
          }),
        ])
      );
      const status = el("select", {
        onchange: async (e) => {
          await call("meta_omnichannel.crm_integration.service.set_conversation_status", {
            conversation: state.selected.name,
            status: e.target.value,
          });
          await openConversation(state.selected);
          await loadList();
        },
      });
      ["Open", "Pending", "Resolved", "Closed"].forEach((s) => {
        const opt = el("option", { text: s, value: s });
        if (state.selected.status === s) opt.selected = true;
        status.appendChild(opt);
      });
      customer.appendChild(el("label", { text: t("status") }));
      customer.appendChild(status);
      customer.appendChild(
        el("p", null, [
          el("button", {
            className: "oc-btn secondary",
            type: "button",
            text: t("create_lead"),
            onclick: async () => {
              await call("meta_omnichannel.crm_integration.service.create_lead_from_conversation", {
                conversation: state.selected.name,
              });
              await openConversation(state.selected);
            },
          }),
        ])
      );
      if (state.panel && state.panel.lead) {
        customer.appendChild(
          el("p", null, [
            el("a", {
              href: `/app/crm-lead/${state.panel.lead.name}`,
              target: "_blank",
              text: `${t("open_crm_lead")} — ${state.panel.lead.lead_name}`,
            }),
          ])
        );
      }
      if (state.panel && state.panel.contact) {
        customer.appendChild(
          el("p", null, [
            el("a", {
              href: `/app/contact/${state.panel.contact.name}`,
              target: "_blank",
              text: t("open_contact"),
            }),
          ])
        );
      }
      customer.appendChild(el("h3", { text: t("notes") }));
      const noteTa = el("textarea", {
        rows: "3",
        value: state.note,
        oninput: (e) => {
          state.note = e.target.value;
        },
      });
      customer.appendChild(noteTa);
      customer.appendChild(
        el("p", null, [
          el("button", {
            className: "oc-btn secondary",
            type: "button",
            text: t("add_note"),
            onclick: async () => {
              await call("meta_omnichannel.crm_integration.service.add_internal_note", {
                conversation: state.selected.name,
                note: state.note,
              });
              state.note = "";
              await openConversation(state.selected);
            },
          }),
        ])
      );
      (state.panel && state.panel.notes ? state.panel.notes : []).forEach((n) => {
        customer.appendChild(
          el("div", { className: "oc-preview" }, [
            el("div", { text: (n.note || "").replace(/<[^>]+>/g, "") }),
            el("div", { className: "channel-pill", text: `${n.author} · ${fmt(n.creation)}` }),
          ])
        );
      });
    }

    shell.appendChild(side);
    shell.appendChild(list);
    shell.appendChild(chat);
    shell.appendChild(customer);
    root.appendChild(shell);
  }

  render();
  loadList();
})();
