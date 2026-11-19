"use client";

import { FormEvent, useState } from "react";

import { brand } from "@/config/brand";
import { ChatError } from "@/lib/chat";
import { ClientRef, signIn, StaffUser } from "@/lib/team";

const c = brand.colors;

/** Staff sign-in, shared by the team assistant (/team) and the admin dashboard (/admin). */
type Props = {
  onDone: (token: string, user: StaffUser, clients: ClientRef[]) => void;
  title?: string;
  note?: string;
};

export default function StaffSignIn({
  onDone,
  title = `${brand.firmName} team assistant`,
  note = "For consultants only. Answers come from internal documents and the clients you are assigned to.",
}: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await signIn(email.trim(), password);
      onDone(r.token, r.user, r.clients);
    } catch (err) {
      setError(err instanceof ChatError ? err.message : "Sign-in failed.");
    } finally {
      setBusy(false);
      setPassword("");
    }
  };

  return (
    <main className="flex min-h-dvh items-center justify-center px-4" style={{ background: c.soft }}>
      <form onSubmit={submit} className="w-full max-w-sm rounded-2xl border bg-white p-6 shadow-sm"
            style={{ borderColor: c.line, color: c.ink }}>
        <h1 className="text-lg font-semibold">{title}</h1>
        <p className="mt-1 text-sm" style={{ color: c.muted }}>
          {note}
        </p>
        <label className="mt-5 block text-sm font-medium" htmlFor="email">Work email</label>
        <input id="email" type="email" autoComplete="username" required value={email}
               onChange={(e) => setEmail(e.target.value)}
               className="mt-1 w-full rounded-lg border px-3 py-2 outline-none focus:ring-2"
               style={{ borderColor: c.line, ["--tw-ring-color" as string]: c.accent }} />
        <label className="mt-3 block text-sm font-medium" htmlFor="password">Password</label>
        <input id="password" type="password" autoComplete="current-password" required value={password}
               onChange={(e) => setPassword(e.target.value)}
               className="mt-1 w-full rounded-lg border px-3 py-2 outline-none focus:ring-2"
               style={{ borderColor: c.line, ["--tw-ring-color" as string]: c.accent }} />
        {error && <p className="mt-3 text-sm" role="alert" style={{ color: c.alert }}>{error}</p>}
        <button type="submit" disabled={busy}
                className="mt-5 h-10 w-full rounded-lg text-sm font-semibold disabled:opacity-50 focus-visible:outline focus-visible:outline-2"
                style={{ background: c.accent, color: c.accentInk }}>
          {busy ? "Signing in" : "Sign in"}
        </button>
      </form>
    </main>
  );
}

