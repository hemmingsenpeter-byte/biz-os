import type { ReactNode } from "react";
import {
  AlertCircle,
  ArrowUpRight,
  Check,
  Inbox,
  LoaderCircle,
} from "lucide-react";
import Link from "next/link";
import { readable } from "@/lib/api";

export function Mark() {
  return (
    <span className="brand-mark" aria-hidden="true">
      <span />
      <span />
      <span />
      <span />
    </span>
  );
}
export function Badge({ status }: { status: string }) {
  return (
    <span className={`badge badge-${status}`}>
      {status === "done" || status === "executed" ? (
        <Check size={12} />
      ) : (
        <span className="badge-dot" />
      )}
      {readable(status)}
    </span>
  );
}
export function Loading({
  label = "Loading your workspace…",
}: {
  label?: string;
}) {
  return (
    <div className="loading-state" role="status">
      <LoaderCircle className="spin" size={22} />
      <span>{label}</span>
    </div>
  );
}
export function ErrorNotice({
  children,
  retry,
}: {
  children: ReactNode;
  retry?: () => void;
}) {
  return (
    <div className="notice notice-error" role="alert">
      <AlertCircle size={18} />
      <div>{children}</div>
      {retry && (
        <button className="text-button" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <Inbox size={24} />
      </span>
      <h2>{title}</h2>
      <p>{children}</p>
    </div>
  );
}
export function PageHeading({
  eyebrow,
  title,
  children,
  action,
}: {
  eyebrow: string;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        {children && <p className="page-description">{children}</p>}
      </div>
      {action}
    </div>
  );
}
export function DetailLink({
  href,
  children,
}: {
  href: string;
  children: ReactNode;
}) {
  return (
    <Link className="detail-link" href={href}>
      {children}
      <ArrowUpRight size={15} />
    </Link>
  );
}
