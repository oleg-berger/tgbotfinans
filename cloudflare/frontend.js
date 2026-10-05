// Keep public request handling small enough for Workers Free's CPU limit.
const MAX_BODY = 20 * 1024 * 1024;
const GUIDE = new Set(Array.from({length: 7}, (_, index) => `/guide/page-${String(index + 1).padStart(2, "0")}.png`));

function authorized(actual, expected) {
  if (!expected || !actual || actual.length !== expected.length) return false;
  let difference = 0;
  for (let index = 0; index < expected.length; index++) {
    difference |= actual.charCodeAt(index) ^ expected.charCodeAt(index);
  }
  return difference === 0;
}

function store(env) {
  return env.FINANCE.get(env.FINANCE.idFromName("finance-v1"));
}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path === "/health" && request.method === "GET") {
      return Response.json(env.TEST_MODE === "local" ? {status: "ok", test_mode: true} : {status: "ok"});
    }
    if (GUIDE.has(path) && request.method === "GET") return env.ASSETS.fetch(request);
    if (path === "/webhook" && request.method === "POST") {
      if (!authorized(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), env.WEBHOOK_SECRET)) {
        return new Response("Forbidden", {status: 403});
      }
    } else if (path.startsWith("/admin/") && request.method === "POST") {
      if (!authorized(request.headers.get("Authorization"), env.ADMIN_SECRET ? `Bearer ${env.ADMIN_SECRET}` : "")) {
        return new Response("Forbidden", {status: 403});
      }
    } else {
      return new Response("Not found", {status: 404});
    }
    const length = request.headers.get("Content-Length");
    if (length && (!/^\d+$/.test(length) || Number(length) > MAX_BODY)) {
      return new Response("Too large", {status: 413});
    }
    try {
      return await store(env).fetch(request);
    } catch {
      console.error("Finance store request failed");
      return new Response("Please retry", {status: 503});
    }
  },
  async scheduled(controller, env) {
    const response = await store(env).fetch(new Request("https://internal/maintenance", {method: "POST"}));
    if (response.status !== 200) throw new Error("Maintenance failed");
  }
};
