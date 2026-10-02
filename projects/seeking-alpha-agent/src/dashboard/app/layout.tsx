import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SAA ledger",
  description: "Seeking Alpha Agent — read-only ledger dashboard: shadow + paper trades, R distribution, expectancy, posterior p/W, Brier by bucket, growth toward the goal.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
