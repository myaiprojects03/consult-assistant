// Client for the private team API (/api/team): sign in, threads, and streamed answers with sources.
// The session token is kept for this browser tab only (sessionStorage) and sent as a Bearer header.

import { API_URL, ChatError, newMessageId } from "@/lib/chat";

const TOKEN_KEY = "consult-team-token";

export type StaffUser = { name: string; email: string; role: "admin" | "consultant" };
export type ClientRef = { id: string; slug: string; name: string; tier: string };
export type Source = { n: number; title: string; kind: string; client: string; path: string };
export type Thread = { id: string; title: string; updated_at: string };
export type TeamTurn = { role: "user" | "assistant"; text: string; sources?: Source[] };
export type TeamEvent =
  | { type: "delta"; text: string }
  | { type: "tool"; name: string }
  | { type: "sources"; sources: Source[] }
  | { type: "done"; message_id: number; replayed?: boolean }
  | { type: "error"; message: string };

export class SignedOut extends ChatError {}

export function savedToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

function keep(token: string) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage blocked: signed in for this page view only */
  }
}

async function request(path: string, token: string, init: RequestInit = {}): Promise<Response> {
  return staffRequest(`/api/team${path}`, token, init);
}

/** A signed-in staff request (team assistant or admin dashboard). A 401 signs the browser out. */
export async function staffRequest(path: string, token: string, init: RequestInit = {}): Promise<Response> {
  let r: Response;
  try {
    r = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}),
                 ...init.headers },
    });
  } catch {
    throw new ChatError("Can't reach the server. Check your connection and try again.");
  }
  if (r.status === 401 && !path.endsWith("/login")) {
    keep("");
    throw new SignedOut("Your session has ended. Please sign in again.", 401, false);
  }
  if (!r.ok) {
    let detail = "Something went wrong.";
    try {
      const body = await r.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ChatError(detail, r.status, r.status >= 500 || r.status === 429 || r.status === 409);
  }
  return r;
}

export async function signIn(email: string, password: string) {
  const r = await request("/login", "", { method: "POST", body: JSON.stringify({ email, password }) });
  const d = await r.json();
  keep(d.token);
  return { token: d.token as string, user: d.user as StaffUser, clients: d.clients as ClientRef[] };
}

export async function signOut(token: string) {
  try {
    await request("/logout", token, { method: "POST" });
  } finally {
    keep("");
  }
}

export async function me(token: string) {
  const d = await (await request("/me", token)).json();
  return { user: d.user as StaffUser, clients: d.clients as ClientRef[] };
}

export async function listThreads(token: string): Promise<Thread[]> {
  return (await (await request("/threads", token)).json()).threads;
}

export async function newThread(token: string): Promise<string> {
  return (await (await request("/threads", token, { method: "POST" })).json()).id;
}

export async function threadMessages(token: string, id: string): Promise<TeamTurn[]> {
  return (await (await request(`/threads/${id}/messages`, token)).json()).messages;
}

export { newMessageId };

/** Sends one question; onEvent gets each streamed event. Re-sending the same messageId is safe. */
export async function ask(token: string, thread: string, messageId: string, text: string, focusClient: string,
                          onEvent: (e: TeamEvent) => void): Promise<void> {
  for (let attempt = 0; ; attempt++) {
    let r: Response;
    try {
      r = await request(`/threads/${thread}/messages`, token, {
        method: "POST", body: JSON.stringify({ client_message_id: messageId, text, focus_client: focusClient }),
      });
    } catch (e) {
      if (e instanceof ChatError && e.status === 409 && attempt < 10) {
        await new Promise((res) => setTimeout(res, 2000));
        continue;
      }
      throw e;
    }
    if (!r.body) throw new ChatError("The connection dropped while answering.");
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
          const event = JSON.parse(line.slice(5)) as TeamEvent;
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
