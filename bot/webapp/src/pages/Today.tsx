import { useEffect, useState } from "react";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";
import type { Booking, Me } from "../types";
import { formatTime, statusLabel } from "../utils/format";

export function Today() {
  const [me, setMe] = useState<Me | null>(null);
  const [bookings, setBookings] = useState<Booking[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [meResp, todayResp] = await Promise.all([
          api.getMe(),
          api.listBookingsToday(),
        ]);
        if (cancelled) return;
        setMe(meResp);
        setBookings(todayResp);
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

  return (
    <div>
      <PageHeader
        title={me ? `Привет, ${me.display_name.split(" ")[0]}!` : "Сегодня"}
        subtitle={me ? `Сегодня записей: ${bookings.length}` : undefined}
      />

      {error ? <div className="error-banner">{error}</div> : null}

      {!loading && me && bookings.length === 0 ? (
        <EmptyState
          icon="📭"
          title="На сегодня пусто"
          description="Поделитесь ссылкой, чтобы клиенты могли записаться сами."
        />
      ) : null}

      <div className="list">
        {bookings.map((b) => (
          <div className="list-item" key={b.id}>
            <div className="list-item__row">
              <div>
                <div className="list-item__title">
                  {formatTime(b.starts_at)} · {b.service_name}
                </div>
                <div className="list-item__meta">
                  {b.client_name}
                  {b.client_phone ? ` · ${b.client_phone}` : ""}
                </div>
              </div>
              <span className={`pill pill--${b.status}`}>{statusLabel(b.status)}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
