import { ReactNode } from "react";

export function PageHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <h1 className="page-header__title">{title}</h1>
        {subtitle ? (
          <div className="page-header__subtitle">{subtitle}</div>
        ) : null}
      </div>
      {action ? <div className="page-header__action">{action}</div> : null}
    </header>
  );
}
