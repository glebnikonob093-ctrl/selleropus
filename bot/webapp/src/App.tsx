import { Navigate, Route, Routes, useLocation, useSearchParams } from "react-router-dom";
import { BottomNav } from "./components/BottomNav";
import { Today } from "./pages/Today";
import { BookingsPage } from "./pages/Bookings";
import { ClientsPage } from "./pages/Clients";
import { ServicesPage } from "./pages/Services";
import { StatsPage } from "./pages/Stats";
import { SettingsPage } from "./pages/Settings";
import { PublicBookingPage } from "./pages/Public";

function App() {
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const masterSlug = searchParams.get("master");

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
