import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Acme IT Consulting",
  description: "Acme IT Consulting, with Ava, the AI assistant.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
