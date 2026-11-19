// Client for the chat API: start or restore a conversation, send a message and read the streamed reply.

export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");
const STORAGE_KEY = "consult-chat-session";

export type Session = { conversationId: string; token: string; greeting: string; assistantName: string };
export type Turn = { role: "user" | "assistant"; text: string };
export type StreamEvent =
  | { type: "delta"; text: string }
  | { type: "tool"; name: string }
  | { type: "handover"; reason: string }
  | { type: "done"; message_id: number; replayed?: boolean }
  | { type: "error"; message: string };

export class ChatError extends Error {
  constructor(message: string, public status = 0, public retryable = true) {
    super(message);
  }
}

function save(s: Session) {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    /* storage blocked (private mode, third-party iframe): the chat still works for this page view */
  }
}

function load(): Session | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function forgetSession() {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

async function errorText(r: Response): Promise<string> {
  try {
    const body = await r.json();
    return typeof body.detail === "string" ? body.detail : "Something went wrong.";
  } catch {
    return "Something went wrong.";
  }
}

/** The visitor's time zone (e.g. "Europe/London"), so call times are offered in their local time. */
function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone ?? "";
  } catch {
    return "";
  }
}

export async function startSession(pageUrl: string): Promise<Session> {
  let r: Response;
  try {
    r = await fetch(`${API_URL}/api/chat/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page_url: pageUrl.slice(0, 500), timezone: browserTimeZone() }),
    });
  } catch {
    throw new ChatError("Can't reach the chat service. Check your connection and try again.");
  }
  if (!r.ok) throw new ChatError(await errorText(r), r.status, r.status >= 500 || r.status === 429);
  const d = await r.json();
  const s = { conversationId: d.conversation_id, token: d.token, greeting: d.greeting, assistantName: d.assistant_name };
  save(s);
  return s;
}

/** The saved conversation and its visible messages, or null if there is none (or it has expired). */
export async function restoreSession(): Promise<{ session: Session; turns: Turn[] } | null> {
  const s = load();
  if (!s) return null;
  try {
    const r = await fetch(`${API_URL}/api/chat/sessions/${s.conversationId}/messages`, {
      headers: { "X-Chat-Token": s.token },
    });
    if (!r.ok) {
      forgetSession();
      return null;
    }
    const d = await r.json();
    return { session: s, turns: d.messages as Turn[] };
  } catch {
    return { session: s, turns: [] };
  }
}

export function newMessageId(): string {
  return crypto.randomUUID().replace(/-/g, "");
}

/** Sends one message and calls onEvent for each streamed event. Resolves when the reply is done.
 *  Throws ChatError when it fails; sending again with the same messageId is safe (answered once). */
export async function sendMessage(s: Session, messageId: string, text: string,
                                  onEvent: (e: StreamEvent) => void): Promise<void> {
  for (let attempt = 0; ; attempt++) {
    let r: Response;
    try {
      r = await fetch(`${API_URL}/api/chat/sessions/${s.conversationId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Chat-Token": s.token },
        body: JSON.stringify({ client_message_id: messageId, text }),
      });
    } catch {
      throw new ChatError("Message not sent: no connection.");
    }
    // 409: the server is still answering this same message (e.g. after a quick retry). Wait, then ask again.
    if (r.status === 409 && attempt < 5) {
      await new Promise((res) => setTimeout(res, 2000));
      continue;
    }
    if (!r.ok || !r.body) {
      throw new ChatError(await errorText(r), r.status, r.status >= 500 || r.status === 429 || r.status === 409);
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finished = false;
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let cut;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          const line = buffer.slice(0, cut).trim();
          buffer = buffer.slice(cut + 2);
          if (!line.startsWith("data:")) continue;
          const event = JSON.parse(line.slice(5)) as StreamEvent;
          if (event.type === "error") throw new ChatError(event.message);
          if (event.type === "done") finished = true;
          onEvent(event);
        }
      }
    } catch (e) {
      if (e instanceof ChatError) throw e;
      throw new ChatError("The connection dropped while answering.");
    }
    if (!finished) throw new ChatError("The connection dropped while answering.");
    return;
  }
}
