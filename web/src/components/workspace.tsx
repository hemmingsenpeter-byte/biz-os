"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import {
  ArrowUpRight,
  ChartColumn,
  CheckCheck,
  CircleHelp,
  Inbox,
  LogOut,
  Menu,
  ShieldCheck,
  X,
} from "lucide-react";
import {
  api,
  ApiError,
  errorMessage,
  type ControlState,
  type User,
} from "@/lib/api";
import { PauseControl, PausedBanner } from "./control";
import { ErrorNotice, Loading, Mark } from "./ui";

const NAV = [
  { href: "/approvals", label: "Approvals", icon: Inbox },
  { href: "/tasks", label: "Tasks", icon: CheckCheck },
  { href: "/scorecard", label: "Scorecard", icon: ChartColumn },
];

const Session = createContext<User | null>(null);
export const useUser = () => useContext(Session);
const Control = createContext<{
  state: ControlState | null;
  refresh: () => void;
}>({ state: null, refresh: () => {} });
// Whether everything is paused; state is null until the first answer arrives.
export const useControl = () => useContext(Control);

export function Workspace({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState("");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    api<User>("/auth/me", { signal: controller.signal })
      .then(setUser)
      .catch((err) => {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError && err.status === 401)
          router.replace(`/auth/login?next=${encodeURIComponent(pathname)}`);
        else setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [router, pathname]);
  const [control, setControl] = useState<ControlState | null>(null);
  const refreshControl = useCallback(() => {
    api<ControlState>("/control")
      .then(setControl)
      .catch(() => {
        // Keep the last known state; the next check or action corrects it.
      });
  }, []);
  const signedIn = Boolean(user);
  useEffect(() => {
    if (!signedIn) return;
    refreshControl();
    const timer = window.setInterval(refreshControl, 30000);
    window.addEventListener("focus", refreshControl);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshControl);
    };
  }, [signedIn, pathname, refreshControl]);
  const controlValue = useMemo(
    () => ({ state: control, refresh: refreshControl }),
    [control, refreshControl],
  );
  async function logout() {
    setSigningOut(true);
    try {
      await api("/auth/logout", { method: "POST" });
      router.replace("/auth/login");
      router.refresh();
    } catch (err) {
      setError(errorMessage(err));
      setSigningOut(false);
    }
  }
  if (!user)
    return (
      <div className="initial-screen">
        <Mark />
        {error ? (
          <ErrorNotice retry={() => window.location.reload()}>
            {error}
          </ErrorNotice>
        ) : (
          <Loading label="Opening your workspace…" />
        )}
      </div>
    );
  const initials = (user.display_name || user.email)
    .split(/\s+/)
    .map((s) => s[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
  return (
    <Session.Provider value={user}>
      <Control.Provider value={controlValue}>
        <div className="workspace">
          {mobileOpen && (
            <button
              className="nav-scrim"
              aria-label="Close navigation"
              onClick={() => setMobileOpen(false)}
            />
          )}
          <aside className={`sidebar ${mobileOpen ? "is-open" : ""}`}>
            <Link
              href="/approvals"
              className="brand"
              aria-label="BIZ OS home"
              onClick={() => setMobileOpen(false)}
            >
              <Mark />
              <span>
                BIZ<span className="brand-light">OS</span>
              </span>
            </Link>
            <button
              className="mobile-close icon-button"
              aria-label="Close navigation"
              onClick={() => setMobileOpen(false)}
            >
              <X size={20} />
            </button>
            <div className="workspace-label">
              <span className="workspace-monogram">B</span>
              <div>
                <strong>Your workspace</strong>
                <span>Business operations</span>
              </div>
              <ShieldCheck size={15} />
            </div>
            <p className="nav-label">WORKSPACE</p>
            <nav aria-label="Main navigation">
              {NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`nav-item ${pathname.startsWith(item.href) ? "active" : ""}`}
                  onClick={() => setMobileOpen(false)}
                >
                  <item.icon size={19} />
                  {item.label}
                  {pathname.startsWith(item.href) && (
                    <span className="nav-active-dot" />
                  )}
                </Link>
              ))}
            </nav>
            <div className="sidebar-bottom">
              <div className="sidebar-note">
                <ShieldCheck size={18} />
                <p>
                  <strong>You&apos;re in control.</strong>
                  <span>Review the details. Make the call.</span>
                </p>
              </div>
              <a
                className="support-link"
                href="https://github.com/hemmingsenpeter-byte/biz-os#readme"
                target="_blank"
                rel="noreferrer"
              >
                <CircleHelp size={16} />
                Workspace guide
                <ArrowUpRight size={14} />
              </a>
              <div className="sidebar-version">
                <span className="live-dot" />
                BIZ OS · Owner console
              </div>
            </div>
          </aside>
          <div className="workspace-main">
            <header className="topbar">
              <div className="breadcrumb">
                <button
                  className="mobile-menu icon-button"
                  aria-label="Open navigation"
                  onClick={() => setMobileOpen(true)}
                >
                  <Menu size={22} />
                </button>
                <span>Workspace</span>
                <span className="breadcrumb-slash">/</span>
                <strong>
                  {NAV.find((item) => pathname.startsWith(item.href))?.label ??
                    "Approvals"}
                </strong>
              </div>
              <div className="account">
                {control?.paused ? (
                  <span className="paused-chip">Paused</span>
                ) : (
                  <PauseControl
                    mayResume={user.role === "owner"}
                    onChange={setControl}
                  />
                )}
                <span className="account-name">
                  {user.display_name || user.email}
                  <small>{user.role === "owner" ? "Owner" : "Staff"}</small>
                </span>
                <span className="avatar">{initials}</span>
                <button
                  className="icon-button logout"
                  aria-label="Sign out"
                  title="Sign out"
                  disabled={signingOut}
                  onClick={() => void logout()}
                >
                  <LogOut size={17} />
                </button>
              </div>
            </header>
            {control?.paused && (
              <PausedBanner
                state={control}
                mayResume={user.role === "owner"}
                onChange={setControl}
              />
            )}
            <main id="main-content" className="main-content">
              {error && <ErrorNotice>{error}</ErrorNotice>}
              {children}
              <footer className="workspace-footer">
                <span>Every decision leaves a record.</span>
                <span>
                  BIZ OS <span className="footer-dot">·</span> Your business, in
                  view.
                </span>
              </footer>
            </main>
          </div>
        </div>
      </Control.Provider>
    </Session.Provider>
  );
}
