import { brand } from "@/config/brand";

// Starting privacy notice for the chat and calls. Before go-live the firm replaces it with its reviewed policy
// (docs/DEPLOY.md, go-live checklist).
export default function Privacy() {
  return (
    <main className="mx-auto max-w-2xl px-6 py-14 leading-relaxed" style={{ color: brand.colors.ink }}>
      <h1 className="text-3xl font-bold">How the chat uses your information</h1>
      <div className="mt-8 space-y-4">
        <p>The assistant on this website stores your messages and any details you share (such as your name, email,
          company and what you need) so it can answer you, and so a consultant can follow up or arrange a call.</p>
        <p>Do not share passwords, payment details or other sensitive information in the chat.</p>
        <p>Answers are written by an AI model from {brand.firmName}&apos;s approved information. It can make mistakes;
          prices, timelines and commitments are only ever set in a written proposal.</p>
        <p>Phone calls and calls made from this website are transcribed. The transcript is kept like a chat.</p>
        <p>Conversations are deleted automatically after 12 months, and booking records 2 years after the call.</p>
        <p>You can ask for a copy of what we hold about you, or for it to be deleted, at any time by contacting{" "}
          {brand.firmName}.</p>
      </div>
    </main>
  );
}
