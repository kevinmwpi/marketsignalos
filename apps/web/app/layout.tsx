import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "MarketSignalOS",
  description: "Track Polymarket wallets, historical forecasting edge, and their open positions.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
