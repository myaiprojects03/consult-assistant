import type { Metadata } from "next";

// Internal tool: never indexed, never framed by other sites.
export const metadata: Metadata = {
  title: "Team assistant",
  robots: { index: false, follow: false },
};

export default function TeamLayout({ children }: { children: React.ReactNode }) {
  return children;
}
