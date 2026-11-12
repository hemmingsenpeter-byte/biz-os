import Link from "next/link";
export default function NotFound() {
  return (
    <main id="main-content" className="not-found">
      <p className="eyebrow">404 · PAGE NOT FOUND</p>
      <h1>This page isn&apos;t here.</h1>
      <p>Head back to your workspace to find your work.</p>
      <Link className="button button-primary" href="/approvals">
        Open workspace
      </Link>
    </main>
  );
}
