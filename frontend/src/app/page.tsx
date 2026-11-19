import Link from "next/link";
import Script from "next/script";

import { brand } from "@/config/brand";

// A stand-in for the firm's real website, so the widget can be seen and tested the way visitors meet it.
// On the real site, only the one <script> line below is added.
const services = [
  ["Cloud migration", "Move servers and applications to Azure or AWS, then keep costs in check."],
  ["Managed IT", "Help desk, monitoring, patching and backups for your team, every month."],
  ["Cybersecurity", "Assessments, hardening and preparation for ISO 27001 and Cyber Essentials."],
  ["Microsoft 365", "Teams, SharePoint, Exchange and device management, set up properly."],
  ["Data and automation", "Dashboards, workflow automation and AI assistants on your own data."],
  ["IT strategy", "Roadmaps, budgets and a part-time virtual CIO."],
];

export default function DemoSite() {
  const c = brand.colors;
  return (
    <div style={{ color: c.ink }}>
      <aside aria-label="About this site" className="px-4 py-2 text-center text-xs" style={{ background: c.soft, color: c.muted }}>
        Demo website for a sample firm. The chat in the corner is the real assistant.
      </aside>
      <header className="mx-auto flex max-w-5xl items-center justify-between px-6 py-6">
        <span className="text-lg font-bold tracking-tight">{brand.firmName}</span>
        <nav className="hidden gap-6 text-sm sm:flex" style={{ color: c.muted }}>
          <a href="#services">Services</a><a href="#how">How we work</a><Link href="/voice">Call us</Link>
          <a href={brand.privacyUrl}>Privacy</a>
        </nav>
      </header>

      <main>
      <section className="mx-auto max-w-5xl px-6 pb-16 pt-10">
        <h1 className="max-w-2xl text-4xl font-bold leading-tight sm:text-5xl">
          IT that keeps up with your business.
        </h1>
        <p className="mt-5 max-w-xl text-lg leading-relaxed" style={{ color: c.muted }}>
          Fifty consultants helping SMEs and enterprises with cloud, security and everyday IT.
          Ask our assistant anything, or book a free discovery call.
        </p>
      </section>

      <section id="services" className="border-t" style={{ borderColor: c.line }}>
        <div className="mx-auto grid max-w-5xl gap-x-10 gap-y-8 px-6 py-14 sm:grid-cols-2 lg:grid-cols-3">
          {services.map(([title, text]) => (
            <div key={title}>
              <h2 className="font-semibold">{title}</h2>
              <p className="mt-1.5 text-sm leading-relaxed" style={{ color: c.muted }}>{text}</p>
            </div>
          ))}
        </div>
      </section>

      <section id="how" className="border-t" style={{ borderColor: c.line, background: c.soft }}>
        <div className="mx-auto max-w-5xl px-6 py-14">
          <h2 className="text-2xl font-bold">How we work</h2>
          <ol className="mt-6 grid gap-6 sm:grid-cols-3">
            {["A free 30-minute discovery call", "A written proposal with scope and cost", "Delivery, then ongoing support"]
              .map((step, i) => (
                <li key={step} className="text-sm leading-relaxed">
                  <span className="mb-2 block text-xs font-semibold" style={{ color: c.accent }}>Step {i + 1}</span>
                  {step}
                </li>
              ))}
          </ol>
        </div>
      </section>

      </main>

      <footer className="mx-auto max-w-5xl px-6 py-10 text-xs" style={{ color: c.muted }}>
        © {new Date().getFullYear()} {brand.firmName} (sample) · <a className="underline" href={brand.privacyUrl}>Privacy</a>
      </footer>

      {/* The one line the firm adds to its website: */}
      <Script src="/embed.js" strategy="afterInteractive" data-label={brand.launcherLabel} data-color={c.accent} />
    </div>
  );
}
