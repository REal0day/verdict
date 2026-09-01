import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, useAuth } from "@/lib/auth";
import { Layout } from "@/components/Layout";
import { Login } from "@/pages/Login";
import { Reports, ProductReports } from "@/pages/Reports";
import { ReportView } from "@/pages/ReportView";
import { Scans } from "@/pages/Scans";
import { ScanDetail } from "@/pages/ScanDetail";
import { FindingDetail } from "@/pages/FindingDetail";
import { Projects } from "@/pages/Projects";
import { ProjectDetail } from "@/pages/ProjectDetail";
import { Agents } from "@/pages/Agents";
import { Workbench } from "@/pages/Workbench";
import { AttachmentView } from "@/pages/AttachmentView";
import { ImportNew } from "@/pages/ImportNew";
import { ImportDetail } from "@/pages/ImportDetail";
import { Welcome } from "@/pages/Welcome";
import { Notifications } from "@/pages/Notifications";
import { Join } from "@/pages/Join";
import { Harnesses } from "@/pages/Harnesses";
import { HarnessNew } from "@/pages/HarnessNew";
import { HarnessDetail } from "@/pages/HarnessDetail";
import { ProductFindings } from "@/pages/ProductFindings";
import { Analytics } from "@/pages/Analytics";
import { Prompts } from "@/pages/Prompts";
import { Profile } from "@/pages/Profile";
import { UsersAdmin } from "@/pages/admin/Users";
import { TeamsAdmin } from "@/pages/admin/Teams";
import { SettingsAdmin } from "@/pages/admin/Settings";

const qc = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, refetchOnWindowFocus: false, retry: 1 },
  },
});

function Gate({ children }: { children: React.ReactNode }) {
  const { me, ready } = useAuth();
  if (!ready) return <div className="min-h-screen bg-bg flex items-center justify-center text-fgmuted text-sm">Loading…</div>;
  if (!me) return <Navigate to="/login" replace />;
  // The first-time onboarding redirect lives in Login.tsx (and the
  // registration form's post-signup redirect) so the user is only
  // sent to /welcome once, not on every subsequent navigation.
  return <>{children}</>;
}

function LegacyProjectRedirect() {
  // /projects/:id -> /products/:id (notification links + old bookmarks).
  const { project_id = "" } = useParams();
  return <Navigate to={`/products/${project_id}`} replace />;
}

function AdminOnly({ children }: { children: React.ReactNode }) {
  const { me } = useAuth();
  if (me?.role !== "admin") return <Navigate to="/" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <BrowserRouter basename="/app">
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/join/:token" element={<Join />} />
            <Route element={<Gate><Layout /></Gate>}>
              <Route index element={<Reports />} />
              <Route path="reports/by-product/:project_id" element={<ProductReports />} />
              <Route path="reports/:report_id" element={<ReportView />} />
              <Route path="scans" element={<Scans />} />
              <Route path="scans/:scan_id" element={<ScanDetail />} />
              <Route path="scans/:scan_id/findings/:finding_id" element={<FindingDetail />} />
              <Route path="products" element={<Projects />} />
              <Route path="products/:project_id" element={<ProjectDetail />} />
              <Route path="products/:project_id/findings" element={<ProductFindings />} />
              {/* Legacy /projects URLs (notifications + bookmarks made before
                  the rename) keep working via redirect. */}
              <Route path="projects" element={<Navigate to="/products" replace />} />
              <Route
                path="projects/:project_id"
                element={<LegacyProjectRedirect />}
              />
              <Route path="attachments/:att_id" element={<AttachmentView />} />
              <Route path="workbench" element={<Workbench />} />
              <Route path="workbench/:sid" element={<Workbench />} />
              <Route path="agents" element={<Agents />} />
              <Route path="imports/new" element={<ImportNew />} />
              <Route path="imports/:imp_id" element={<ImportDetail />} />
              <Route path="welcome" element={<Welcome />} />
              <Route path="notifications" element={<Notifications />} />
              <Route path="harnesses" element={<Harnesses />} />
              <Route path="harnesses/new" element={<HarnessNew />} />
              <Route path="harnesses/:harness_id" element={<HarnessDetail />} />
              <Route path="analytics" element={<Analytics />} />
              <Route path="prompts" element={<Prompts />} />
              <Route path="profile" element={<Profile />} />
              <Route path="admin/users" element={<AdminOnly><UsersAdmin /></AdminOnly>} />
              <Route path="admin/teams" element={<AdminOnly><TeamsAdmin /></AdminOnly>} />
              <Route path="admin/settings" element={<AdminOnly><SettingsAdmin /></AdminOnly>} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  );
}
