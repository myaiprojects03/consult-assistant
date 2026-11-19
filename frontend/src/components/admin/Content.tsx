"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { brand } from "@/config/brand";
import { ChatError } from "@/lib/chat";
import { adminGet, adminSend, ContentFile, ContentItem, Entry, when } from "@/lib/admin";

const c = brand.colors;

const NEW_DOC = `---
title: Title of the document
kind: sop          # proposal, case_study, sop, template, technical, notes, policy
# client: northwind   (only for a document about one client)
---
## First section
Write the content here. Sections starting with "## " are cited separately.
`;

const FORM_WORDS: Record<string, [string, string, string]> = {     // title label, body label, add button
  "faqs.yaml": ["Question", "Answer", "Add a question"],
  "services.yaml": ["Service", "Description", "Add a service"],
  "company.yaml": ["Topic", "Text", "Add a topic"],
  "policies.yaml": ["Policy", "Text", "Add a policy"],
};

function EntryForm({ path, entries, setEntries }: { path: string; entries: Entry[]; setEntries: (e: Entry[]) => void }) {
  const [t, b, add] = FORM_WORDS[path];
  const update = (i: number, patch: Partial<Entry>) => setEntries(entries.map((e, j) => (j === i ? { ...e, ...patch } : e)));
  const move = (i: number, d: number) => {
    const next = [...entries];
    [next[i], next[i + d]] = [next[i + d], next[i]];
    setEntries(next);
  };
  return (
    <div className="space-y-3">
      {entries.map((e, i) => (
        <fieldset key={e.slug || `new-${i}`} className="rounded-xl border p-3" style={{ borderColor: c.line }}>
          <legend className="sr-only">{`${t} ${i + 1}`}</legend>
          <label className="block text-xs font-medium" style={{ color: c.muted }} htmlFor={`t-${i}`}>{t}</label>
          <input id={`t-${i}`} value={e.title} onChange={(ev) => update(i, { title: ev.target.value })} maxLength={200}
                 className="mt-1 w-full rounded-lg border px-3 py-1.5 text-sm font-medium" style={{ borderColor: c.line }} />
          <label className="mt-2 block text-xs font-medium" style={{ color: c.muted }} htmlFor={`b-${i}`}>{b}</label>
          <textarea id={`b-${i}`} value={e.body} onChange={(ev) => update(i, { body: ev.target.value })} rows={3}
                    className="mt-1 w-full resize-y rounded-lg border px-3 py-2 text-sm leading-relaxed" style={{ borderColor: c.line }} />
          <div className="mt-1 flex gap-3 text-xs" style={{ color: c.muted }}>
            <button type="button" disabled={i === 0} onClick={() => move(i, -1)} className="underline disabled:no-underline disabled:opacity-40">Move up</button>
            <button type="button" disabled={i === entries.length - 1} onClick={() => move(i, 1)} className="underline disabled:no-underline disabled:opacity-40">Move down</button>
            <button type="button" onClick={() => setEntries(entries.filter((_, j) => j !== i))} className="underline" style={{ color: c.alert }}>Remove</button>
          </div>
        </fieldset>
      ))}
      <button type="button" onClick={() => setEntries([...entries, { title: "", body: "" }])}
              className="rounded-lg border px-3 py-1.5 text-sm" style={{ borderColor: c.line }}>{add}</button>
    </div>
  );
}

