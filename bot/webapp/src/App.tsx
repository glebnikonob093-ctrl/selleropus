import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation, useSearchParams } from "react-router-dom";
import { api } from "./api";
import { BottomNav } from "./components/BottomNav";
import { Today } from "./pages/Today";
import { BookingsPage } from "./pages/Bookings";
import { ClientsPage } from "./pages/Clients";
import { ServicesPage } from "./pages/Services";
import { StatsPage } from "./pages/Stats";
import { SettingsPage } from "./pages/Settings";
import { PublicBookingPage } from "./pages/Public";
import type { Me } from "./types";

function NotMasterPage() {
  return (
    <div style={{ padding: "2rem", textAlign: "center" }}>
      <h2>Clientika</h2>
      <p>
        Для записи к мастеру используйте ссылку, которую он вам прислал.
      </p>
      <p style={{ color: "#888", fontSize: "0.9rem" }}>
        Если вы мастер — напишите боту <code>/start</code> чтобы зарегистрироваться.
      </p>
    </div>
  );
}

function App() {
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const masterSlug = searchParams.get("master");

  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .getMe()
      .then(setMe)
      .catch(() => setMe(null))
      .finally(() => setLoading(false));
  }, []);

  // If a "?master=<slug>" query param is present we always show the public
  // booking flow, regardless of route. This makes it trivial for masters to
  // share a single deep link with clients.
  if (masterSlug) {
    return <PublicBookingPage slug={masterSlug} />;
  }

  if (location.pathname.startsWith("/book/")) {
    const slug = location.pathname.split("/book/")[1] ?? "";
    return <PublicBookingPage slug={slug} />;
  }

  if (loading) {
    return (
      <div style={{ padding: "2rem", textAlign: "center" }}>Загрузка...</div>
    );
  }

  if (!me?.is_master) {
    return <NotMasterPage />;
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
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <BottomNav />
    </div>
  );
}

export default App;
