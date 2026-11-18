function boot() {
  return window.__OMNICHANNEL_BOOT__ || {};
}

function csrf() {
  return boot().csrf_token || (window.csrf_token || "");
}

export async function call(method, args = {}) {
  const body = new URLSearchParams();
  Object.entries(args).forEach(([key, value]) => {
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
      if (msgs.length) {
        message = JSON.parse(msgs[0]).message || message;
      }
    } catch (_) {
      /* ignore */
    }
    throw new Error(message);
  }
  return data.message;
}

export function newNonce() {
  if (crypto?.randomUUID) return crypto.randomUUID();
  return `n-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
