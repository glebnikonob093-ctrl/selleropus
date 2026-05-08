import { NavLink } from "react-router-dom";

const TABS: Array<{ to: string; label: string; icon: string }> = [
  { to: "/", label: "Сегодня", icon: "🗓️" },
  { to: "/bookings", label: "Записи", icon: "📋" },
  { to: "/clients", label: "Клиенты", icon: "👥" },
  { to: "/services", label: "Услуги", icon: "💼" },
  { to: "/stats", label: "Доход", icon: "📈" },
];

export function BottomNav() {
  return (
    <nav className="bottom-nav">
      {TABS.map((tab) => (
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
