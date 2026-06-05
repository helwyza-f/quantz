import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Quantz Control",
  description: "Operational console for Quantz autonomous trading agent",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
