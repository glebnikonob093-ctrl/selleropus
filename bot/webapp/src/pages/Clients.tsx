import { useEffect, useState } from "react";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import type { Client } from "../types";
import { formatDateTime } from "../utils/format";

export function ClientsPage() {
  const [clients, setClients] = useState<Client[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Client | null>(null);

  const reload = async (q?: string) => {
    try {
      const list = await api.listClients(q);
      setClients(list);
    } catch (err) {
      setError(String(err));
    }
  };

  useEffect(() => {
    void reload();
  }, []);

  return (
    <div>
      <PageHeader
        title="Клиенты"
        subtitle={clients.length ? `Всего: ${clients.length}` : undefined}
        action={
          <button className="btn btn--small" onClick={() => setCreating(true)}>
            + Клиент
          </button>
        }
      />

      {error ? <div className="error-banner">{error}</div> : null}

      <div className="field">
        <input
          className="field__input"
          placeholder="Поиск по имени или телефону"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            void reload(e.target.value);
          }}
        />
      </div>

      {clients.length === 0 ? (
        <EmptyState
          icon="👥"
          title="Список пуст"
          description="Добавьте первого клиента или дайте ссылку — клиенты появятся сами."
        />
      ) : null}

      <div className="list">
        {clients.map((c) => (
          <div
            className="list-item"
            key={c.id}
            style={{
              textAlign: "left",
              border: "1px solid var(--border)",
              padding: "8px 12px",
              marginBottom: 6,
              borderRadius: 8,
              background: c.is_blocked ? "rgba(255,0,0,0.05)" : undefined,
            }}
          >
            <div
              className="list-item__row"
              onClick={() => setEditing(c)}
              style={{ cursor: "pointer" }}
            >
              <div>
                <div className="list-item__title">
                  {c.name}
                  {c.is_blocked ? " 🚫" : ""}
                </div>
                <div className="list-item__meta">
                  {c.phone ? c.phone : "—"}
                  {c.tg_username ? (
                    <>
                      {" · "}
                      <a
                        href={`https://t.me/${c.tg_username}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={(e) => e.stopPropagation()}
                        style={{ color: "var(--link, #2481cc)" }}
                      >
                        @{c.tg_username}
                      </a>
                    </>
                  ) : null}
                </div>
                {c.tg_user_id ? (
                  <div className="list-item__meta">
                    TG ID:{" "}
                    <span
                      onClick={(e) => {
                        e.stopPropagation();
                        void navigator.clipboard.writeText(
                          String(c.tg_user_id),
                        );
                      }}
                      title="Нажмите чтобы скопировать"
                      style={{
                        cursor: "pointer",
                        textDecoration: "underline dotted",
                        fontFamily: "monospace",
                      }}
                    >
                      {c.tg_user_id}
                    </span>
                  </div>
                ) : null}
              </div>
              <div className="list-item__meta" style={{ textAlign: "right" }}>
                {c.last_visit_at ? formatDateTime(c.last_visit_at) : "ни разу"}
              </div>
            </div>
            {c.tg_user_id ? (
              <div style={{ marginTop: 6, display: "flex", gap: 6 }}>
                {c.is_blocked ? (
                  <button
                    className="btn btn--small"
                    style={{ fontSize: 12 }}
                    onClick={async () => {
                      await api.unblockClient(c.tg_user_id!);
                      await reload(query);
                    }}
                  >
                    ✅ Разблокировать
                  </button>
                ) : (
                  <button
                    className="btn btn--small btn--ghost"
                    style={{ fontSize: 12, color: "var(--danger, red)" }}
                    onClick={async () => {
                      const ok = window.confirm(
                        `Заблокировать клиента "${c.name}"?`,
                      );
                      if (!ok) return;
                      await api.blockClient(c.tg_user_id!);
                      await reload(query);
                    }}
                  >
                    🚫 Заблокировать
                  </button>
                )}
              </div>
            ) : null}
          </div>
        ))}
      </div>

      <ClientFormModal
        open={creating}
        title="Новый клиент"
        onClose={() => setCreating(false)}
        onSubmit={async (payload) => {
          await api.createClient(payload);
          setCreating(false);
          await reload(query);
        }}
      />

      <ClientFormModal
        open={!!editing}
        title="Редактирование клиента"
        client={editing ?? undefined}
        onClose={() => setEditing(null)}
        onDelete={
          editing
            ? async () => {
                if (!editing) return;
                const ok = window.confirm(
                  `Удалить клиента "${editing.name}" и все его записи? Это действие нельзя отменить.`,
                );
                if (!ok) return;
                await api.deleteClient(editing.id);
                setEditing(null);
                await reload(query);
              }
            : undefined
        }
        onSubmit={async (payload) => {
          if (!editing) return;
          await api.updateClient(editing.id, payload);
          setEditing(null);
          await reload(query);
        }}
      />
    </div>
  );
}

function ClientFormModal({
  open,
  title,
  client,
  onClose,
  onSubmit,
  onDelete,
}: {
  open: boolean;
  title: string;
  client?: Client;
  onClose: () => void;
  onSubmit: (payload: {
    name: string;
    phone?: string | null;
    tg_username?: string | null;
    notes?: string | null;
  }) => Promise<void>;
  onDelete?: () => Promise<void>;
}) {
  const [name, setName] = useState(client?.name ?? "");
  const [phone, setPhone] = useState(client?.phone ?? "");
  const [tgUsername, setTgUsername] = useState(client?.tg_username ?? "");
  const [notes, setNotes] = useState(client?.notes ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setName(client?.name ?? "");
    setPhone(client?.phone ?? "");
    setTgUsername(client?.tg_username ?? "");
    setNotes(client?.notes ?? "");
    setError(null);
  }, [client, open]);

  const submit = async () => {
    if (!name.trim()) {
      setError("Имя обязательно");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSubmit({
        name: name.trim(),
        phone: phone.trim() || null,
        tg_username: tgUsername.trim() || null,
        notes: notes.trim() || null,
      });
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} title={title} onClose={onClose}>
      {error ? <div className="error-banner">{error}</div> : null}
      <div className="field">
        <label className="field__label">Имя</label>
        <input
          className="field__input"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field__label">Телефон</label>
        <input
          className="field__input"
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field__label">Telegram username</label>
        <input
          className="field__input"
          value={tgUsername}
          onChange={(e) => setTgUsername(e.target.value)}
          placeholder="без @"
        />
      </div>
      <div className="field">
        <label className="field__label">Заметки</label>
        <textarea
          className="field__textarea"
          rows={3}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
      </div>
      <button className="btn btn--block" disabled={saving} onClick={submit}>
        {saving ? "Сохраняем..." : "Сохранить"}
      </button>
      {onDelete ? (
        <>
          <div style={{ height: 8 }} />
          <button
            className="btn btn--ghost btn--block"
            style={{ color: "var(--danger)" }}
            onClick={onDelete}
          >
            Удалить
          </button>
        </>
      ) : null}
    </Modal>
  );
}
