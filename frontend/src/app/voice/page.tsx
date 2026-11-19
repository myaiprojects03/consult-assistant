import type { Metadata } from "next";
import Link from "next/link";

import VoiceCallPanel from "@/components/VoiceCallPanel";
import { brand } from "@/config/brand";

export const metadata: Metadata = {
  title: `Call ${brand.firmName}`,
  description: "Talk to our assistant by voice, from your browser.",
};

// Browser voice call: the same assistant as the phone line and the chat, spoken.
export default function VoicePage() {
  const c = brand.colors;
  return (
    <main className="min-h-dvh" style={{ color: c.ink }}>
      <header className="mx-auto flex max-w-5xl items-center justify-between px-6 py-6">
        <Link href="/" className="text-lg font-bold tracking-tight">{brand.firmName}</Link>
        <a href={brand.privacyUrl} className="text-sm" style={{ color: c.muted }}>Privacy</a>
      </header>
      <div className="mx-auto max-w-xl px-6 pb-16 pt-6">
        <h1 className="text-3xl font-bold leading-tight">Talk to Ava</h1>
        <p className="mt-3 leading-relaxed" style={{ color: c.muted }}>
          Ask about our services, book a free discovery call, or check on your project. Speak naturally and
          interrupt whenever you like. Ask for a person at any time.
        </p>
        <div className="mt-12">
          <VoiceCallPanel />
        </div>
        <p className="mt-12 text-xs leading-relaxed" style={{ color: c.muted }}>
          Your speech is turned into text by your browser and the conversation is stored to help you and
          arrange calls. See our <a href={brand.privacyUrl} className="underline">privacy notice</a>.
        </p>
      </div>
    </main>
  );
}
