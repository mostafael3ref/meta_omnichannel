/**
 * Omnichannel Inbox runtime bundle (no-build fallback).
 * Preferred path on Linux Bench: build Vue sources from /frontend via `npm run build`.
 */
(function () {
  const boot = window.omnichannelBoot || {};
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
      send_image: "Send image",
      send_document: "Send document",
      attach_image: "Attach file",
      remove_image: "Remove",
      image_only_hint: "Only the image will be sent",
      image_and_text_hint: "Image and text are sent as two separate Messenger messages",
      image_sent_text_pending:
        "Image was sent. The text was not sent — review the draft and send when ready.",
      image_type_rejected: "Use a JPEG, PNG, GIF, PDF, or DOCX file",
      image_too_large: "File must be 8 MiB or smaller",
      image_uncertain:
        "Delivery is unconfirmed — the file may already have been sent. Remove this selection before choosing another file; do not resend.",
      document_only_hint: "Only the document will be sent",
      document_and_text_hint: "Document and text are sent as two separate Messenger messages",
      document_sent_text_pending:
        "Document was sent. The text was not sent — review the draft and send when ready.",
      document_uncertain:
        "Delivery is unconfirmed — the document may already have been sent. Remove this selection before choosing another file; do not resend.",
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
      open_attachment: "Open attachment",
      attachment_unavailable: "Attachment unavailable",
      close_lightbox: "Close",
      download_attachment: "Download",
      page_id_label: "Page ID",
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
      send_image: "إرسال صورة",
      send_document: "إرسال مستند",
      attach_image: "إرفاق ملف",
      remove_image: "إزالة",
      image_only_hint: "ستُرسل الصورة فقط",
      image_and_text_hint: "تُرسل الصورة والنص كرسالتين منفصلتين في ماسنجر",
      image_sent_text_pending:
        "أُرسلت الصورة. لم يُرسل النص — راجع المسودة وأرسل عند الجاهزية.",
      image_type_rejected: "استخدم ملف JPEG أو PNG أو GIF أو PDF أو DOCX",
      image_too_large: "يجب ألا يتجاوز الملف 8 ميبيبايت",
      image_uncertain:
        "لم يُؤكَّد التسليم — ربما أُرسل الملف بالفعل. أزل هذا الاختيار قبل اختيار ملف آخر؛ لا تعِد الإرسال.",
      document_only_hint: "سيُرسل المستند فقط",
      document_and_text_hint: "يُرسل المستند والنص كرسالتين منفصلتين في ماسنجر",
      document_sent_text_pending:
        "أُرسل المستند. لم يُرسل النص — راجع المسودة وأرسل عند الجاهزية.",
      document_uncertain:
        "لم يُؤكَّد التسليم — ربما أُرسل المستند بالفعل. أزل هذا الاختيار قبل اختيار ملف آخر؛ لا تعِد الإرسال.",
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
      open_attachment: "فتح المرفق",
      attachment_unavailable: "المرفق غير متاح",
      close_lightbox: "إغلاق",
      download_attachment: "تنزيل",
      page_id_label: "معرف الصفحة",
    },
  };
  const t = (k) => (rtl ? i18n.ar : i18n.en)[k] || i18n.en[k] || k;

  function csrf() {
    return boot.csrf_token || window.csrf_token || "";
  }

  /** Accept https URLs only — never javascript:/data:/http:. */
  function safeHttpsUrl(raw) {
    const s = String(raw || "").trim();
    if (!s) return null;
    try {
      const u = new URL(s);
      if (u.protocol !== "https:") return null;
      return u.href;
    } catch (_) {
      return null;
    }
  }

  /**
   * Trusted same-origin private File path only.
   * Rejects protocol-relative, traversal, schemes, and untrusted relative paths.
   */
  function safePrivateFilePath(raw) {
    const s = String(raw || "").trim();
    if (!s || !s.startsWith("/private/files/")) return null;
    if (s.startsWith("//")) return null;
    if (s.includes("..") || s.includes("\\") || s.includes("\0")) return null;
    if (/\s/.test(s)) return null;
    if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(s)) return null;
    return s;
  }

  /** Prefer private File paths or https CDN — never javascript:/data:/http:. */
  function safeAttachmentUrl(raw) {
    const s = String(raw || "").trim();
    if (!s) return null;
    if (s.startsWith("/")) return safePrivateFilePath(s);
    return safeHttpsUrl(s);
  }

  /** Local Ready File first; fall back to Meta HTTPS URL. */
  function preferredAttachmentUrl(m) {
    return safeAttachmentUrl(m && m.attachment_file_url) || safeAttachmentUrl(m && m.attachment_url);
  }

  function attachmentLinkLabel(messageType) {
    const kind = String(messageType || "").trim();
    if (kind === "Image" || kind === "Audio" || kind === "Video" || kind === "File") {
      return rtl ? t("open_attachment") : `Open ${kind.toLowerCase()}`;
    }
    return t("open_attachment");
  }

  function renderSafeAttachmentLink(url, messageType) {
    return el("a", {
      className: "oc-attach-link",
      href: url,
      target: "_blank",
      rel: "noopener noreferrer",
      text: attachmentLinkLabel(messageType),
    });
  }

  /**
   * Same-origin private Files: Frappe ``download_file`` (session + File/Message
   * permission → Content-Disposition: attachment). External Meta HTTPS stays a
   * plain link — browsers may ignore cross-origin ``download``.
   */
  function attachmentDownloadHref(url) {
    const privatePath = safePrivateFilePath(url);
    if (privatePath) {
      return (
        "/api/method/frappe.handler.download_file?file_url=" +
        encodeURIComponent(privatePath)
      );
    }
    return safeHttpsUrl(url) || null;
  }

  function renderDownloadAction(url) {
    const href = attachmentDownloadHref(url);
    if (!href) return null;
    const isPrivate = !!safePrivateFilePath(url);
    const attrs = {
      className: "oc-btn secondary",
      href: href,
      rel: "noopener noreferrer",
      text: t("download_attachment"),
    };
    if (!isPrivate) {
      attrs.target = "_blank";
      attrs.download = "";
      attrs.title = t("open_attachment");
    }
    return el("a", attrs);
  }

  let lightboxTrigger = null;

  function getLightboxFocusables(root) {
    if (!root) return [];
    return Array.prototype.slice
      .call(root.querySelectorAll('a[href], button:not([disabled])'))
      .filter(function (node) {
        return node && node.offsetParent !== null;
      });
  }

  function closeImageLightbox() {
    const existing = document.getElementById("oc-lightbox");
    if (existing) existing.remove();
    document.removeEventListener("keydown", onLightboxKeydown);
    const trigger = lightboxTrigger;
    lightboxTrigger = null;
    if (trigger && typeof trigger.focus === "function") {
      try {
        trigger.focus();
      } catch (_) {}
    }
  }

  function onLightboxKeydown(ev) {
    if (ev.key === "Escape") {
      ev.preventDefault();
      closeImageLightbox();
      return;
    }
    if (ev.key !== "Tab") return;
    const root = document.getElementById("oc-lightbox");
    const focusables = getLightboxFocusables(root);
    if (!focusables.length) {
      ev.preventDefault();
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  }

  function openImageLightbox(url, altText, triggerEl) {
    const existing = document.getElementById("oc-lightbox");
    if (existing) existing.remove();
    document.removeEventListener("keydown", onLightboxKeydown);
    lightboxTrigger = triggerEl || null;

    const backdrop = el("div", {
      id: "oc-lightbox",
      className: "oc-lightbox",
      role: "dialog",
      "aria-modal": "true",
      "aria-label": altText || t("open_attachment"),
      onclick: (ev) => {
        if (ev.target === backdrop) closeImageLightbox();
      },
    });
    const panel = el("div", { className: "oc-lightbox-panel" });
    panel.appendChild(
      el("img", {
        className: "oc-lightbox-img",
        src: url,
        alt: altText || "Image attachment",
      })
    );
    const actions = el("div", { className: "oc-lightbox-actions" });
    const downloadBtn = renderDownloadAction(url);
    if (downloadBtn) actions.appendChild(downloadBtn);
    actions.appendChild(
      el("button", {
        type: "button",
        className: "oc-btn",
        text: t("close_lightbox"),
        onclick: closeImageLightbox,
      })
    );
    panel.appendChild(actions);
    backdrop.appendChild(panel);
    document.body.appendChild(backdrop);
    document.addEventListener("keydown", onLightboxKeydown);
    const focusables = getLightboxFocusables(backdrop);
    if (focusables.length) focusables[0].focus();
  }

  function renderAttachmentBlock(m) {
    const localRaw = m.attachment_file_url;
    const metaRaw = m.attachment_url;
    const url = preferredAttachmentUrl(m);
    const type = m.message_type || "Unknown";
    if (!url) {
      if (localRaw || metaRaw) {
        return el("div", { className: "oc-attach-fallback", text: t("attachment_unavailable") });
      }
      return null;
    }
    const wrap = el("div", { className: "oc-attach" });
    const onMediaError = (ev) => {
      const node = ev.currentTarget;
      if (!node || !node.parentNode) return;
      node.replaceWith(renderSafeAttachmentLink(url, type));
    };
    if (type === "Image") {
      const alt = (m.text_content || "").trim() || "Image attachment";
      wrap.appendChild(
        el("img", {
          className: "oc-attach-media oc-attach-thumb",
          src: url,
          alt: alt,
          loading: "lazy",
          tabindex: "0",
          role: "button",
          title: t("open_attachment"),
          onclick: (ev) => openImageLightbox(url, alt, ev.currentTarget),
          onkeydown: (ev) => {
            if (ev.key === "Enter" || ev.key === " ") {
              ev.preventDefault();
              openImageLightbox(url, alt, ev.currentTarget);
            }
          },
          onerror: onMediaError,
        })
      );
    } else if (type === "Audio") {
      wrap.appendChild(
        el("audio", {
          className: "oc-attach-media",
          src: url,
          controls: true,
          preload: "metadata",
          onerror: onMediaError,
        })
      );
    } else if (type === "Video") {
      wrap.appendChild(
        el("video", {
          className: "oc-attach-media",
          src: url,
          controls: true,
          preload: "metadata",
          playsinline: true,
          onerror: onMediaError,
        })
      );
    } else {
      wrap.appendChild(renderSafeAttachmentLink(url, type === "File" ? "File" : type));
    }
    return wrap;
  }

  function renderMessageBody(m) {
    const parts = [];
    const caption = String(m.text_content || "").trim();
    const attach = renderAttachmentBlock(m);
    if (caption) parts.push(el("div", { className: "oc-bubble-text", text: caption }));
    if (attach) parts.push(attach);
    if (!caption && !attach) parts.push(el("div", { text: m.message_type || "" }));
    return parts;
  }

  function frappeErrorMessage(data, fallback) {
    let message = fallback || "Request failed";
    try {
      const msgs = JSON.parse(data._server_messages || "[]");
      if (msgs.length) message = JSON.parse(msgs[0]).message || message;
    } catch (_) {}
    if (data.exception) message = data.exception;
    return message;
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
    // Successful Frappe responses may include _server_messages (msgprint).
    // Only treat HTTP errors / exc as failures — do not discard data.message.
    if (!response.ok || data.exc) {
      throw new Error(frappeErrorMessage(data, data.exc || "Request failed"));
    }
    return data.message;
  }

  /**
   * Authenticated multipart POST. Do not set Content-Type — the browser must
   * supply the multipart boundary. CSRF still goes on the header.
   */
  async function callMultipart(method, fields, fileField, file) {
    const body = new FormData();
    Object.entries(fields || {}).forEach(([key, value]) => {
      if (value === undefined || value === null) return;
      body.append(key, String(value));
    });
    if (fileField && file) {
      body.append(fileField, file, file.name || fileField || "upload");
    }
    const response = await fetch(`/api/method/${method}`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "X-Frappe-CSRF-Token": csrf(),
        "X-Requested-With": "XMLHttpRequest",
      },
      body,
    });
    const data = await response.json();
    if (!response.ok || data.exc) {
      throw new Error(frappeErrorMessage(data, data.exc || "Request failed"));
    }
    return data.message;
  }

  function nonce() {
    return crypto.randomUUID ? crypto.randomUUID() : `n-${Date.now()}`;
  }

  const MAX_COMPOSER_ATTACHMENT_BYTES = 8 * 1024 * 1024;
  const MAX_COMPOSER_IMAGE_BYTES = MAX_COMPOSER_ATTACHMENT_BYTES;
  const ALLOWED_IMAGE_MIME = {
    "image/jpeg": true,
    "image/jpg": true,
    "image/png": true,
    "image/gif": true,
  };
  const DOCX_MIME =
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
  const ALLOWED_DOCUMENT_MIME = {
    "application/pdf": true,
  };
  ALLOWED_DOCUMENT_MIME[DOCX_MIME] = true;
  const COMPOSER_ACCEPT =
    "image/jpeg,image/png,image/gif,.jpg,.jpeg,.png,.gif," +
    "application/pdf,.pdf," +
    DOCX_MIME +
    ",.docx";

  /**
   * Per-conversation pending outbound attachment (image or document).
   * Kept as ``imageDrafts`` for existing wiring/tests; entries include ``kind``.
   */
  const imageDrafts = Object.create(null);
  /**
   * Text captured for a conversation when attachment Sent but follow-up text was deferred
   * (e.g. user switched threads). Restored on reopen; never auto-sent later.
   */
  const deferredTextByConversation = Object.create(null);

  function stashDeferredText(conversationName, text, nonce) {
    const key = conversationName || "";
    const trimmed = (text || "").trim();
    if (!key || !trimmed) return;
    deferredTextByConversation[key] = { text: trimmed, nonce: nonce || null };
  }

  function clearDeferredText(conversationName) {
    delete deferredTextByConversation[conversationName || ""];
  }

  function restoreDeferredTextIfAny(conversationName) {
    const pending = deferredTextByConversation[conversationName || ""];
    if (!pending) return;
    state.draft = pending.text;
    if (pending.nonce) {
      sendAttempt = {
        conversation: conversationName,
        draftText: pending.text,
        nonce: pending.nonce,
      };
    }
  }

  function imageFileKey(file) {
    if (!file) return "";
    return [file.name || "", file.size || 0, file.lastModified || 0, file.type || ""].join("|");
  }

  function fileMime(file) {
    return String((file && file.type) || "")
      .split(";", 1)[0]
      .trim()
      .toLowerCase();
  }

  function isAllowedComposerImage(file) {
    if (!file) return false;
    const mime = fileMime(file);
    if (ALLOWED_IMAGE_MIME[mime]) return true;
    const name = String(file.name || "").toLowerCase();
    return /\.(jpe?g|png|gif)$/.test(name);
  }

  function isAllowedComposerDocument(file) {
    if (!file) return false;
    const mime = fileMime(file);
    if (ALLOWED_DOCUMENT_MIME[mime]) return true;
    const name = String(file.name || "").toLowerCase();
    return /\.(pdf|docx)$/.test(name);
  }

  function attachmentKindForFile(file) {
    if (isAllowedComposerImage(file)) return "image";
    if (isAllowedComposerDocument(file)) return "document";
    return null;
  }

  function documentTypeLabel(file, mime) {
    const name = String((file && file.name) || "").toLowerCase();
    const type = String(mime || fileMime(file) || "").toLowerCase();
    if (type === "application/pdf" || name.endsWith(".pdf")) return "PDF";
    if (type === DOCX_MIME || name.endsWith(".docx")) return "DOCX";
    return "File";
  }

  function formatAttachmentSize(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10 * 1024 ? 1 : 0) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function attachmentSendSpec(kind) {
    if (kind === "document") {
      return {
        kind: "document",
        method: "meta_omnichannel.api.inbox.send_document_message",
        field: "document",
        defaultName: "document",
        onlyHint: "document_only_hint",
        andTextHint: "document_and_text_hint",
        uncertain: "document_uncertain",
        sentPending: "document_sent_text_pending",
        sendLabel: "send_document",
      };
    }
    return {
      kind: "image",
      method: "meta_omnichannel.api.inbox.send_image_message",
      field: "image",
      defaultName: "image",
      onlyHint: "image_only_hint",
      andTextHint: "image_and_text_hint",
      uncertain: "image_uncertain",
      sentPending: "image_sent_text_pending",
      sendLabel: "send_image",
    };
  }

  function revokeImagePreview(draft) {
    if (!draft || !draft.previewUrl) return;
    try {
      if (typeof URL !== "undefined" && URL.revokeObjectURL) URL.revokeObjectURL(draft.previewUrl);
    } catch (_) {}
    draft.previewUrl = null;
  }

  function clearImageDraft(conversationName) {
    const key = conversationName || "";
    const draft = imageDrafts[key];
    if (!draft) return;
    revokeImagePreview(draft);
    delete imageDrafts[key];
  }

  function clearAllImageDrafts() {
    Object.keys(imageDrafts).forEach((key) => clearImageDraft(key));
  }

  function currentImageDraft() {
    if (!state.selected || !state.selected.name) return null;
    return imageDrafts[state.selected.name] || null;
  }

  function nonceForImage(conversationName, file) {
    const key = imageFileKey(file);
    const existing = imageDrafts[conversationName];
    if (existing && existing.fileKey === key && existing.nonce) {
      return existing.nonce;
    }
    return nonce();
  }

  function selectComposerAttachment(file) {
    if (!state.selected || state.selected.provider !== "Facebook") return false;
    if (state.sending) return false;
    const conversationName = state.selected.name;
    if (!file) return false;
    const kind = attachmentKindForFile(file);
    if (!kind) {
      state.error = t("image_type_rejected");
      render();
      return false;
    }
    if ((file.size || 0) > MAX_COMPOSER_ATTACHMENT_BYTES) {
      state.error = t("image_too_large");
      render();
      return false;
    }
    const prev = imageDrafts[conversationName];
    const fileKey = imageFileKey(file);
    let nextNonce = nonce();
    if (prev && prev.fileKey === fileKey && prev.kind === kind && prev.nonce) {
      nextNonce = prev.nonce;
    }
    if (prev) revokeImagePreview(prev);
    let previewUrl = null;
    if (kind === "image") {
      try {
        if (typeof URL !== "undefined" && URL.createObjectURL) {
          previewUrl = URL.createObjectURL(file);
        }
      } catch (_) {
        previewUrl = null;
      }
    }
    const mime = file.type || "";
    const spec = attachmentSendSpec(kind);
    imageDrafts[conversationName] = {
      conversation: conversationName,
      kind: kind,
      file: file,
      fileKey: fileKey,
      filename: file.name || spec.defaultName,
      mime: mime,
      typeLabel: kind === "document" ? documentTypeLabel(file, mime) : "",
      size: file.size || 0,
      previewUrl: previewUrl,
      nonce: nextNonce,
      blockResend: false,
    };
    state.error = "";
    render();
    return true;
  }

  /** Image-only selector kept for tests; picker uses ``selectComposerAttachment``. */
  function selectComposerImage(file) {
    if (!file) return false;
    if (!isAllowedComposerImage(file)) {
      if (isAllowedComposerDocument(file)) {
        return selectComposerAttachment(file);
      }
      state.error = t("image_type_rejected");
      render();
      return false;
    }
    return selectComposerAttachment(file);
  }

  function removeComposerImage() {
    if (!state.selected || !state.selected.name) return;
    if (state.sending) return;
    clearImageDraft(state.selected.name);
    state.error = "";
    render();
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
    sending: false,
  };

  /** One nonce per logical send attempt (conversation + unchanged draft text). */
  let sendAttempt = { conversation: null, draftText: null, nonce: null };
  let sendGeneration = 0;

  function clearSendAttempt() {
    sendAttempt = { conversation: null, draftText: null, nonce: null };
  }

  function nonceForDraft(conversationName, text) {
    const trimmed = (text || "").trim();
    if (
      sendAttempt.nonce &&
      sendAttempt.conversation === conversationName &&
      sendAttempt.draftText === trimmed
    ) {
      return sendAttempt.nonce;
    }
    const next = nonce();
    sendAttempt = { conversation: conversationName, draftText: trimmed, nonce: next };
    return next;
  }

  function composerCanCompose() {
    return !!state.selected && state.selected.provider === "Facebook" && !state.sending;
  }

  function composerCanSend() {
    const attachmentDraft = currentImageDraft();
    const hasAttachment = !!(attachmentDraft && attachmentDraft.file);
    const attachmentBlocked = !!(hasAttachment && attachmentDraft.blockResend);
    return (
      composerCanCompose() &&
      !attachmentBlocked &&
      (hasAttachment || !!(state.draft || "").trim())
    );
  }

  function composerSendButtonLabel(attachmentDraft) {
    const hasAttachment = !!(attachmentDraft && attachmentDraft.file);
    if (hasAttachment && (state.draft || "").trim()) return t("send");
    if (hasAttachment) return t(attachmentSendSpec(attachmentDraft.kind).sendLabel);
    return t("send");
  }

  /**
   * Patch live composer controls without a full render.
   * Needed because draft input updates state.draft but does not rebuild the DOM;
   * otherwise Send stays disabled after a successful clear until the next render.
   */
  function syncComposerControls() {
    if (!root) return;
    const canCompose = composerCanCompose();
    const canSend = composerCanSend();
    const attachmentDraft = currentImageDraft();
    const hasAttachment = !!(attachmentDraft && attachmentDraft.file);
    const spec = hasAttachment ? attachmentSendSpec(attachmentDraft.kind) : null;
    const sendBtn = root.querySelector(".oc-composer-send");
    if (sendBtn) {
      sendBtn.disabled = !canSend;
      sendBtn.textContent = composerSendButtonLabel(attachmentDraft);
    }
    const ta = root.querySelector(".oc-composer textarea");
    if (ta) ta.disabled = !canCompose;
    const attach = root.querySelector(".oc-composer-attach");
    if (attach) attach.disabled = !canCompose;
    const fileInput = root.querySelector(".oc-composer-file");
    if (fileInput) fileInput.disabled = !canCompose;
    const removeBtn = root.querySelector(".oc-composer-remove");
    if (removeBtn) removeBtn.disabled = !!state.sending;
    const hint = root.querySelector(".oc-composer-hint");
    if (hint && hasAttachment && spec) {
      hint.textContent = (state.draft || "").trim() ? t(spec.andTextHint) : t(spec.onlyHint);
    }
  }

  /** Clear sending and unlock composer without rebuilding the message list. */
  function finishSending() {
    state.sending = false;
    if (state.selected && !state.loadingChat && root.querySelector(".oc-chat .oc-messages")) {
      syncComposerControls();
      return;
    }
    render();
  }

  function onDraftInput(value) {
    state.draft = value;
    const trimmed = (value || "").trim();
    if (sendAttempt.draftText != null && trimmed !== sendAttempt.draftText) {
      // Editing starts a new logical attempt — do not reuse the old nonce.
      clearSendAttempt();
      sendGeneration += 1;
    }
    syncComposerControls();
  }

  const REALTIME_EVENT = "omnichannel_conversation_updated";
  const REALTIME_DEBOUNCE_MS = 400;
  const REALTIME_SUBSCRIBE_RETRY_MS = 250;
  const REALTIME_SUBSCRIBE_MAX_ATTEMPTS = 40;
  let realtimeSubscribed = false;
  let realtimeEventSocket = null;
  let realtimeBoundSocket = null;
  let realtimeSubscribeRetryTimer = null;
  let realtimeSubscribeAttempts = 0;
  let realtimeDebounceTimer = null;
  let pendingRealtimeConversations = new Set();
  let realtimeReconnectBound = false;
  let listRefreshGen = 0;
  let messagesRefreshGen = 0;
  let openConversationSeq = 0;
  /** When true, the next non-loading messages render scrolls to the latest message. */
  let pendingScrollToLatest = false;
  let softListRunning = false;
  let softListWanted = false;
  let softListDone = null;
  let pendingSoftMessageRefresh = null;

  const root = document.getElementById("omnichannel-app");
  if (!root) return;

  /** Event payload is a hint only — require a nonempty conversation name string. */
  function validateConversationHint(payload) {
    if (!payload || typeof payload !== "object") return null;
    const name = payload.conversation;
    if (typeof name !== "string") return null;
    const trimmed = name.trim();
    return trimmed || null;
  }

  function isPermissionError(err) {
    const msg = String((err && err.message) || err || "").toLowerCase();
    return (
      msg.includes("not permitted") ||
      msg.includes("permissionerror") ||
      msg.includes("permission denied") ||
      msg.includes("forbidden") ||
      msg.includes("403")
    );
  }

  function captureFocusedField(selector) {
    const node = root.querySelector(selector);
    if (!node || document.activeElement !== node) return null;
    return {
      focus: true,
      start: typeof node.selectionStart === "number" ? node.selectionStart : null,
      end: typeof node.selectionEnd === "number" ? node.selectionEnd : null,
    };
  }

  function restoreFocusedField(selector, field) {
    if (!field || !field.focus) return;
    const node = root.querySelector(selector);
    if (!node) return;
    node.focus();
    if (field.start != null && field.end != null && typeof node.setSelectionRange === "function") {
      try {
        node.setSelectionRange(field.start, field.end);
      } catch (_) {}
    }
  }

  function captureUiChrome() {
    const msgs = root.querySelector(".oc-messages");
    const chrome = {
      scrollTop: 0,
      nearBottom: true,
      composer: captureFocusedField(".oc-composer textarea"),
      search: captureFocusedField(".oc-list-toolbar input[type='search']"),
      note: captureFocusedField(".oc-customer textarea"),
    };
    if (msgs) {
      chrome.scrollTop = msgs.scrollTop;
      chrome.nearBottom = msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 80;
    }
    return chrome;
  }

  function restoreUiChrome(chrome, options) {
    if (!chrome) return;
    const opts = options || {};
    const msgs = root.querySelector(".oc-messages");
    if (msgs) {
      // Opening a conversation always lands on the latest message.
      // Soft message refreshes keep prior position unless the user was near the bottom.
      // preserveChat reuses the same .oc-messages node — restore exact scrollTop only
      // (near-bottom follow belongs to content refresh, not list-only soft redraw).
      if (pendingScrollToLatest && !state.loadingChat) {
        msgs.scrollTop = msgs.scrollHeight;
        pendingScrollToLatest = false;
      } else if (!pendingScrollToLatest) {
        if (opts.preserveScrollExact) msgs.scrollTop = chrome.scrollTop;
        else if (chrome.nearBottom) msgs.scrollTop = msgs.scrollHeight;
        else msgs.scrollTop = chrome.scrollTop;
      }
    }
    restoreFocusedField(".oc-composer textarea", chrome.composer);
    restoreFocusedField(".oc-list-toolbar input[type='search']", chrome.search);
    restoreFocusedField(".oc-customer textarea", chrome.note);
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "className") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k === "value") node.value = v == null ? "" : v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
      else if (v !== false && v != null) node.setAttribute(k, v === true ? "" : v);
    });
    (children || []).forEach((c) => c && node.appendChild(c));
    return node;
  }

  function listConversationArgs() {
    const args = { limit: 40, offset: 0 };
    if (state.search) args.search = state.search;
    if (["Facebook", "Instagram", "WhatsApp"].includes(state.filter)) args.provider = state.filter;
    if (state.filter === "me") args.assigned = "me";
    if (state.filter === "unassigned") args.assigned = "unassigned";
    return args;
  }

  function mergeSelectedFromList(items) {
    if (!state.selected || !state.selected.name) return;
    const fresh = (items || []).find((c) => c && c.name === state.selected.name);
    if (!fresh) return;
    state.selected = Object.assign({}, state.selected, {
      participant_name: fresh.participant_name,
      participant_id: fresh.participant_id,
      provider: fresh.provider,
      status: fresh.status,
      last_message_at: fresh.last_message_at,
      last_inbound_at: fresh.last_inbound_at,
      last_message_preview: fresh.last_message_preview,
      unread_count: fresh.unread_count,
      page_id: fresh.page_id,
      page_name: fresh.page_name,
    });
  }

  function conversationPageName(c) {
    return String((c && c.page_name) || "").trim();
  }

  function conversationPageId(c) {
    return String((c && c.page_id) || "").trim();
  }

  /** Facebook: page_name primary; other providers: participant. */
  function conversationPrimaryTitle(c) {
    if (!c) return t("empty_chat");
    if (String(c.provider || "") === "Facebook") {
      const pageName = conversationPageName(c);
      if (pageName) return pageName;
    }
    return String(c.participant_name || c.participant_id || "").trim() || t("empty_chat");
  }

  function buildChatHeaderTitle(selected) {
    const children = [
      el("strong", {
        className: "oc-chat-title-primary",
        text: conversationPrimaryTitle(selected),
      }),
    ];
    if (selected) {
      children.push(
        el("div", {
          className: "channel-pill",
          text: `${selected.provider} · ${selected.status}`,
        })
      );
      // Facebook only: one compact Page ID line (never duplicate page_name).
      if (String(selected.provider || "") === "Facebook") {
        const pageId = conversationPageId(selected);
        if (pageId) {
          children.push(
            el("div", {
              className: "oc-chat-page-id",
              text: `${t("page_id_label")}: ${pageId}`,
            })
          );
        }
      }
    }
    return el("div", { className: "oc-chat-title" }, children);
  }

  async function loadList() {
    state.loadingList = true;
    state.error = "";
    const gen = ++listRefreshGen;
    render();
    try {
      const data = await call("meta_omnichannel.api.inbox.list_conversations", listConversationArgs());
      if (gen !== listRefreshGen) return;
      let items = data.items || [];
      if (state.filter === "unread") items = items.filter((c) => (c.unread_count || 0) > 0);
      state.conversations = items;
      mergeSelectedFromList(items);
    } catch (e) {
      if (gen !== listRefreshGen) return;
      state.error = e.message || String(e);
    } finally {
      if (gen === listRefreshGen) state.loadingList = false;
      render();
    }
  }

  /**
   * Soft list refresh for realtime — no loading flash.
   * Coalesces overlapping calls so the newest filter/search query always runs and wins.
   */
  async function softLoadList() {
    softListWanted = true;
    if (softListRunning) return softListDone;
    softListRunning = true;
    softListDone = (async () => {
      try {
        while (softListWanted) {
          softListWanted = false;
          const gen = ++listRefreshGen;
          const args = listConversationArgs();
          try {
            const data = await call("meta_omnichannel.api.inbox.list_conversations", args);
            if (gen !== listRefreshGen || softListWanted) continue;
            let items = data.items || [];
            if (state.filter === "unread") items = items.filter((c) => (c.unread_count || 0) > 0);
            state.conversations = items;
            mergeSelectedFromList(items);
            render({ preserveChat: true });
          } catch (e) {
            if (gen !== listRefreshGen || softListWanted) continue;
            state.error = e.message || String(e);
            render({ preserveChat: true });
          }
        }
      } finally {
        softListRunning = false;
        softListDone = null;
      }
    })();
    return softListDone;
  }

  function clearInaccessibleConversation() {
    state.selected = null;
    state.messages = [];
    state.panel = null;
    state.draft = "";
    state.note = "";
    state.loadingChat = false;
    state.sending = false;
    pendingSoftMessageRefresh = null;
    pendingScrollToLatest = false;
    clearSendAttempt();
    clearAllImageDrafts();
    Object.keys(deferredTextByConversation).forEach((key) => clearDeferredText(key));
    sendGeneration += 1;
    state.error = t("empty_chat");
    messagesRefreshGen += 1;
    openConversationSeq += 1;
    render();
  }

  async function drainPendingSoftMessageRefresh() {
    const pending = pendingSoftMessageRefresh;
    pendingSoftMessageRefresh = null;
    if (!pending) return;
    if (!state.selected || state.selected.name !== pending) return;
    if (state.loadingChat) {
      pendingSoftMessageRefresh = pending;
      return;
    }
    await softRefreshMessages(pending);
  }

  /** Refresh messages for the active thread only — never reloads customer panel. */
  async function softRefreshMessages(conversationName) {
    if (!conversationName || !state.selected || state.selected.name !== conversationName) return;
    // Queue until openConversation finishes so realtime hints are not dropped.
    if (state.loadingChat) {
      pendingSoftMessageRefresh = conversationName;
      return;
    }
    const gen = ++messagesRefreshGen;
    const selectedName = conversationName;
    try {
      const msgData = await call("meta_omnichannel.api.inbox.list_messages", {
        conversation: selectedName,
        limit: 100,
        offset: 0,
      });
      if (gen !== messagesRefreshGen) return;
      if (!state.selected || state.selected.name !== selectedName) return;
      state.messages = (msgData && msgData.items) || [];
      // Soft realtime refresh must never mark read / zero unread.
      if (msgData && msgData.conversation) {
        const c = msgData.conversation;
        if (c.unread_count != null) state.selected.unread_count = c.unread_count;
        if (c.last_inbound_at != null) state.selected.last_inbound_at = c.last_inbound_at;
        if (c.last_message_at != null) state.selected.last_message_at = c.last_message_at;
        if (c.page_name != null) state.selected.page_name = c.page_name;
        if (c.page_id != null) state.selected.page_id = c.page_id;
      }
      render();
    } catch (e) {
      if (gen !== messagesRefreshGen) return;
      if (!state.selected || state.selected.name !== selectedName) return;
      if (isPermissionError(e)) {
        clearInaccessibleConversation();
        return;
      }
      state.error = e.message || String(e);
      render();
    }
  }

  /**
   * After a confirmed outbound send: soft-refresh the active thread without
   * full-chat Loading (keeps existing bubbles visible), then soft-refresh the
   * conversation list preview (preserveChat — no chat rebuild / error wipe).
   * Never clears state.messages first. Scroll follows soft-refresh rules
   * (near-bottom → tip; reading older → preserve).
   */
  async function refreshAfterSend(conversationName) {
    if (!conversationName) return;
    if (state.selected && state.selected.name === conversationName) {
      await softRefreshMessages(conversationName);
    }
    await softLoadList();
  }

  async function openConversation(row) {
    const seq = ++openConversationSeq;
    // Open-time read watermark: prefer inbound/list tip; wall-clock fallback so
    // mark_conversation_read always gets a race-safe as_of (never omit).
    const asOf =
      row.last_inbound_at || row.last_message_at || new Date().toISOString();
    state.selected = row;
    restoreDeferredTextIfAny(row && row.name);
    state.loadingChat = true;
    state.error = "";
    pendingScrollToLatest = true;
    messagesRefreshGen += 1;
    const openGen = messagesRefreshGen;
    render();
    // Outer try/finally guarantees loadingChat cleanup + pending drain even when
    // the list_messages phase returns early (e.g. messagesRefreshGen invalidated).
    try {
      // Load messages independently of the customer panel so a panel failure
      // cannot leave .oc-messages empty after a successful list_messages call.
      try {
        const msgData = await call("meta_omnichannel.api.inbox.list_messages", {
          conversation: row.name,
          limit: 100,
          offset: 0,
        });
        if (seq !== openConversationSeq || openGen !== messagesRefreshGen) return;
        if (!state.selected || state.selected.name !== row.name) return;
        state.messages = (msgData && msgData.items) || [];
        // Explicit open/read only — never from soft refresh or pagination.
        try {
          const readData = await call(
            "meta_omnichannel.api.inbox.mark_conversation_read",
            { conversation: row.name, as_of: asOf }
          );
          if (seq !== openConversationSeq || openGen !== messagesRefreshGen) return;
          if (!state.selected || state.selected.name !== row.name) return;
          const unread =
            readData && readData.unread_count != null ? readData.unread_count : 0;
          row.unread_count = unread;
          state.selected.unread_count = unread;
        } catch (readErr) {
          if (seq !== openConversationSeq || openGen !== messagesRefreshGen) return;
          if (!state.selected || state.selected.name !== row.name) return;
          if (isPermissionError(readErr)) {
            clearInaccessibleConversation();
            return;
          }
        }
      } catch (e) {
        if (seq !== openConversationSeq || openGen !== messagesRefreshGen) return;
        if (!state.selected || state.selected.name !== row.name) return;
        if (isPermissionError(e)) {
          clearInaccessibleConversation();
          return;
        }
        state.messages = [];
        state.error = e.message || String(e);
      }

      try {
        if (seq !== openConversationSeq || openGen !== messagesRefreshGen) return;
        if (!state.selected || state.selected.name !== row.name) return;
        state.panel = await call("meta_omnichannel.crm_integration.service.get_customer_panel", {
          conversation: row.name,
        });
      } catch (e) {
        if (seq !== openConversationSeq || openGen !== messagesRefreshGen) return;
        if (!state.selected || state.selected.name !== row.name) return;
        state.panel = null;
        if (isPermissionError(e)) {
          clearInaccessibleConversation();
          return;
        }
        if (!state.error) state.error = e.message || String(e);
      }
    } finally {
      // Only the current open may finalize; stale opens (including same-conversation
      // reopen) must not touch loadingChat / drain for a newer request.
      if (seq !== openConversationSeq) return;
      if (!state.selected || state.selected.name !== row.name) return;
      state.loadingChat = false;
      render();
      await drainPendingSoftMessageRefresh();
    }
  }

  function applyTextSendResult(result, opts) {
    const conversationName = opts.conversationName;
    const text = opts.text;
    const myGen = opts.myGen;
    const textNonce = opts.textNonce;
    // Never mutate another conversation's draft from a stale response.
    if (myGen !== sendGeneration) return "stale";
    if (!state.selected || state.selected.name !== conversationName) {
      if (result && result.ok) {
        clearDeferredText(conversationName);
        if (sendAttempt.conversation === conversationName) clearSendAttempt();
        if ((state.draft || "").trim() === text) {
          state.draft = "";
        }
      } else {
        stashDeferredText(conversationName, text, textNonce || sendAttempt.nonce);
      }
      return "switched";
    }
    if ((state.draft || "").trim() !== text) return "edited";

    if (result && result.ok) {
      state.draft = "";
      clearSendAttempt();
      clearDeferredText(conversationName);
      state.error = "";
      return "ok";
    }
    if (result && result.uncertain) {
      state.error = result.error || "Delivery has not been confirmed";
    } else {
      state.error = (result && result.error) || "Send failed";
    }
    return "kept";
  }

  async function sendMessage() {
    // Synchronous in-flight guard — second click cannot start another request.
    if (state.sending) return;
    if (!state.selected || state.selected.provider !== "Facebook") return;
    // Attachment selection uses attachment/combined send — never silently attach draft text here.
    const pendingAttachment = currentImageDraft();
    if (pendingAttachment && pendingAttachment.file) return;
    const text = (state.draft || "").trim();
    if (!text) return;

    const conversationName = state.selected.name;
    const clientNonce = nonceForDraft(conversationName, text);
    const myGen = ++sendGeneration;
    state.sending = true;
    state.error = "";
    render();
    try {
      const result = await call("meta_omnichannel.api.inbox.send_message", {
        conversation: conversationName,
        text: text,
        client_nonce: clientNonce,
      });
      const outcome = applyTextSendResult(result, {
        conversationName,
        text,
        myGen,
        textNonce: clientNonce,
      });
      if (outcome === "ok") {
        await refreshAfterSend(conversationName);
        return;
      }
      if (outcome === "kept") render();
    } catch (e) {
      if (myGen !== sendGeneration) return;
      if (!state.selected || state.selected.name !== conversationName) return;
      if ((state.draft || "").trim() !== text) return;
      state.error = e.message || String(e);
      render();
    } finally {
      finishSending();
    }
  }

  async function sendImageMessage() {
    if (state.sending) return;
    if (!state.selected || state.selected.provider !== "Facebook") return;
    const conversationName = state.selected.name;
    const draft = imageDrafts[conversationName];
    if (!draft || !draft.file) return;
    const spec = attachmentSendSpec(draft.kind || "image");
    if (draft.blockResend) {
      state.error = t(spec.uncertain);
      render();
      return;
    }
    // Combined path when composer has both attachment and text.
    const companionText = (state.draft || "").trim();
    if (companionText) {
      return sendCombinedImageAndText();
    }

    const clientNonce = draft.nonce || nonceForImage(conversationName, draft.file);
    draft.nonce = clientNonce;
    const fileKey = draft.fileKey;
    const draftKind = draft.kind || "image";
    const myGen = ++sendGeneration;
    state.sending = true;
    state.error = "";
    render();
    try {
      const result = await callMultipart(
        spec.method,
        {
          conversation: conversationName,
          client_nonce: clientNonce,
        },
        spec.field,
        draft.file
      );
      // Stale: switched conversation or replaced the selection mid-flight.
      if (myGen !== sendGeneration) return;
      if (!state.selected || state.selected.name !== conversationName) return;
      const still = imageDrafts[conversationName];
      if (
        !still ||
        still.fileKey !== fileKey ||
        still.nonce !== clientNonce ||
        (still.kind || "image") !== draftKind
      ) {
        return;
      }

      if (result && result.ok) {
        clearImageDraft(conversationName);
        // Attachment-only: keep any unrelated text draft intact.
        state.error = "";
        await refreshAfterSend(conversationName);
        return;
      }

      if (result && result.uncertain) {
        still.blockResend = true;
        state.error = t(spec.uncertain);
      } else {
        state.error = (result && result.error) || "Send failed";
      }
      render();
    } catch (e) {
      if (myGen !== sendGeneration) return;
      if (!state.selected || state.selected.name !== conversationName) return;
      const still = imageDrafts[conversationName];
      if (!still || still.fileKey !== fileKey || still.nonce !== clientNonce) return;
      state.error = e.message || String(e);
      render();
    } finally {
      finishSending();
    }
  }

  /**
   * Attachment then text as two Messenger messages. Holds state.sending across both.
   * If the user leaves the conversation after attachment Sent, text is deferred (not auto-sent).
   */
  async function sendCombinedImageAndText() {
    if (state.sending) return;
    if (!state.selected || state.selected.provider !== "Facebook") return;
    const conversationName = state.selected.name;
    const draft = imageDrafts[conversationName];
    if (!draft || !draft.file) return;
    const spec = attachmentSendSpec(draft.kind || "image");
    if (draft.blockResend) {
      state.error = t(spec.uncertain);
      render();
      return;
    }
    const text = (state.draft || "").trim();
    if (!text) {
      return sendImageMessage();
    }

    const imageNonce = draft.nonce || nonceForImage(conversationName, draft.file);
    draft.nonce = imageNonce;
    const fileKey = draft.fileKey;
    const draftKind = draft.kind || "image";
    const imageFile = draft.file;
    const textNonce = nonceForDraft(conversationName, text);
    const myGen = ++sendGeneration;
    state.sending = true;
    state.error = "";
    render();

    try {
      const imageResult = await callMultipart(
        spec.method,
        {
          conversation: conversationName,
          client_nonce: imageNonce,
        },
        spec.field,
        imageFile
      );

      const imageRow = imageDrafts[conversationName];
      const imageStillOurs =
        imageRow &&
        imageRow.fileKey === fileKey &&
        imageRow.nonce === imageNonce &&
        (imageRow.kind || "image") === draftKind;

      if (!(imageResult && imageResult.ok)) {
        if (imageStillOurs) {
          if (imageResult && imageResult.uncertain) {
            imageRow.blockResend = true;
          }
        }
        // Keep text draft + text nonce; never start text after Failed/Unknown.
        if (myGen === sendGeneration && state.selected && state.selected.name === conversationName) {
          if (imageResult && imageResult.uncertain) {
            state.error = t(spec.uncertain);
          } else {
            state.error = (imageResult && imageResult.error) || "Send failed";
          }
          render();
        }
        return;
      }

      // Confirmed attachment Sent — clear attachment for this conversation only.
      clearImageDraft(conversationName);

      // Defer text if the user switched away or edited before we start text.
      const stillOnConversation =
        !!state.selected && state.selected.name === conversationName;
      const draftUnchanged = (state.draft || "").trim() === text;
      if (!stillOnConversation || myGen !== sendGeneration || !draftUnchanged) {
        stashDeferredText(conversationName, text, textNonce);
        if (stillOnConversation && myGen === sendGeneration) {
          state.error = t(spec.sentPending);
          render();
        }
        return;
      }

      // Second request uses captured conversation/text/nonce — not live selection for targeting.
      const textResult = await call("meta_omnichannel.api.inbox.send_message", {
        conversation: conversationName,
        text: text,
        client_nonce: textNonce,
      });

      const outcome = applyTextSendResult(textResult, {
        conversationName,
        text,
        myGen,
        textNonce,
      });
      if (outcome === "ok") {
        await refreshAfterSend(conversationName);
        return;
      }
      if (outcome === "kept" || outcome === "edited") {
        if (outcome === "edited" && !state.error) {
          state.error = t(spec.sentPending);
        }
        render();
      }
      // outcome === "switched": attachment already Sent; text handled without touching B.
    } catch (e) {
      // Exception may be from attachment or text phase.
      const imageRow = imageDrafts[conversationName];
      if (imageRow && imageRow.fileKey === fileKey) {
        // Attachment phase likely failed before clear.
        if (myGen === sendGeneration && state.selected && state.selected.name === conversationName) {
          state.error = e.message || String(e);
          render();
        }
        return;
      }
      // Attachment was cleared — treat as text-phase failure; keep text + nonce.
      stashDeferredText(conversationName, text, textNonce);
      if (myGen !== sendGeneration) return;
      if (!state.selected || state.selected.name !== conversationName) return;
      if ((state.draft || "").trim() !== text) return;
      state.error = e.message || String(e);
      render();
    } finally {
      finishSending();
    }
  }

  function sendComposer() {
    if (state.sending) return;
    const attachment = currentImageDraft();
    if (attachment && attachment.file) {
      const companionText = (state.draft || "").trim();
      if (companionText) return sendCombinedImageAndText();
      return sendImageMessage();
    }
    return sendMessage();
  }

  function patchPreservedChatHeader(chatEl) {
    if (!chatEl || !state.selected) return;
    const header = chatEl.querySelector(".oc-chat-header");
    if (!header) return;
    const nextTitle = buildChatHeaderTitle(state.selected);
    const currentTitle = header.querySelector(".oc-chat-title");
    if (currentTitle) currentTitle.replaceWith(nextTitle);
    else header.insertBefore(nextTitle, header.firstChild);
  }

  function patchPreservedCustomerTitle(customerEl) {
    if (!customerEl || !state.selected) return;
    const strong = customerEl.querySelector(".oc-customer-title strong");
    if (strong) strong.textContent = conversationPrimaryTitle(state.selected);
  }

  function render(options) {
    const opts = options || {};
    const chrome = captureUiChrome();
    let reusedChat = null;
    let reusedCustomer = null;
    if (
      opts.preserveChat &&
      state.selected &&
      !state.loadingChat &&
      root.querySelector(".oc-chat .oc-messages")
    ) {
      reusedChat = root.querySelector(".oc-chat");
      reusedCustomer = root.querySelector(".oc-customer");
      if (reusedChat) reusedChat.remove();
      if (reusedCustomer) reusedCustomer.remove();
    }
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
    const listItems = el("div", { className: "oc-list-items" });
    if (state.loadingList) listItems.appendChild(el("div", { className: "oc-empty", text: t("loading") }));
    else if (!state.conversations.length) listItems.appendChild(el("div", { className: "oc-empty", text: t("empty_list") }));
    else {
      state.conversations.forEach((c) => {
        listItems.appendChild(
          el(
            "button",
            {
              type: "button",
              className: "oc-conv" + (state.selected && state.selected.name === c.name ? " active" : ""),
              onclick: () => openConversation(c),
            },
            [
              el("div", { className: "oc-conv-top" }, [
                el("span", { text: conversationPrimaryTitle(c) }),
                c.unread_count ? el("span", { className: "oc-badge", text: String(c.unread_count) }) : null,
              ]),
              el("div", { className: "channel-pill", text: `${c.provider} · ${fmt(c.last_message_at)}` }),
              el("div", { className: "oc-preview", text: c.last_message_preview || "" }),
            ]
          )
        );
      });
    }
    list.appendChild(listItems);

    let chat;
    let customer;
    if (reusedChat) {
      chat = reusedChat;
      patchPreservedChatHeader(chat);
      customer = reusedCustomer || el("aside", { className: "oc-customer" }, [el("h3", { text: t("customer") })]);
      patchPreservedCustomerTitle(customer);
    } else {
      chat = el("section", { className: "oc-chat" });
      chat.appendChild(
        el("header", { className: "oc-chat-header" }, [buildChatHeaderTitle(state.selected)])
      );
      if (state.error) chat.appendChild(el("div", { className: "oc-error", text: state.error }));
      if (!state.selected) chat.appendChild(el("div", { className: "oc-empty", text: t("empty_chat") }));
      else if (state.loadingChat) chat.appendChild(el("div", { className: "oc-empty", text: t("loading") }));
      else {
        const msgs = el("div", { className: "oc-messages" });
        state.messages.forEach((m) => {
          msgs.appendChild(
            el(
              "div",
              { className: "oc-bubble " + (m.direction === "Outbound" ? "outbound" : "inbound") },
              [
                ...renderMessageBody(m),
                el("div", {
                  className: "oc-meta",
                  text: `${fmt(m.sent_at)}${m.direction === "Outbound" ? " · " + m.delivery_status : ""}`,
                }),
              ]
            )
          );
        });
        chat.appendChild(msgs);
      }
      const composer = el("div", { className: "oc-composer" });
      const canCompose = composerCanCompose();
      const canSend = composerCanSend();
      const attachmentDraft = currentImageDraft();
      const hasAttachment = !!(attachmentDraft && attachmentDraft.file);
      const attachmentSpec = hasAttachment
        ? attachmentSendSpec(attachmentDraft.kind || "image")
        : null;

      if (hasAttachment) {
        const preview = el("div", {
          className:
            "oc-composer-preview" +
            (attachmentDraft.kind === "document" ? " is-document" : ""),
        });
        if (attachmentDraft.kind === "document") {
          preview.appendChild(
            el("div", {
              className: "oc-composer-doc-badge",
              text: attachmentDraft.typeLabel || "File",
            })
          );
        } else if (attachmentDraft.previewUrl) {
          preview.appendChild(
            el("img", {
              className: "oc-composer-thumb",
              src: attachmentDraft.previewUrl,
              alt: attachmentDraft.filename || "Image",
            })
          );
        }
        const metaKids = [
          el("div", {
            className: "oc-composer-filename",
            text: attachmentDraft.filename || attachmentSpec.defaultName,
          }),
        ];
        if (attachmentDraft.kind === "document") {
          metaKids.push(
            el("div", {
              className: "oc-composer-filesize",
              text:
                (attachmentDraft.typeLabel || "File") +
                " · " +
                formatAttachmentSize(attachmentDraft.size),
            })
          );
        }
        metaKids.push(
          el("div", {
            className: "oc-composer-hint",
            text: (state.draft || "").trim()
              ? t(attachmentSpec.andTextHint)
              : t(attachmentSpec.onlyHint),
          })
        );
        preview.appendChild(el("div", { className: "oc-composer-preview-meta" }, metaKids));
        preview.appendChild(
          el("button", {
            className: "oc-btn secondary oc-composer-remove",
            type: "button",
            text: t("remove_image"),
            disabled: state.sending,
            onclick: removeComposerImage,
          })
        );
        composer.appendChild(preview);
      }

      const isFacebook = !!(state.selected && state.selected.provider === "Facebook");
      const row = el("div", {
        className: "oc-composer-row" + (isFacebook ? " has-attach" : ""),
      });
      let fileInput = null;
      if (isFacebook) {
        fileInput = el("input", {
          className: "oc-composer-file",
          type: "file",
          accept: COMPOSER_ACCEPT,
          disabled: !canCompose,
          onchange: (e) => {
            const input = e && e.target;
            const file =
              input && input.files && input.files.length ? input.files[0] : null;
            if (file) selectComposerAttachment(file);
            if (input) input.value = "";
          },
        });
        composer.appendChild(fileInput);
        row.appendChild(
          el("button", {
            className: "oc-btn secondary oc-composer-attach",
            type: "button",
            text: "＋",
            title: t("attach_image"),
            "aria-label": t("attach_image"),
            disabled: !canCompose,
            onclick: () => {
              if (!canCompose || !fileInput) return;
              fileInput.click();
            },
          })
        );
      }
      const ta = el("textarea", {
        rows: "3",
        placeholder: t("composer_placeholder"),
        value: state.draft,
        disabled: !canCompose,
        oninput: (e) => {
          onDraftInput(e.target.value);
        },
      });
      row.appendChild(ta);
      row.appendChild(
        el("button", {
          className: "oc-btn oc-composer-send",
          type: "button",
          text: composerSendButtonLabel(attachmentDraft),
          disabled: !canSend,
          onclick: sendComposer,
        })
      );
      composer.appendChild(row);
      chat.appendChild(composer);
      if (state.selected && state.selected.provider !== "Facebook") {
        chat.appendChild(el("div", { className: "oc-empty", text: t("pending_channel") }));
      }

      customer = el("aside", { className: "oc-customer" }, [el("h3", { text: t("customer") })]);
      if (!state.selected) customer.appendChild(el("div", { className: "oc-empty", text: t("empty_chat") }));
      else {
        customer.appendChild(
          el("div", { className: "oc-customer-title" }, [
            el("strong", { text: conversationPrimaryTitle(state.selected) }),
          ])
        );
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
    }

    shell.appendChild(side);
    shell.appendChild(list);
    shell.appendChild(chat);
    shell.appendChild(customer);
    root.appendChild(shell);
    restoreUiChrome(chrome, { preserveScrollExact: !!reusedChat });
  }

  async function flushRealtimeRefresh() {
    realtimeDebounceTimer = null;
    const hints = Array.from(pendingRealtimeConversations);
    pendingRealtimeConversations.clear();
    const doReconnect = realtimeReconnectBound;
    realtimeReconnectBound = false;
    const selectedName = state.selected && state.selected.name;
    await softLoadList();
    const refreshMessages =
      doReconnect || (selectedName && hints.indexOf(selectedName) !== -1);
    if (
      refreshMessages &&
      state.selected &&
      state.selected.name &&
      (!selectedName || state.selected.name === selectedName)
    ) {
      await softRefreshMessages(state.selected.name);
    }
  }

  function scheduleRealtimeRefresh(conversationName, options) {
    const opts = options || {};
    if (conversationName) pendingRealtimeConversations.add(conversationName);
    if (opts.reconnect) realtimeReconnectBound = true;
    if (realtimeDebounceTimer) clearTimeout(realtimeDebounceTimer);
    realtimeDebounceTimer = setTimeout(flushRealtimeRefresh, REALTIME_DEBOUNCE_MS);
  }

  function onConversationUpdated(payload) {
    const name = validateConversationHint(payload);
    if (!name) return;
    scheduleRealtimeRefresh(name);
  }

  /**
   * Frappe v15 website uses lazy_connect: frappe.realtime.on() is a no-op until
   * realtime.socket exists, and connect() is what clears lazy_connect.
   * Do not call socket.connect() directly — use frappe.realtime.connect().
   */
  function ensureRealtimeConnected() {
    try {
      const rt = window.frappe && window.frappe.realtime;
      if (rt && typeof rt.connect === "function") rt.connect();
    } catch (_) {}
  }

  function onRealtimeSocketConnect() {
    // One soft refresh after reconnect — coalesced; no polling loop.
    scheduleRealtimeRefresh(state.selected && state.selected.name, { reconnect: true });
  }

  function bindRealtimeConnectHandler(rt) {
    const socket = rt && rt.socket;
    if (!socket || typeof socket.on !== "function") return;
    // Socket-aware: Frappe normally keeps one socket, but if it is replaced, rebind.
    if (realtimeBoundSocket === socket) return;
    if (realtimeBoundSocket && typeof realtimeBoundSocket.off === "function") {
      try {
        realtimeBoundSocket.off("connect", onRealtimeSocketConnect);
      } catch (_) {}
    }
    realtimeBoundSocket = socket;
    socket.on("connect", onRealtimeSocketConnect);
  }

  function scheduleRealtimeSubscribeRetry() {
    if (realtimeSubscribeRetryTimer) return;
    if (realtimeSubscribed && realtimeEventSocket) {
      const rt = window.frappe && window.frappe.realtime;
      if (rt && rt.socket && rt.socket === realtimeEventSocket) return;
    }
    if (realtimeSubscribeAttempts >= REALTIME_SUBSCRIBE_MAX_ATTEMPTS) return;
    realtimeSubscribeAttempts += 1;
    realtimeSubscribeRetryTimer = setTimeout(function () {
      realtimeSubscribeRetryTimer = null;
      subscribeRealtimeOnce();
    }, REALTIME_SUBSCRIBE_RETRY_MS);
  }

  function clearRealtimeSubscribeRetry() {
    if (!realtimeSubscribeRetryTimer) return;
    clearTimeout(realtimeSubscribeRetryTimer);
    realtimeSubscribeRetryTimer = null;
  }

  function subscribeRealtimeOnce() {
    const rt = window.frappe && window.frappe.realtime;
    // Frappe RealTimeClient.on() silently no-ops when socket is missing.
    if (!rt || typeof rt.on !== "function" || !rt.socket) {
      scheduleRealtimeSubscribeRetry();
      return false;
    }
    // Already registered on the active socket — do not add duplicate listeners.
    if (realtimeSubscribed && realtimeEventSocket === rt.socket) return false;

    try {
      // Drop handlers from a replaced socket instance before binding the new one.
      if (realtimeEventSocket && realtimeEventSocket !== rt.socket && typeof realtimeEventSocket.off === "function") {
        try {
          realtimeEventSocket.off(REALTIME_EVENT, onConversationUpdated);
        } catch (_) {}
      }
      // Bind reconnect handler before connect so the first connect is observed.
      bindRealtimeConnectHandler(rt);
      // Must run after socket exists and before marking subscribed.
      rt.on(REALTIME_EVENT, onConversationUpdated);
    } catch (_) {
      // Registration failed — do not claim subscribed; keep bounded retries alive.
      scheduleRealtimeSubscribeRetry();
      return false;
    }

    ensureRealtimeConnected();
    realtimeEventSocket = rt.socket;
    realtimeSubscribed = true;
    clearRealtimeSubscribeRetry();
    return true;
  }

  // Diagnostics surface: read-only introspection + real function refs for tests.
  // Test-only state mutators are injected by the Node harness, not shipped here.
  window.__omnichannelRealtime = {
    eventName: REALTIME_EVENT,
    debounceMs: REALTIME_DEBOUNCE_MS,
    validateConversationHint: validateConversationHint,
    isPermissionError: isPermissionError,
    scheduleRealtimeRefresh: scheduleRealtimeRefresh,
    onConversationUpdated: onConversationUpdated,
    subscribeRealtimeOnce: subscribeRealtimeOnce,
    softRefreshMessages: softRefreshMessages,
    softLoadList: softLoadList,
    refreshAfterSend: refreshAfterSend,
    openConversation: openConversation,
    sendMessage: sendMessage,
    sendImageMessage: sendImageMessage,
    sendCombinedImageAndText: sendCombinedImageAndText,
    sendComposer: sendComposer,
    selectComposerImage: selectComposerImage,
    selectComposerAttachment: selectComposerAttachment,
    removeComposerImage: removeComposerImage,
    getDeferredText: function (conversationName) {
      const pending = deferredTextByConversation[conversationName || ""];
      return pending
        ? { text: pending.text, nonce: pending.nonce || null }
        : null;
    },
    drainPendingSoftMessageRefresh: drainPendingSoftMessageRefresh,
    getPendingSoftMessageRefresh: function () {
      return pendingSoftMessageRefresh;
    },
    getOpenConversationSeq: function () {
      return openConversationSeq;
    },
    getSendAttemptNonce: function () {
      return sendAttempt.nonce;
    },
    getImageAttemptNonce: function () {
      const draft = currentImageDraft();
      return draft ? draft.nonce : null;
    },
    getImageDraft: function () {
      const draft = currentImageDraft();
      if (!draft) return null;
      return {
        conversation: draft.conversation,
        kind: draft.kind || "image",
        filename: draft.filename,
        mime: draft.mime,
        typeLabel: draft.typeLabel || "",
        size: draft.size,
        fileKey: draft.fileKey,
        nonce: draft.nonce,
        blockResend: !!draft.blockResend,
        hasFile: !!draft.file,
        previewUrl: draft.previewUrl || null,
      };
    },
    getState: function () {
      return {
        selected: state.selected && state.selected.name,
        loadingChat: state.loadingChat,
        sending: state.sending,
        draft: state.draft,
        note: state.note,
        search: state.search,
        filter: state.filter,
        error: state.error,
        messageCount: state.messages.length,
        conversationCount: state.conversations.length,
        panel: state.panel,
        hasImageDraft: !!(currentImageDraft() && currentImageDraft().file),
        canCompose: composerCanCompose(),
        canSend: composerCanSend(),
      };
    },
    syncComposerControls: syncComposerControls,
    isSubscribed: function () {
      return realtimeSubscribed;
    },
    isConnectHandlerBound: function () {
      const rt = window.frappe && window.frappe.realtime;
      return !!(realtimeBoundSocket && rt && rt.socket === realtimeBoundSocket);
    },
    getBoundEventSocket: function () {
      return realtimeEventSocket;
    },
    getBoundConnectSocket: function () {
      return realtimeBoundSocket;
    },
    pendingCount: function () {
      return pendingRealtimeConversations.size;
    },
    isSoftListRunning: function () {
      return softListRunning;
    },
  };

  render();
  loadList();
  subscribeRealtimeOnce();
})();
