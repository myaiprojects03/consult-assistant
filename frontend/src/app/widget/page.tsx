"use client";

import { useEffect, useState } from "react";

import ChatWidget from "@/components/ChatWidget";

// The chat as it runs inside the iframe that embed.js adds to the firm's website.
// ?page= is the page the visitor was on (stored with the conversation); ?embedded=1 shows a close button.
export default function WidgetPage() {
  const [params, setParams] = useState<{ page: string; embedded: boolean } | null>(null);

  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    setParams({ page: q.get("page") ?? document.referrer ?? "", embedded: q.get("embedded") === "1" });
  }, []);

  if (!params) return null;
  return (
    <main className="h-dvh">
      <ChatWidget pageUrl={params.page} embedded={params.embedded} />
    </main>
  );
}
