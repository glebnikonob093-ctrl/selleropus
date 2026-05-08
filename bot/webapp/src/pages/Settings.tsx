import { useEffect, useState } from "react";
import { api } from "../api";
import { PageHeader } from "../components/PageHeader";
import type { Me } from "../types";

function buildPublicUrl(me: Me): string {
  const origin = window.location.origin;
  return `${origin}/${me.public_link_path}`;
}

function minutesToHHMM(m: number): string {
  const h = String(Math.floor(m / 60)).padStart(2, "0");
  const mm = String(m % 60).padStart(2, "0");
  return `${h}:${mm}`;
}

function hhmmToMinutes(s: string): number {
  const [h, m] = s.split(":").map(Number);
  return (h ?? 0) * 60 + (m ?? 0);
}

export function SettingsPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const m = await api.getMe();
        setMe(m);
      } catch (err) {
        setError(String(err));
      }
    })();
  }, []);

  const save = async () => {
    if (!me) return;
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const updated = await api.updateMe({
        display_name: me.display_name,
        slug: me.slug,
        timezone: me.timezone,
        work_start_minutes: me.work_start_minutes,
        work_end_minutes: me.work_end_minutes,
        slot_step_minutes: me.slot_step_minutes,
      });
      setMe(updated);
      setSaved(true);
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  };

  if (!me) {
    return (
      <div>
        <PageHeader title="Настройки" />
        {error ? <div className="error-banner">{error}</div> : null}
      </div>
    );
  }

  const publicUrl = buildPublicUrl(me);

  return (
    <div>
      <PageHeader title="Настройки" />
      {error ? <div className="error-banner">{error}</div> : null}
      {saved ? <div className="success-banner">Сохранено</div> : null}

      <div className="card">
        <div style={{ marginBottom: 8, fontWeight: 600 }}>
          Ссылка для записи клиентов
        </div>
        <div className="copy-link">{publicUrl}</div>
        <div style={{ height: 8 }} />
        <button
          className="btn btn--ghost btn--block"
          onClick={() => {
            navigator.clipboard?.writeText(publicUrl).catch(() => undefined);
          }}
        >
          Скопировать
        </button>
      </div>

      <div className="field">
        <label className="field__label">Отображаемое имя</label>
        <input
          className="field__input"
          value={me.display_name}
          onChange={(e) => setMe({ ...me, display_name: e.target.value })}
        />
      </div>

      <div className="field">
        <label className="field__label">Slug (часть ссылки для клиентов)</label>
        <input
          className="field__input"
          value={me.slug}
          onChange={(e) => setMe({ ...me, slug: e.target.value })}
        />
      </div>

      <div className="field">
        <label className="field__label">Таймзона (IANA)</label>
        <input
          className="field__input"
          value={me.timezone}
          onChange={(e) => setMe({ ...me, timezone: e.target.value })}
          placeholder="Europe/Moscow"
        />
      </div>

      <div className="field__row">
        <div className="field" style={{ flex: 1 }}>
          <label className="field__label">Начало рабочего дня</label>
          <input
            className="field__input"
            type="time"
            value={minutesToHHMM(me.work_start_minutes)}
            onChange={(e) =>
              setMe({ ...me, work_start_minutes: hhmmToMinutes(e.target.value) })
            }
          />
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label className="field__label">Конец</label>
          <input
            className="field__input"
            type="time"
            value={minutesToHHMM(me.work_end_minutes)}
            onChange={(e) =>
              setMe({ ...me, work_end_minutes: hhmmToMinutes(e.target.value) })
            }
          />
        </div>
      </div>

      <div className="field">
        <label className="field__label">Шаг сетки слотов, минут</label>
        <input
          className="field__input"
          inputMode="numeric"
          value={me.slot_step_minutes}
          onChange={(e) =>
            setMe({ ...me, slot_step_minutes: Number(e.target.value) || 30 })
          }
        />
      </div>

      <button className="btn btn--block" disabled={saving} onClick={save}>
        {saving ? "Сохраняем..." : "Сохранить"}
      </button>
    </div>
  );
}
