import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";
import type { AdminUser, AdminUserRole } from "../types";

type RoleFilter = "all" | AdminUserRole;

const ROLE_FILTERS: Array<{ key: RoleFilter; label: string }> = [
  { key: "all", label: "Все" },
  { key: "master", label: "Мастера" },
  { key: "pending", label: "Заявки" },
  { key: "admin", label: "Админы" },
];

function roleBadge(user: AdminUser): { label: string; cls: string } {
  if (user.is_admin) return { label: "админ", cls: "pill pill--confirmed" };
  if (user.is_master) return { label: "мастер", cls: "pill pill--came" };
  return { label: "не мастер", cls: "pill pill--new" };
}

/**
 * Admin panel for promoting/demoting bot users.
 *
 * The whole thing is gated behind ``is_admin`` server-side (every admin route
 * verifies via the ``get_current_admin`` dependency), so we don't need to
 * worry about non-admins reaching this URL — they'll just get 403s for every
 * call. The Mini App also hides the tab from the bottom nav as a UX nicety.
 */
export function AdminPage({ currentUserId }: { currentUserId: number }) {
  const [filter, setFilter] = useState<RoleFilter>("all");
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const [newTgId, setNewTgId] = useState("");
  const [newDisplayName, setNewDisplayName] = useState("");
  const [newUsername, setNewUsername] = useState("");
  const [adding, setAdding] = useState(false);

  const sorted = useMemo(() => {
    return [...users].sort((a, b) => {
      const score = (u: AdminUser) =>
        (u.is_admin ? 0 : u.is_master ? 1 : 2) * 1e15 - new Date(u.created_at).getTime();
      return score(a) - score(b);
    });
  }, [users]);

  async function refresh(activeFilter: RoleFilter = filter) {
    setLoading(true);
    setError(null);
    try {
      const role = activeFilter === "all" ? undefined : activeFilter;
      const resp = await api.listAdminUsers(role);
      setUsers(resp);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh(filter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  async function onAdd(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = parseInt(newTgId.trim(), 10);
    if (!parsed || parsed <= 0) {
      setError("TG ID должен быть положительным числом");
      return;
    }
    setAdding(true);
    setError(null);
    try {
      const created = await api.addMasterByTgId({
        tg_user_id: parsed,
        display_name: newDisplayName.trim() || null,
        tg_username: newUsername.trim() || null,
      });
      setInfo(`Готово: ${created.display_name} теперь мастер.`);
      setNewTgId("");
      setNewDisplayName("");
      setNewUsername("");
      await refresh(filter);
    } catch (err) {
      setError(String(err));
    } finally {
      setAdding(false);
    }
  }

  async function togglePromote(user: AdminUser) {
    setError(null);
    try {
      if (user.is_master) {
        await api.demoteMaster(user.id);
        setInfo(`${user.display_name} больше не мастер.`);
      } else {
        await api.setUserRoles(user.id, { is_master: true });
        setInfo(`${user.display_name} назначен мастером.`);
      }
      await refresh(filter);
    } catch (err) {
      setError(String(err));
    }
  }

  async function toggleAdmin(user: AdminUser) {
    setError(null);
    try {
      await api.setUserRoles(user.id, { is_admin: !user.is_admin });
      setInfo(
        user.is_admin
          ? `${user.display_name} больше не админ.`
          : `${user.display_name} теперь админ.`,
      );
      await refresh(filter);
    } catch (err) {
      setError(String(err));
    }
  }

  return (
    <div>
      <PageHeader title="Админ-панель" subtitle="Пользователи бота" />

      {error ? <div className="error-banner">{error}</div> : null}
      {info ? <div className="success-banner">{info}</div> : null}

      <form className="card" onSubmit={onAdd}>
        <div style={{ fontWeight: 600, marginBottom: 8 }}>
          Назначить мастером по TG ID
        </div>
        <div className="field">
          <label className="field__label">Telegram user id *</label>
          <input
            className="field__input"
            inputMode="numeric"
            pattern="[0-9]*"
            value={newTgId}
            onChange={(e) => setNewTgId(e.target.value)}
            placeholder="например, 1200247714"
          />
        </div>
        <div className="field">
          <label className="field__label">Имя (необязательно)</label>
          <input
            className="field__input"
            value={newDisplayName}
            onChange={(e) => setNewDisplayName(e.target.value)}
            placeholder="как звать мастера"
          />
        </div>
        <div className="field">
          <label className="field__label">@username (необязательно)</label>
          <input
            className="field__input"
            value={newUsername}
            onChange={(e) => setNewUsername(e.target.value)}
            placeholder="без @"
          />
        </div>
        <button className="btn btn--block" type="submit" disabled={adding}>
          {adding ? "Сохраняю…" : "Сделать мастером"}
        </button>
        <div
          style={{
            color: "var(--hint)",
            fontSize: 12,
            marginTop: 8,
          }}
        >
          Если пользователь ещё не открывал бота — создастся заглушка,
          она автоматически дополнится данными при первом /start.
        </div>
      </form>

      <div className="segment" style={{ marginTop: 16 }}>
        {ROLE_FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            className={
              "segment__btn" +
              (filter === f.key ? " segment__btn--active" : "")
            }
            onClick={() => setFilter(f.key)}
          >
            {f.label}
          </button>
        ))}
      </div>

      {loading ? <div style={{ color: "var(--hint)" }}>Загружаю…</div> : null}

      {!loading && sorted.length === 0 ? (
        <EmptyState
          icon="📂"
          title="Пусто"
          description="Под этим фильтром пока никого нет."
        />
      ) : null}

      <div className="list">
        {sorted.map((u) => {
          const badge = roleBadge(u);
          const isSelf = u.id === currentUserId;
          return (
            <div className="list-item" key={u.id}>
              <div className="list-item__row">
                <div>
                  <div className="list-item__title">
                    {u.display_name}
                    {isSelf ? " (вы)" : ""}
                  </div>
                  <div className="list-item__meta">
                    {u.tg_username ? `@${u.tg_username} · ` : ""}TG ID:{" "}
                    {u.tg_user_id}
                  </div>
                </div>
                <span className={badge.cls}>{badge.label}</span>
              </div>
              <div className="list-item__actions">
                <button
                  className="btn btn--small"
                  type="button"
                  onClick={() => togglePromote(u)}
                >
                  {u.is_master ? "Снять мастера" : "Сделать мастером"}
                </button>
                <button
                  className="btn btn--small btn--ghost"
                  type="button"
                  onClick={() => toggleAdmin(u)}
                  disabled={isSelf && u.is_admin}
                  title={isSelf && u.is_admin ? "Себя демоутить нельзя" : ""}
                >
                  {u.is_admin ? "Снять админа" : "Сделать админом"}
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
