import { format } from "date-fns";
import { useCallback, useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { IconCross } from "@humansignal/icons";
import { Userpic, Button, Select } from "@humansignal/ui";
import { cn } from "../../../utils/bem";
import "./SelectedUser.scss";

const ROLE_OPTIONS = [
  { value: "admin", label: "Admin" },
  { value: "qa", label: "QA (Supervisor)" },
  { value: "labeller", label: "Labeller" },
];

const ROLE_LABELS = {
  admin: "Admin",
  qa: "QA (Supervisor)",
  labeller: "Labeller",
};

const UserProjectsLinks = ({ projects }) => {
  return (
    <div className={cn("user-info").elem("links-list").toClassName()}>
      {projects.map((project) => (
        <NavLink
          className={cn("user-info").elem("project-link").toClassName()}
          key={`project-${project.id}`}
          to={`/projects/${project.id}`}
          data-external
        >
          {project.title}
        </NavLink>
      ))}
    </div>
  );
};

export const SelectedUser = ({ user, onClose, isAdmin, onRoleChange }) => {
  const [pendingRole, setPendingRole] = useState(user.role);
  const [saving, setSaving] = useState(false);

  const hasChanged = pendingRole !== user.role;

  useEffect(() => {
    setPendingRole(user.role);
  }, [user.id, user.role]);

  const handleSave = useCallback(async () => {
    if (!hasChanged) return;
    setSaving(true);
    try {
      await onRoleChange?.({ user }, pendingRole);
    } finally {
      setSaving(false);
    }
  }, [hasChanged, pendingRole, user, onRoleChange]);

  const fullName = [user.first_name, user.last_name]
    .filter((n) => !!n)
    .join(" ")
    .trim();

  return (
    <div className={cn("user-info").toClassName()}>
      <Button
        look="string"
        onClick={onClose}
        className="absolute top-[20px] right-[24px]"
        aria-label="Close user details"
      >
        <IconCross />
      </Button>

      <div className={cn("user-info").elem("header").toClassName()}>
        <Userpic user={user} style={{ width: 64, height: 64, fontSize: 28 }} />
        <div className={cn("user-info").elem("info-wrapper").toClassName()}>
          {fullName && <div className={cn("user-info").elem("full-name").toClassName()}>{fullName}</div>}
          <p className={cn("user-info").elem("email").toClassName()}>{user.email}</p>
        </div>
      </div>

      {user.role && (
        <div className={cn("user-info").elem("section").toClassName()}>
          <div className={cn("user-info").elem("section-title").toClassName()}>Role</div>
          {isAdmin ? (
            <div className="user-info__role-editor">
              <Select
                value={pendingRole}
                options={ROLE_OPTIONS}
                onChange={(val) => setPendingRole(val)}
              />
              {hasChanged && (
                <Button
                  look="primary"
                  size="compact"
                  onClick={handleSave}
                  disabled={saving}
                  className="user-info__role-save"
                  style={{ marginTop: 8 }}
                >
                  {saving ? "Saving…" : "Save"}
                </Button>
              )}
            </div>
          ) : (
            <span className={`user-info__role-badge user-info__role-badge_${user.role}`}>
              {ROLE_LABELS[user.role] || user.role}
            </span>
          )}
        </div>
      )}

      {user.phone && (
        <div className={cn("user-info").elem("section").toClassName()}>
          <a href={`tel:${user.phone}`}>{user.phone}</a>
        </div>
      )}

      {!!user.created_projects?.length && (
        <div className={cn("user-info").elem("section").toClassName()}>
          <div className={cn("user-info").elem("section-title").toClassName()}>Created Projects</div>

          <UserProjectsLinks projects={user.created_projects} />
        </div>
      )}

      {!!user.contributed_to_projects?.length && (
        <div className={cn("user-info").elem("section").toClassName()}>
          <div className={cn("user-info").elem("section-title").toClassName()}>Contributed to</div>

          <UserProjectsLinks projects={user.contributed_to_projects} />
        </div>
      )}

      <p className={cn("user-info").elem("last-active").toClassName()}>
        Last activity on: {format(new Date(user.last_activity), "dd MMM yyyy, KK:mm a")}
      </p>
    </div>
  );
};
