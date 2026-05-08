import { useEffect, useState } from "react";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";
import type { ReturnClient, Stats } from "../types";
import { formatPrice } from "../utils/format";

type Period = "day" | "week" | "month";

export function StatsPage() {
  const [period, setPeriod] = useState<Period>("month");
  const [stats, setStats] = useState<Stats | null>(null);
  const [returnClients, setReturnClients] = useState<ReturnClient[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [s, rc] = await Promise.all([
          api.getStats(period),
          api.getReturnClients(30),
        ]);
        if (cancelled) return;
        setStats(s);
        setReturnClients(rc);
      } catch (err) {
        if (!cancelled) setError(String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [period]);

  return (
    <div>
      <PageHeader title="Доход" />
      {error ? <div className="error-banner">{error}</div> : null}

      <div className="segment">
        {(["day", "week", "month"] as Period[]).map((p) => (
          <button
            key={p}
            className={
              "segment__btn" + (p === period ? " segment__btn--active" : "")
            }
            onClick={() => setPeriod(p)}
          >
            {p === "day" ? "День" : p === "week" ? "Неделя" : "Месяц"}
          </button>
        ))}
      </div>

      <div className="kpi-grid">
        <div className="kpi">
          <div className="kpi__label">Доход</div>
          <div className="kpi__value">{formatPrice(stats?.revenue ?? 0)}</div>
        </div>
        <div className="kpi">
          <div className="kpi__label">Записей</div>
          <div className="kpi__value">{stats?.bookings_total ?? 0}</div>
        </div>
        <div className="kpi">
          <div className="kpi__label">Из них «пришёл»</div>
          <div className="kpi__value">{stats?.bookings_came ?? 0}</div>
        </div>
        <div className="kpi">
          <div className="kpi__label">Вернуть клиентов</div>
          <div className="kpi__value">{returnClients.length}</div>
        </div>
      </div>

      <h3 style={{ margin: "16px 0 8px" }}>Топ услуг</h3>
      {stats && stats.top_services.length > 0 ? (
        <div className="list">
          {stats.top_services.map((t) => (
            <div className="list-item" key={t.service_id}>
              <div className="list-item__row">
                <div className="list-item__title">{t.service_name}</div>
                <div className="list-item__meta">
                  {t.bookings} · {formatPrice(t.revenue)}
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState
          icon="📈"
          title="Пока нет статистики"
          description="Когда клиенты начнут приходить, здесь появится топ услуг."
        />
      )}

      <h3 style={{ margin: "16px 0 8px" }}>Кого пора вернуть</h3>
      {returnClients.length > 0 ? (
        <div className="list">
          {returnClients.map((rc) => (
            <div className="list-item" key={rc.client_id}>
              <div className="list-item__row">
                <div className="list-item__title">{rc.name}</div>
                <div className="list-item__meta">
                  {rc.days_since != null
                    ? `${rc.days_since} дней без визита`
                    : "новый"}
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState
          icon="✨"
          title="Все клиенты были недавно"
          description="Никого возвращать не нужно — продолжайте в том же духе."
        />
      )}
    </div>
  );
}
