import { Link, NavLink, Outlet } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/cn";
import {
  FileText, ShieldAlert, FolderGit2, Server, Terminal,
  LogOut, Sun, Moon, UserCircle2, Users, Users2, Wrench, BarChart3,
  SlidersHorizontal, MessageSquareText,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NotificationsBell } from "@/components/NotificationsBell";

const NAV = [
  { to: "/workbench", label: "Workbench", icon: Terminal },
  { to: "/products",  label: "Products",  icon: FolderGit2 },
  { to: "/",          label: "Reports",   icon: FileText },
  { to: "/scans",     label: "Scans",     icon: ShieldAlert },
  { to: "/analytics", label: "Analytics", icon: BarChart3 },
  { to: "/prompts",   label: "Prompts",   icon: MessageSquareText },
  { to: "/harnesses", label: "Harnesses", icon: Wrench },
  { to: "/agents",    label: "Agents",    icon: Server },
] as const;

const ADMIN_NAV = [
  { to: "/admin/users", label: "Users", icon: Users },
  { to: "/admin/teams", label: "Teams", icon: Users2 },
  { to: "/admin/settings", label: "Settings", icon: SlidersHorizontal },
] as const;

export function Layout() {
  const { me, logout } = useAuth();
  const [dark, setDark] = useState(
    () => localStorage.getItem("irs.theme") === "dark",
  );

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("irs.theme", dark ? "dark" : "light");
  }, [dark]);

  return (
    <div className="flex min-h-screen">
      {/* sidebar */}
      <aside className="w-60 shrink-0 bg-surface border-r border-border flex flex-col">
        <div className="px-5 py-4 border-b border-border">
          <div className="text-lg font-semibold tracking-tight">
            <span className="text-primary">Verdict</span>
            <span className="text-fgmuted font-normal text-xs ml-2">AI security findings</span>
          </div>
        </div>
        <nav className="flex-1 px-2 py-3 space-y-0.5 overflow-y-auto">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors",
                  isActive
                    ? "bg-primary/10 text-primary"
                    : "text-fgmuted hover:bg-muted hover:text-fg"
                )
              }
            >
              <Icon size={16} strokeWidth={2} />
              <span>{label}</span>
            </NavLink>
          ))}

          {me?.role === "admin" ? (
            <>
              <div className="px-3 pt-4 pb-1 text-[10px] uppercase tracking-wider text-fgmuted/70">
                Admin
              </div>
              {ADMIN_NAV.map(({ to, label, icon: Icon }) => (
                <NavLink
                  key={to}
                  to={to}
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors",
                      isActive
                        ? "bg-primary/10 text-primary"
                        : "text-fgmuted hover:bg-muted hover:text-fg"
                    )
                  }
                >
                  <Icon size={16} strokeWidth={2} />
                  <span>{label}</span>
                </NavLink>
              ))}
            </>
          ) : null}
        </nav>
        <div className="px-3 py-3 border-t border-border space-y-2">
          <Link
            to="/profile"
            className="flex items-center gap-2 text-xs text-fgmuted hover:text-fg px-2 truncate group"
          >
            <UserCircle2 size={14} className="shrink-0 group-hover:text-primary" />
            <span className="truncate">
              {me?.email}
              {me ? <span className="ml-1 text-fgmuted/70">({me.role})</span> : null}
            </span>
          </Link>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => setDark((d) => !d)}
              className="flex-1 inline-flex items-center justify-center gap-1.5 px-2 py-1.5 rounded-md text-xs text-fgmuted hover:bg-muted hover:text-fg"
              title="Toggle theme"
            >
              {dark ? <Sun size={14} /> : <Moon size={14} />}
              {dark ? "Light" : "Dark"}
            </button>
            <button
              type="button"
              onClick={logout}
              className="flex-1 inline-flex items-center justify-center gap-1.5 px-2 py-1.5 rounded-md text-xs text-fgmuted hover:bg-danger/10 hover:text-danger"
              title="Sign out"
            >
              <LogOut size={14} /> Sign out
            </button>
          </div>
        </div>
      </aside>

      {/* content */}
      <main className="flex-1 min-w-0 bg-bg">
        <div className="max-w-[1400px] mx-auto px-6 py-6">
          <div className="flex justify-end mb-2">
            <NotificationsBell />
          </div>
          <Outlet />
        </div>
      </main>
    </div>
  );
}

export function PageHeader({ title, subtitle, action }:
  { title: string; subtitle?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="flex items-end justify-between gap-4 mb-5">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle ? <p className="text-sm text-fgmuted mt-0.5">{subtitle}</p> : null}
      </div>
      {action}
    </div>
  );
}
