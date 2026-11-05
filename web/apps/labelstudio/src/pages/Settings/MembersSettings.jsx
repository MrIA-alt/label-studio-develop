import { useCallback, useContext, useEffect, useState } from "react";
import { Button, Userpic } from "@humansignal/ui";
import { Spinner } from "../../components";
import { useAPI } from "../../providers/ApiProvider";
import { ProjectContext } from "../../providers/ProjectProvider";
import { cn } from "../../utils/bem";
import "./MembersSettings.scss";

const ROLE_LABELS = {
  admin: "Admin",
  qa: "QA (Supervisor)",
  labeller: "Labeller",
};

export const MembersSettings = () => {
  const api = useAPI();
  const { project } = useContext(ProjectContext);
  const [orgMembers, setOrgMembers] = useState(null);
  const [projectMembers, setProjectMembers] = useState(null);
  const [loading, setLoading] = useState(true);
  const [updating, setUpdating] = useState({});

  const fetchData = useCallback(async () => {
    if (!project?.id) return;
    setLoading(true);

    const [membersRes, projectMembersRes] = await Promise.all([
      api.callApi("memberships", {
        params: { pk: 1, page_size: -1 },
      }),
      api.callApi("projectMembers", {
        params: { pk: project.id },
      }),
    ]);

    if (membersRes?.results) {
      // Show labellers and QA — admins already have access to all projects
      const assignable = membersRes.results.filter((m) => m.role === "labeller" || m.role === "qa");
      setOrgMembers(assignable);
    }

    if (Array.isArray(projectMembersRes)) {
      setProjectMembers(projectMembersRes);
    }

    setLoading(false);
  }, [project?.id]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const isAssigned = useCallback(
    (userId) => {
      return projectMembers?.some((pm) => pm.user.id === userId && pm.enabled);
    },
    [projectMembers],
  );

  const toggleMember = useCallback(
    async (userId) => {
      if (updating[userId]) return;
      setUpdating((prev) => ({ ...prev, [userId]: true }));

      try {
        if (isAssigned(userId)) {
          await api.callApi("removeProjectMember", {
            params: { pk: project.id, userPk: userId },
          });
        } else {
          await api.callApi("addProjectMember", {
            params: { pk: project.id },
            body: { user_id: userId },
          });
        }
        await fetchData();
      } finally {
        setUpdating((prev) => ({ ...prev, [userId]: false }));
      }
    },
    [project?.id, isAssigned, updating, fetchData],
  );

  if (loading) {
    return (
      <div className={cn("members-settings").toClassName()}>
        <h1>Members</h1>
        <div className={cn("members-settings").elem("loading").toClassName()}>
          <Spinner size={36} />
        </div>
      </div>
    );
  }

  return (
    <div className={cn("members-settings").toClassName()}>
      <h1>Members</h1>
      <p className="settings-description">
        Assign members to this project. Only assigned labellers and QA supervisors will be able to see and work on this project.
        Admins have access to all projects automatically.
      </p>
      <div className={cn("settings-wrapper").toClassName()} style={{ marginTop: 24 }}>
        {orgMembers && orgMembers.length > 0 ? (
          <div className={cn("members-settings").elem("list").toClassName()}>
            <div className={cn("members-settings").elem("header").toClassName()}>
              <div className={cn("members-settings").elem("col").mix("avatar").toClassName()} />
              <div className={cn("members-settings").elem("col").mix("email").toClassName()}>Email</div>
              <div className={cn("members-settings").elem("col").mix("name").toClassName()}>Name</div>
              <div className={cn("members-settings").elem("col").mix("role").toClassName()}>Role</div>
              <div className={cn("members-settings").elem("col").mix("action").toClassName()}>Access</div>
            </div>
            <div className={cn("members-settings").elem("body").toClassName()}>
              {orgMembers.map((member) => {
                const { user } = member;
                const assigned = isAssigned(user.id);
                const isUpdating = updating[user.id];

                return (
                  <div
                    key={`member-${user.id}`}
                    className={cn("members-settings").elem("row").toClassName()}
                  >
                    <div className={cn("members-settings").elem("field").mix("avatar").toClassName()}>
                      <Userpic user={user} style={{ width: 28, height: 28 }} />
                    </div>
                    <div className={cn("members-settings").elem("field").mix("email").toClassName()}>
                      {user.email}
                    </div>
                    <div className={cn("members-settings").elem("field").mix("name").toClassName()}>
                      {user.first_name} {user.last_name}
                    </div>
                    <div className={cn("members-settings").elem("field").mix("role").toClassName()}>
                      <span className={`members-settings__role-badge members-settings__role-badge_${member.role}`}>
                        {ROLE_LABELS[member.role] || member.role}
                      </span>
                    </div>
                    <div className={cn("members-settings").elem("field").mix("action").toClassName()}>
                      <Button
                        size="small"
                        look={assigned ? "destructive" : "primary"}
                        waiting={isUpdating}
                        onClick={() => toggleMember(user.id)}
                        aria-label={assigned ? `Remove ${user.email}` : `Assign ${user.email}`}
                      >
                        {assigned ? "Remove" : "Assign"}
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ) : (
          <p>No labellers found in your organization. Invite labellers from the Organization page first.</p>
        )}
      </div>
    </div>
  );
};

MembersSettings.menuItem = "Members";
MembersSettings.path = "/members";
MembersSettings.exact = true;
