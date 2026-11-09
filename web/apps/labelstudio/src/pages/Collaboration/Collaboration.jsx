import { campaignClass } from "./classes";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "@humansignal/core/providers/AuthProvider";
import { useProject } from "../../providers/ProjectProvider";
import { useCampaignApi } from "./api";
import { MyTasks } from "./MyTasks";
import { Supervision } from "./Supervision";
import { Administration } from "./Administration";
import "./Collaboration.scss";

export function CollaborationPage() {
  const { project, fetchProject } = useProject();
  const { user } = useAuth();
  const call = useCampaignApi();
  const [role, setRole] = useState(null);
  const [policy, setPolicy] = useState(null);
  const [tab, setTab] = useState(null);
  const [error, setError] = useState("");
  const [viewer, setViewer] = useState(null);
  const [checking, setChecking] = useState(true);
  const checkedScope = useRef(null);
  const retryValidation = useRef(null);

  useEffect(() => {
    if (!project.id) return;
    let cancelled = false;
    let request = 0;
    // A redundant focus during a control click must not hide that control.
    let wasAway = document.hidden;
    async function validateSession() {
      const current = ++request;
      const stale = () => cancelled || current !== request;
      setChecking(true);
      setError("");
      try {
        const profile = await call("me");
        if (stale()) return;
        setViewer(profile);
        const identity = `${project.id}:${profile.id}:${profile.active_organization}`;
        if (checkedScope.current && !checkedScope.current.startsWith(`${identity}:`)) {
          checkedScope.current = null;
          setRole(null);
          setPolicy(null);
          setTab(null);
        }
        const [authorization, settings] = await Promise.all([
          call("currentUserRole"),
          call("campaignPolicy", { pk: project.id }),
        ]);
        if (stale()) return;
        if (!["admin", "qa", "labeller"].includes(authorization.role))
          throw new Error("Your project role could not be verified.");
        const scope = `${identity}:${authorization.role}`;
        if (checkedScope.current !== scope) setTab(authorization.role === "labeller" ? "tasks" : "review");
        checkedScope.current = scope;
        setRole(authorization.role);
        setPolicy(settings);
      } catch (reason) {
        if (!stale()) {
          if ([401, 403, 404].includes(Number(reason.status))) {
            checkedScope.current = null;
            setRole(null);
            setPolicy(null);
          }
          setError(reason.message);
        }
      } finally {
        if (!stale()) setChecking(false);
      }
    }
    retryValidation.current = validateSession;
    const onBlur = (event) => {
      if (event.target === window) wasAway = true;
    };
    const onFocus = (event) => {
      if (event.target !== window || !wasAway) return;
      wasAway = false;
      validateSession();
    };
    const onVisible = () => {
      if (document.hidden) {
        wasAway = true;
      } else if (wasAway) {
        wasAway = false;
        validateSession();
      }
    };
    validateSession();
    window.addEventListener("blur", onBlur);
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      if (retryValidation.current === validateSession) retryValidation.current = null;
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [call, project.id, user?.id, user?.active_organization]);

  return (
    <main className={campaignClass("campaign")} aria-label="Collaborative annotation campaign">
      <header className={campaignClass("campaign-header")}>
        <div>
          <p className={campaignClass("campaign-eyebrow")}>Annotation campaign</p>
          <h1>{project.title ?? "Loading project…"}</h1>
          {viewer && <p>Signed in as {viewer.email}</p>}
        </div>
        <Link to={`/projects/${project.id}/data`}>Open Data Manager</Link>
      </header>
      {error && (
        <>
          <p role="alert" className={campaignClass("campaign-error")}>
            {error}
          </p>
          <button type="button" onClick={() => retryValidation.current?.()}>
            Retry access check
          </button>
        </>
      )}
      {(checking || !policy) && !error && <p role="status">Checking your campaign access…</p>}
      {policy && role && (
        <div
          key={`${viewer?.id}:${viewer?.active_organization}:${role}`}
          style={checking || error ? { visibility: "hidden" } : undefined}
          aria-hidden={checking || !!error || undefined}
        >
          {policy.is_archived && (
            <p role="status" className={campaignClass("campaign-panel")}>
              This campaign is archived. Saved results remain available for consultation and export.
            </p>
          )}
          <nav className={campaignClass("campaign-tabs")} aria-label="Campaign workspace">
            <button type="button" aria-current={tab === "tasks" ? "page" : undefined} onClick={() => setTab("tasks")}>
              My tasks
            </button>
            {role !== "labeller" && (
              <>
                <button
                  type="button"
                  aria-current={tab === "review" ? "page" : undefined}
                  onClick={() => setTab("review")}
                >
                  Supervision
                </button>
                <button
                  type="button"
                  aria-current={tab === "admin" ? "page" : undefined}
                  onClick={() => setTab("admin")}
                >
                  {role === "admin" ? "Administration" : "Task distribution"}
                </button>
              </>
            )}
          </nav>
          {tab === "tasks" && <MyTasks project={project} policy={policy} />}
          {tab === "review" && role !== "labeller" && <Supervision project={project} archived={policy.is_archived} />}
          {tab === "admin" && role !== "labeller" && (
            <Administration
              project={project}
              policy={policy}
              role={role}
              onPolicyChanged={async (updated) => {
                setPolicy(updated);
                await fetchProject(project.id, true);
              }}
            />
          )}
        </div>
      )}
    </main>
  );
}

CollaborationPage.path = "/collaboration";
CollaborationPage.title = "Campaign";
CollaborationPage.exact = true;
