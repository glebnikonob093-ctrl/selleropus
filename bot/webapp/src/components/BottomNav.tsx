import { NavLink } from "react-router-dom";

type Tab = { to: string; label: string; icon: string };

const MASTER_TABS: Tab[] = [
  { to: "/", label: "Сегодня", icon: "🗓️" },
  { to: "/bookings", label: "Записи", icon: "📋" },
  { to: "/clients", label: "Клиенты", icon: "👥" },
  { to: "/services", label: "Услуги", icon: "💼" },
  { to: "/stats", label: "Доход", icon: "📈" },
];

const ADMIN_TAB: Tab = { to: "/admin", label: "Админ", icon: "🛡️" };

export function BottomNav({ isAdmin = false }: { isAdmin?: boolean }) {
  const tabs = isAdmin ? [...MASTER_TABS, ADMIN_TAB] : MASTER_TABS;
  return (
    <nav className="bottom-nav">
      {tabs.map((tab) => (
        <NavLink
          key={tab.to}
          to={tab.to}
          end={tab.to === "/"}
          className={({ isActive }) =>
            "bottom-nav__item" + (isActive ? " bottom-nav__item--active" : "")
          }
        >
          <span className="bottom-nav__icon">{tab.icon}</span>
          <span className="bottom-nav__label">{tab.label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
