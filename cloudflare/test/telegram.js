// Internal local-only binding. It never sends requests to Telegram.
export default {
  async fetch(request) {
    const method = new URL(request.url).pathname.split("/").pop();
    if (["sendPhoto", "sendDocument", "editMessageMedia"].includes(method)) {
      const form = await request.formData();
      const file = form.get(method === "sendDocument" ? "document" : "photo");
      if (!file || file.size === 0 || !form.get("chat_id")) {
        return Response.json({ok: false, error_code: 400, description: "Invalid upload"});
      }
    } else {
      const body = await request.json();
      if (!body.chat_id && method !== "answerCallbackQuery") {
        return Response.json({ok: false, error_code: 400, description: "Missing chat"});
      }
    }
    return Response.json({ok: true, result: {message_id: 1}});
  }
};
