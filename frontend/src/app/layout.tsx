import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Acme IT Consulting",
  description: "Demo website with the AI assistant chat widget.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
