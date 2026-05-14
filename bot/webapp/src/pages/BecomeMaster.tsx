import { PageHeader } from "../components/PageHeader";
import type { Me } from "../types";

/**
 * Landing screen shown to anyone who opens the Mini App without master
 * privileges. It surfaces the "become a master" conditions and a deep-link
 * to the configured admin so the user can request access in two taps.
 *
 * The conditions text and admin URL come from /api/me, which is the same
 * source of truth the Telegram bot uses — so updating BECOME_MASTER_CONDITIONS
 * in the environment changes both surfaces.
 */
export function BecomeMasterPage({ me }: { me: Me }) {
  const conditions = me.become_master_conditions?.trim();
  const adminUrl = me.admin_contact_url?.trim();

  return (
    <div>
      <PageHeader
        title="Стать мастером"
        subtitle="Clientika — это мини-CRM для самозанятых в Telegram"
      />

      <div className="card">
        <div style={{ fontWeight: 600, marginBottom: 8 }}>
          Привет, {me.display_name.split(" ")[0] || "друг"}!
        </div>
        <div style={{ color: "var(--hint)", marginBottom: 12 }}>
          У вас сейчас обычный аккаунт. Чтобы получить доступ к панели
          мастера — записям, клиентам, услугам и публичной ссылке для записи —
          нужно подать заявку администратору.
        </div>

        {conditions ? (
          <div
            style={{
              whiteSpace: "pre-wrap",
              background: "rgba(36,129,204,0.08)",
              borderRadius: 12,
              padding: 12,
              marginBottom: 12,
              fontSize: 14,
            }}
          >
            {conditions}
          </div>
        ) : null}

        {adminUrl ? (
          <a
            className="btn btn--block"
            href={adminUrl}
            target="_blank"
            rel="noreferrer noopener"
          >
            ✉️ Написать админу
          </a>
        ) : (
          <div className="error-banner">
            Контакт админа пока не настроен. Сообщите владельцу бота, что
            переменная <code>ADMIN_TG_IDS</code> не задана.
          </div>
        )}

        <div
          style={{
            color: "var(--hint)",
            fontSize: 12,
            marginTop: 12,
            textAlign: "center",
          }}
        >
          Ваш TG ID: <code>{me.tg_user_id}</code>
        </div>
      </div>
    </div>
  );
}
