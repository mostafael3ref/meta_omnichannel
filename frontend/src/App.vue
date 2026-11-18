<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { call, newNonce } from "./api";
import { isRtl, t } from "./i18n";

const rtl = isRtl();
const loadingList = ref(false);
const loadingChat = ref(false);
const sending = ref(false);
const error = ref("");
const filter = ref("all");
const search = ref("");
const conversations = ref([]);
const selected = ref(null);
const messages = ref([]);
const draft = ref("");
const panel = ref(null);
const noteDraft = ref("");

const navItems = [
  { id: "all", label: () => t("all") },
  { id: "Facebook", label: () => t("facebook") },
  { id: "Instagram", label: () => t("instagram") },
  { id: "WhatsApp", label: () => t("whatsapp") },
  { id: "unread", label: () => t("unread") },
  { id: "me", label: () => t("assigned_to_me") },
  { id: "unassigned", label: () => t("unassigned") },
];

const listQuery = computed(() => {
  const q = { limit: 40, offset: 0, search: search.value || undefined };
  if (["Facebook", "Instagram", "WhatsApp"].includes(filter.value)) {
    q.provider = filter.value;
  }
  if (filter.value === "me") q.assigned = "me";
  if (filter.value === "unassigned") q.assigned = "unassigned";
  return q;
});

async function loadConversations() {
  loadingList.value = true;
  error.value = "";
  try {
    const data = await call("meta_omnichannel.api.inbox.list_conversations", listQuery.value);
    let items = data.items || [];
    if (filter.value === "unread") {
      items = items.filter((c) => (c.unread_count || 0) > 0);
    }
    conversations.value = items;
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    loadingList.value = false;
  }
}

async function openConversation(row) {
  selected.value = row;
  loadingChat.value = true;
  error.value = "";
  try {
    const [msgData, customer] = await Promise.all([
      call("meta_omnichannel.api.inbox.list_messages", {
        conversation: row.name,
        limit: 100,
        offset: 0,
      }),
      call("meta_omnichannel.crm_integration.service.get_customer_panel", {
        conversation: row.name,
      }),
    ]);
    messages.value = msgData.items || [];
    panel.value = customer;
    row.unread_count = 0;
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    loadingChat.value = false;
  }
}

async function send() {
  if (!selected.value || !draft.value.trim() || sending.value) return;
  sending.value = true;
  error.value = "";
  try {
    const result = await call("meta_omnichannel.api.inbox.send_message", {
      conversation: selected.value.name,
      text: draft.value,
      client_nonce: newNonce(),
    });
    if (!result.ok) {
      error.value = result.error || "Send failed";
    }
    draft.value = "";
    await openConversation(selected.value);
    await loadConversations();
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    sending.value = false;
  }
}

async function syncInbox() {
  error.value = "";
  try {
    const result = await call("meta_omnichannel.api.inbox.sync_inbox", { limit: 50 });
    await loadConversations();
    if (result && result.ok === false) {
      error.value = result.error || "Sync failed";
    }
  } catch (e) {
    error.value = e.message || String(e);
  }
}

async function assignToMe() {
  if (!selected.value) return;
  await call("meta_omnichannel.crm_integration.service.assign_conversation", {
    conversation: selected.value.name,
    user: window.__OMNICHANNEL_BOOT__?.user,
  });
  await openConversation(selected.value);
  await loadConversations();
}

async function createLead() {
  if (!selected.value) return;
  await call("meta_omnichannel.crm_integration.service.create_lead_from_conversation", {
    conversation: selected.value.name,
  });
  await openConversation(selected.value);
}

async function setStatus(status) {
  if (!selected.value) return;
  await call("meta_omnichannel.crm_integration.service.set_conversation_status", {
    conversation: selected.value.name,
    status,
  });
  await openConversation(selected.value);
  await loadConversations();
}

async function addNote() {
  if (!selected.value || !noteDraft.value.trim()) return;
  await call("meta_omnichannel.crm_integration.service.add_internal_note", {
    conversation: selected.value.name,
    note: noteDraft.value,
  });
  noteDraft.value = "";
  await openConversation(selected.value);
}

function formatTime(value) {
  if (!value) return "";
  try {
    return new Date(value).toLocaleString();
  } catch (_) {
    return value;
  }
}

watch([filter, search], () => {
  loadConversations();
});

onMounted(loadConversations);
</script>

