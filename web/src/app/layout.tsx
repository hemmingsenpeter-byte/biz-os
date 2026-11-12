import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "BIZ OS — Your business, in view",
    template: "%s · BIZ OS",
  },
  description: "Review approvals, follow work and keep every decision in view.",
  robots: { index: false, follow: false },
  referrer: "same-origin",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <a className="skip-link" href="#main-content">
          Skip to content
        </a>
        {children}
      </body>
    </html>
  );
}