export default function Content({ token, onError }: { token: string; onError: (e: unknown) => void }) {
  const [items, setItems] = useState<ContentItem[] | null>(null);
  const [path, setPath] = useState("faqs.yaml");
  const [file, setFile] = useState<ContentFile | null>(null);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [newName, setNewName] = useState("");
  const template = useRef("");          // a new internal document starts from NEW_DOC
  const [entries, setEntries] = useState<Entry[] | null>(null);    // list files: edited as a form
  const [asText, setAsText] = useState(false);

  const loadList = useCallback(() => {
    adminGet<{ items: ContentItem[] }>(token, "/content").then((r) => setItems(r.items)).catch(onError);
  }, [token, onError]);
  const loadFile = useCallback((p: string) => {
    setFile(null);
    adminGet<ContentFile>(token, `/content/${p}`).then((f) => {
      setFile(f);
      setEntries(f.entries ? f.entries.map((e) => ({ ...e })) : null);
      setText(f.text || template.current);
      template.current = "";
      setNote("");
    }).catch(onError);
  }, [token, onError]);
  useEffect(() => { loadList(); }, [loadList]);
  useEffect(() => { setMessage(null); setAsText(false); loadFile(path); }, [path, loadFile]);

  const formMode = Boolean(entries && FORM_WORDS[path] && !asText);
  const changed = file !== null && (formMode ? JSON.stringify(entries) !== JSON.stringify(file.entries)
                                             : text !== file.text);
  const act = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true);
    setMessage(null);
    try {
      await fn();
      setMessage({ ok: true, text: done });
      loadList();
      loadFile(path);
    } catch (e) {
      if (e instanceof ChatError) setMessage({ ok: false, text: e.message });
      else onError(e);
    } finally {
      setBusy(false);
    }
  };
  const save = () => act(() => formMode
    ? adminSend(token, `/content-form/${path}`, "PUT", { entries, base_version: file?.version ?? 0, note })
    : adminSend(token, `/content/${path}`, "PUT", { text, base_version: file?.version ?? 0, note }),
  "Saved. The assistant uses it from now on.");
  const reset = () => {
    if (!window.confirm("Go back to the original version of this file? Your edits stay in the history.")) return;
    void act(() => adminSend(token, `/content/${path}/reset`, "POST", { base_version: file?.version ?? 0 }),
      "Back to the original version.");
  };
  const remove = () => {
    if (!window.confirm("Remove this document? It stays in the history.")) return;
    void act(() => adminSend(token, `/content/${path}?base_version=${file?.version ?? 0}`, "DELETE"), "Removed.")
      .then(() => setPath("faqs.yaml"));
  };
  const viewVersion = async (v: number) => {
    try {
      const old = await adminGet<{ text: string }>(token, `/content/${path}?version=${v}`);
      setText(old.text);
      setAsText(true);
      setNote(`restore version ${v}`);
      setMessage({ ok: true, text: `Version ${v} is in the editor. Save to make it current again.` });
    } catch (e) {
      onError(e);
    }
  };
  const createDoc = () => {
    const slug = newName.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    if (!slug) return;
    template.current = NEW_DOC;
    setNewName("");
    setPath(`internal/${slug}.md`);
  };

  const groups: [string, ContentItem[]][] = [
    ["What the assistant says and does", (items ?? []).filter((i) => i.group === "assistant")],
    ["Internal documents (team assistant)", (items ?? []).filter((i) => i.group === "internal")],
  ];
  const isInternal = path.startsWith("internal/");

  return (
    <div className="grid gap-4 lg:grid-cols-[16rem_minmax(0,1fr)]">
      <nav aria-label="Files" className="space-y-5">
        {groups.map(([title, list]) => (
          <div key={title}>
            <h2 className="mb-1 text-xs font-semibold" style={{ color: c.muted }}>{title}</h2>
            <ul>
              {list.map((i) => (
                <li key={i.path}>
                  <button onClick={() => setPath(i.path)} aria-current={i.path === path ? "page" : undefined}
                          className="w-full rounded-md px-2 py-1.5 text-left text-sm hover:bg-[#F6FAF9]"
                          style={i.path === path ? { background: c.soft, fontWeight: 600 } : undefined}>
                    {i.title}{i.edited && <span className="ml-1 text-xs font-normal" style={{ color: c.muted }}>(edited)</span>}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
        <form onSubmit={(e) => { e.preventDefault(); createDoc(); }} className="flex gap-2">
          <input aria-label="New document name" value={newName} onChange={(e) => setNewName(e.target.value)}
                 placeholder="New document name" className="min-w-0 flex-1 rounded-lg border px-2 py-1.5 text-sm"
                 style={{ borderColor: c.line }} />
          <button className="rounded-lg border px-2 text-sm" style={{ borderColor: c.line }}>Add</button>
        </form>
      </nav>

      <section className="min-w-0">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h2 className="text-base font-semibold">{items?.find((i) => i.path === path)?.title ?? path}</h2>
            <p className="text-sm" style={{ color: c.muted }}>
              {items?.find((i) => i.path === path)?.help}{" "}
              {file && (file.edited ? `Version ${file.version}, edited.` : "The original version.")}
            </p>
          </div>
        </div>
        {entries && FORM_WORDS[path] && (
          <p className="mb-2 text-sm">
            <button onClick={() => { setAsText(!asText); if (file) { setText(file.text); setEntries(file.entries ? file.entries.map((e) => ({ ...e })) : null); } }}
                    className="underline" style={{ color: c.accent }}>
              {asText ? "Edit as a form" : "Edit as text (advanced)"}
            </button>
          </p>
        )}
        {formMode && entries ? <EntryForm path={path} entries={entries} setEntries={setEntries} /> : (
          <textarea aria-label={`Contents of ${path}`} value={text} onChange={(e) => setText(e.target.value)}
                    spellCheck={false} disabled={!file}
                    className="h-[55vh] w-full resize-y rounded-xl border p-3 font-mono text-[13px] leading-relaxed outline-none focus:ring-2"
                    style={{ borderColor: c.line, ["--tw-ring-color" as string]: c.accent }} />
        )}
        <div className="sticky bottom-0 z-10 -mx-1 mt-3 flex flex-wrap items-center gap-2 border-t bg-white px-1 py-3"
             style={{ borderColor: c.line }}>
          <input aria-label="What changed" value={note} onChange={(e) => setNote(e.target.value)} maxLength={200}
                 placeholder="What changed (optional)" className="min-w-0 flex-1 rounded-lg border px-3 py-1.5 text-sm"
                 style={{ borderColor: c.line }} />
          <button onClick={() => void save()} disabled={!changed || busy}
                  className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-40"
                  style={{ background: c.accent, color: c.accentInk }}>{busy ? "Checking" : "Save"}</button>
          {file?.edited && !isInternal && (
            <button onClick={reset} disabled={busy} className="rounded-lg border px-3 py-1.5 text-sm" style={{ borderColor: c.line }}>
              Back to original</button>
          )}
          {isInternal && file?.edited && (
            <button onClick={remove} disabled={busy} className="rounded-lg border px-3 py-1.5 text-sm" style={{ borderColor: c.line, color: c.alert }}>
              Remove document</button>
          )}
          {changed && <button onClick={() => { if (file) { setText(file.text); setEntries(file.entries ? file.entries.map((e) => ({ ...e })) : null); } }}
                              className="text-sm underline" style={{ color: c.muted }}>Discard changes</button>}
        </div>
        {message && (
          <p role="status" className="mt-2 whitespace-pre-wrap rounded-lg p-3 text-sm"
             style={{ background: message.ok ? c.soft : "#FFF4ED", color: message.ok ? c.ink : c.alert }}>{message.text}</p>
        )}
        {file && file.history.length > 0 && (
          <details className="mt-4 text-sm">
            <summary className="cursor-pointer font-medium">History ({file.history.length})</summary>
            <ul className="mt-2 space-y-1">
              {file.history.map((h) => (
                <li key={h.version} className="flex flex-wrap items-baseline gap-2">
                  <span className="tabular-nums">Version {h.version}</span>
                  <span style={{ color: c.muted }}>{when(h.created_at)}{h.note ? `: ${h.note}` : ""}</span>
                  {!h.deleted && h.note !== "back to the default file" && (
                    <button onClick={() => void viewVersion(h.version)} className="underline" style={{ color: c.accent }}>Open</button>
                  )}
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>
    </div>
  );
}
