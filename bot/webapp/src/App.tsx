import { useEffect, useState } from "react";
import {
  Navigate,
  Route,
  Routes,
  useLocation,
  useSearchParams,
} from "react-router-dom";
import { api } from "./api";
import { BottomNav } from "./components/BottomNav";
import type { Me } from "./types";
import { AdminPage } from "./pages/Admin";
import { BecomeMasterPage } from "./pages/BecomeMaster";
import { Today } from "./pages/Today";
import { BookingsPage } from "./pages/Bookings";
import { ClientsPage } from "./pages/Clients";
import { ServicesPage } from "./pages/Services";
import { StatsPage } from "./pages/Stats";
import { SettingsPage } from "./pages/Settings";
import { PublicBookingPage } from "./pages/Public";

/**
 * Top-level shell. Three modes:
 *
 * 1. ``?master=<slug>`` (or ``/book/<slug>``) — public booking page; renders
 *    without any auth so anyone with the link can book.
 * 2. Authenticated user without ``is_master`` — landing page that explains
 *    what Clientika is and how to become a master.
 * 3. Authenticated master/admin — the full master CRM with optional Admin
 *    tab for admins.
 */
function App() {
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const masterSlug = searchParams.get("master");

  if (masterSlug) {
    return <PublicBookingPage slug={masterSlug} />;
  }

  if (location.pathname.startsWith("/book/")) {
    const slug = location.pathname.split("/book/")[1] ?? "";
    return <PublicBookingPage slug={slug} />;
  }

  return <AuthenticatedShell />;
}

function AuthenticatedShell() {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await api.getMe();
        if (!cancelled) setMe(resp);
      } catch (err) {
        if (!cancelled) setError(String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) {
    return (
      <div className="app-shell">
        <main className="app-shell__main">
          <div style={{ padding: 32, textAlign: "center", color: "var(--hint)" }}>
            Загружаю…
          </div>
        </main>
      </div>
    );
  }

  if (error || !me) {
    return (
      <div className="app-shell">
        <main className="app-shell__main">
          <div className="error-banner">
            {error ?? "Не удалось загрузить профиль"}
          </div>
        </main>
      </div>
    );
  }

  // Non-masters get a focused landing screen — none of the master tabs are
  // reachable, and the bottom nav is hidden so the page feels intentional.
  if (!me.is_master && !me.is_admin) {
    return (
      <div className="app-shell">
        <main className="app-shell__main">
          <Routes>
            <Route path="*" element={<BecomeMasterPage me={me} />} />
          </Routes>
        </main>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <main className="app-shell__main">
        <Routes>
          <Route path="/" element={<Today />} />
          <Route path="/bookings" element={<BookingsPage />} />
          <Route path="/clients" element={<ClientsPage />} />
          <Route path="/services" element={<ServicesPage />} />
          <Route path="/stats" element={<StatsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          {me.is_admin ? (
            <Route
              path="/admin"
              element={<AdminPage currentUserId={me.id} />}
            />
          ) : null}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <BottomNav isAdmin={me.is_admin} />
    </div>
  );
}

export default App;