<template>
  <div class="oc-shell" :dir="rtl ? 'rtl' : 'ltr'">
    <aside class="oc-side">
      <h1 class="oc-brand"><span>{{ t("inbox") }}</span></h1>
      <nav class="oc-nav">
        <button
          v-for="item in navItems"
          :key="item.id"
          type="button"
          :class="{ active: filter === item.id }"
          @click="filter = item.id"
        >
          {{ item.label() }}
        </button>
      </nav>
    </aside>

    <section class="oc-list">
      <div class="oc-list-toolbar">
        <input v-model="search" type="search" :placeholder="t('search')" />
        <button class="oc-btn secondary" type="button" @click="syncInbox">{{ t("sync") }}</button>
      </div>
      <div v-if="loadingList" class="oc-empty">{{ t("loading") }}</div>
      <div v-else-if="!conversations.length" class="oc-empty">{{ t("empty_list") }}</div>
      <button
        v-for="c in conversations"
        :key="c.name"
        type="button"
        class="oc-conv"
        :class="{ active: selected?.name === c.name }"
        @click="openConversation(c)"
      >
        <div class="oc-conv-top">
          <span>{{ c.participant_name || c.participant_id }}</span>
          <span v-if="c.unread_count" class="oc-badge">{{ c.unread_count }}</span>
        </div>
        <div class="channel-pill">{{ c.provider }} · {{ formatTime(c.last_message_at) }}</div>
        <div class="oc-preview">{{ c.last_message_preview }}</div>
      </button>
    </section>

    <section class="oc-chat">
      <header class="oc-chat-header">
        <div>
          <strong>{{ selected?.participant_name || t("empty_chat") }}</strong>
          <div v-if="selected" class="channel-pill">{{ selected.provider }} · {{ selected.status }}</div>
        </div>
        <button
          v-if="selected?.provider === 'Facebook'"
          class="oc-btn secondary"
          type="button"
          @click="call('meta_omnichannel.api.inbox.sync_conversation', { conversation: selected.name }).then(() => openConversation(selected))"
        >
          Sync
        </button>
      </header>

      <div v-if="error" class="oc-error">{{ error }}</div>
      <div v-if="!selected" class="oc-empty">{{ t("empty_chat") }}</div>
      <div v-else-if="loadingChat" class="oc-empty">{{ t("loading") }}</div>
      <div v-else class="oc-messages">
        <div
          v-for="m in messages"
          :key="m.name"
          class="oc-bubble"
          :class="m.direction === 'Outbound' ? 'outbound' : 'inbound'"
        >
          <div>{{ m.text_content || m.message_type }}</div>
          <div v-if="m.attachment_url" class="oc-meta">
            <a :href="m.attachment_url" target="_blank" rel="noopener">Attachment</a>
          </div>
          <div class="oc-meta">
            {{ formatTime(m.sent_at) }}
            <span v-if="m.direction === 'Outbound'"> · {{ m.delivery_status }}</span>
          </div>
        </div>
      </div>

      <div class="oc-composer">
        <textarea
          v-model="draft"
          rows="3"
          :placeholder="t('composer_placeholder')"
          :disabled="!selected || selected.provider !== 'Facebook'"
          @keydown.ctrl.enter="send"
        />
        <button
          class="oc-btn"
          type="button"
          :disabled="!selected || selected.provider !== 'Facebook' || sending || !draft.trim()"
          @click="send"
        >
          {{ t("send") }}
        </button>
      </div>
      <div v-if="selected && selected.provider !== 'Facebook'" class="oc-empty">
        {{ t("pending_channel") }}
      </div>
    </section>

    <aside class="oc-customer">
      <h3>{{ t("customer") }}</h3>
      <div v-if="!selected" class="oc-empty">{{ t("empty_chat") }}</div>
      <template v-else>
        <section>
          <div><strong>{{ selected.participant_name }}</strong></div>
          <div class="channel-pill">{{ selected.provider }} / {{ selected.participant_id }}</div>
          <p>
            <button class="oc-btn secondary" type="button" @click="assignToMe">{{ t("assign_me") }}</button>
          </p>
          <label>{{ t("status") }}</label>
          <select :value="selected.status" @change="setStatus($event.target.value)">
            <option>Open</option>
            <option>Pending</option>
            <option>Resolved</option>
            <option>Closed</option>
          </select>
        </section>

        <section>
          <button class="oc-btn secondary" type="button" @click="createLead">{{ t("create_lead") }}</button>
          <p v-if="panel?.lead">
            <a :href="`/app/crm-lead/${panel.lead.name}`" target="_blank">{{ t("open_crm_lead") }}</a>
            — {{ panel.lead.lead_name }} ({{ panel.lead.status }})
          </p>
          <p v-if="panel?.contact">
            <a :href="`/app/contact/${panel.contact.name}`" target="_blank">{{ t("open_contact") }}</a>
          </p>
        </section>

        <section>
          <h3>{{ t("notes") }}</h3>
          <textarea v-model="noteDraft" rows="3" />
          <p>
            <button class="oc-btn secondary" type="button" @click="addNote">{{ t("add_note") }}</button>
          </p>
          <div v-for="n in panel?.notes || []" :key="n.name" class="oc-preview">
            <div v-html="n.note"></div>
            <div class="channel-pill">{{ n.author }} · {{ formatTime(n.creation) }}</div>
          </div>
        </section>
      </template>
    </aside>
  </div>
</template>
