import { campaignClass } from "./classes";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Pagination, Status, useCampaignApi } from "./api";

const SCOPES = {
  "projects:write": "Create and publish projects",
  "projects:delete": "Delete projects",
  "policy:write": "Configure annotation policies",
  "assignments:read": "Read assignments",
  "assignments:write": "Distribute and reassign tasks",
  "reviews:read": "Read annotations for review",
  "reviews:write": "Review and arbitrate annotations",
  "progress:read": "Read progress",
  "results:read": "Read and export results",
  "imports:write": "Import tasks",
  "annotations:write": "Submit annotations",
  "annotations:delete": "Delete annotations",
  "predictions:delete": "Delete model predictions",
  "tasks:read": "Read source tasks",
  "tasks:write": "Modify source tasks",
  "tasks:delete": "Delete source tasks and uploads",
  "projects:read": "Read project configuration",
  "policy:read": "Read annotation policies",
  "webhooks:write": "Manage webhooks",
};

export function Administration({ project, policy, role, onPolicyChanged }) {
  const call = useCampaignApi();
  const isAdmin = role === "admin";
  const [form, setForm] = useState(policy);
  const [tasks, setTasks] = useState({ tasks: [], total: 0 });
  const [taskOffset, setTaskOffset] = useState(0);
  const [members, setMembers] = useState([]);
  const [taskIds, setTaskIds] = useState([]);
  const [userIds, setUserIds] = useState([]);
  const [assignments, setAssignments] = useState({ results: [], count: 0 });
  const [assignmentOffset, setAssignmentOffset] = useState(0);
  const [accounts, setAccounts] = useState({ results: [], count: 0 });
  const [accountOffset, setAccountOffset] = useState(0);
  const [audit, setAudit] = useState({ results: [], count: 0 });
  const [auditOffset, setAuditOffset] = useState(0);
  const [accountName, setAccountName] = useState("");
  const [scopes, setScopes] = useState(["progress:read"]);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [published, setPublished] = useState(project.is_published);
  const [inviteEmails, setInviteEmails] = useState("");
  const [invitations, setInvitations] = useState({ results: [], count: 0 });
  const [inviteOffset, setInviteOffset] = useState(0);
  useEffect(() => {
    setForm(policy);
  }, [policy]);

  const load = useCallback(async () => {
    try {
      const me = await call("me");
      const [sourceTasks, orgMembers, assigned, projectMembers] = await Promise.all([
        call("campaignTaskList", { project: project.id, page: Math.floor(taskOffset / 25) + 1, page_size: 25 }),
        call("memberships", { pk: me.active_organization, page_size: 500 }),
        call("campaignAssignments", { pk: project.id, offset: assignmentOffset, limit: 25 }),
        call("projectMembers", { pk: project.id }),
      ]);
      setTasks(sourceTasks);
      setAssignments(assigned);
      const explicitMembers = projectMembers.results ?? projectMembers;
      const eligible = (orgMembers.results ?? orgMembers).filter(
        (member) => member.role === "labeller" && member.user?.is_active !== false,
      );
      setMembers(
        explicitMembers.length
          ? eligible.filter((member) =>
              explicitMembers.some(
                (assignedMember) => assignedMember.enabled && assignedMember.user.id === member.user.id,
              ),
            )
          : eligible,
      );
      if (isAdmin) {
        const [agents, events, pendingInvites] = await Promise.all([
          call("campaignServiceAccounts", { offset: accountOffset, limit: 25 }),
          call("campaignAudit", { project: project.id, offset: auditOffset, limit: 25 }),
          call("campaignInvitations", { pk: project.id, offset: inviteOffset, limit: 25 }),
        ]);
        setAccounts(agents);
        setAudit(events);
        setInvitations(pendingInvites);
      }
    } catch (reason) {
      setError(reason.message);
    }
  }, [call, project.id, isAdmin, taskOffset, assignmentOffset, accountOffset, auditOffset, inviteOffset]);
  useEffect(() => {
    load();
  }, [load]);

  async function run(operation, success) {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await operation();
      setMessage(success);
      await load();
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  }
  const toggle = (values, value, checked) => (checked ? [...values, value] : values.filter((item) => item !== value));

  async function savePolicy(event) {
    event.preventDefault();
    await run(async () => {
      const updated = await call("campaignSavePolicy", { pk: project.id }, form);
      await onPolicyChanged(updated);
    }, "Annotation policy saved.");
  }

  return (
    <>
      {error && (
        <p role="alert" className={campaignClass("campaign-error")}>
          {error}
        </p>
      )}
      {message && (
        <p role="status" className={campaignClass("campaign-success")}>
          {message}
        </p>
      )}
      {isAdmin && (
        <section className={campaignClass("campaign-panel")}>
          <h2>Invite annotators by email</h2>
          <p>Select the tasks below, then enter the annotators’ email addresses. Tasks are reserved until they activate their invitation.</p>
          {!invitations.mail_configured && <p role="status">Mail delivery is not configured. Invitations will remain queued until the administrator configures SMTP or Microsoft 365.</p>}
          {policy.assignment_mode !== "manual" && <p>Choose Manual assignment mode to pre-assign tasks by email.</p>}
          <form onSubmit={(event) => {
            event.preventDefault();
            run(async () => {
              await call("campaignInvite", { pk: project.id }, {
                emails: inviteEmails.split(/[\s,;]+/).filter(Boolean), task_ids: taskIds,
              });
              setInviteEmails("");
            }, "Invitations queued. Email delivery runs automatically; check the delivery status below.");
          }}>
            <label>Email addresses<textarea required value={inviteEmails} onChange={(event) => setInviteEmails(event.target.value)} placeholder="alice@example.org; bob@example.org" /></label>
            <p>{taskIds.length} selected task(s) per annotator</p>
            <button type="submit" disabled={busy || policy.is_archived || policy.assignment_mode !== "manual" || !taskIds.length}>Invite and pre-assign selected tasks</button>
          </form>
          <button type="button" onClick={load} disabled={busy}>Refresh invitation status</button>
          <table>
            <thead><tr><th>Email</th><th>Invitation</th><th>Email delivery</th><th>Tasks</th><th>Actions</th></tr></thead>
            <tbody>{invitations.results.map((invite) => (
              <tr key={invite.id}>
                <td>{invite.email}</td><td>{invite.expired ? "expired" : invite.status}</td>
                <td>{invite.delivery_status}{invite.error_code ? ` (${invite.error_code})` : ""}</td>
                <td>{invite.task_ids.length}</td>
                <td>{invite.status === "pending" && ["resend", "cancel"].map((action) => (
                  <button type="button" key={action} disabled={busy || policy.is_archived}
                    onClick={() => {
                      if (action === "cancel" && !window.confirm("Cancel this invitation and release its task reservations?")) return;
                      run(() => call("campaignInvitationAction", { pk: project.id, invitationId: invite.id, action }, {}), "Invitation updated.");
                    }}>{action === "resend" ? "Resend email" : "Cancel invitation"}</button>
                ))}</td>
              </tr>
            ))}</tbody>
          </table>
          <Pagination offset={inviteOffset} count={invitations.count} onChange={setInviteOffset} />
        </section>
      )}
      {isAdmin && (
        <section className={campaignClass("campaign-panel")}>
          <h2>Annotation policy</h2>
          <form onSubmit={savePolicy} className={campaignClass("campaign-form")}>
            <label>
              Independent annotations required
              <input
                type="number"
                min="1"
                max="10"
                required
                value={form.required_annotations}
                onChange={(event) => setForm({ ...form, required_annotations: Number(event.target.value) })}
              />
            </label>
            <label>
              Assignment mode
              <select
                value={form.assignment_mode}
                onChange={(event) => setForm({ ...form, assignment_mode: event.target.value })}
              >
                <option value="open">Open queue</option>
                <option value="manual">Manual</option>
                <option value="automatic">Automatic</option>
                <option value="hybrid">Hybrid</option>
              </select>
            </label>
            <label>
              Review workflow
              <select
                value={form.review_mode}
                onChange={(event) => setForm({ ...form, review_mode: event.target.value })}
              >
                <option value="none">No review lock</option>
                <option value="manual">Manual review and correction</option>
              </select>
            </label>
            <label>
              Maximum active tasks per annotator
              <input
                type="number"
                min="1"
                value={form.max_active_tasks_per_user ?? ""}
                placeholder="Unlimited"
                onChange={(event) =>
                  setForm({
                    ...form,
                    max_active_tasks_per_user: event.target.value ? Number(event.target.value) : null,
                  })
                }
              />
            </label>
            <label>
              Assignment expires after (hours)
              <input
                type="number"
                min="1"
                value={form.assignment_ttl_hours ?? ""}
                placeholder="No expiry"
                onChange={(event) =>
                  setForm({ ...form, assignment_ttl_hours: event.target.value ? Number(event.target.value) : null })
                }
              />
            </label>
            <label>
              Agreement threshold
              <input
                type="number"
                min="0"
                max="1"
                step="0.01"
                required
                value={form.agreement_threshold}
                onChange={(event) => setForm({ ...form, agreement_threshold: Number(event.target.value) })}
              />
            </label>
            <label className={campaignClass("campaign-check")}>
              <input
                type="checkbox"
                checked={form.blind_mode}
                onChange={(event) => setForm({ ...form, blind_mode: event.target.checked })}
              />{" "}
              Hide other annotators’ work
            </label>
            <label className={campaignClass("campaign-check")}>
              <input
                type="checkbox"
                checked={form.anonymous_review}
                onChange={(event) => setForm({ ...form, anonymous_review: event.target.checked })}
              />{" "}
              Anonymous supervisor review
            </label>
            <label className={campaignClass("campaign-check")}>
              <input
                type="checkbox"
                checked={form.allow_predictions}
                onChange={(event) => setForm({ ...form, allow_predictions: event.target.checked })}
              />{" "}
              Allow model predictions
            </label>
            <div className={campaignClass("campaign-actions")}>
              <button type="submit" disabled={busy || policy.is_archived}>
                Save policy
              </button>
              <button
                type="button"
                disabled={busy || published || policy.is_archived}
                onClick={() =>
                  run(async () => {
                    await call("campaignPublish", { pk: project.id }, {});
                    setPublished(true);
                  }, "Project published.")
                }
              >
                {published ? "Published" : "Publish project"}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  const restoring = policy.is_archived;
                  if (
                    !window.confirm(
                      restoring
                        ? "Restore this campaign and resume annotation?"
                        : "Archive this campaign? Annotation and distribution will stop. Saved results will remain available.",
                    )
                  )
                    return;
                  run(
                    async () => {
                      const updated = await call(
                        restoring ? "campaignRestore" : "campaignArchive",
                        { pk: project.id },
                        {},
                      );
                      await onPolicyChanged(updated);
                    },
                    restoring ? "Campaign restored." : "Campaign archived.",
                  );
                }}
              >
                {policy.is_archived ? "Restore campaign" : "Archive campaign"}
              </button>
            </div>
          </form>
          <nav className={campaignClass("campaign-toolbar")} aria-label="Project administration">
            <Link to={`/projects/${project.id}/settings/members`}>Project members</Link>
            <Link to={`/projects/${project.id}/settings/labeling`}>Annotation interface</Link>
            <Link to={`/projects/${project.id}/data/import`}>Import data</Link>
            <Link to={`/projects/${project.id}/data/export`}>Standard exports</Link>
          </nav>
        </section>
      )}

      <section className={campaignClass("campaign-panel")}>
        <h2>Task distribution</h2>
        <div className={campaignClass("campaign-toolbar")}>
          <button
            type="button"
            disabled={busy || policy.is_archived || !["automatic", "hybrid"].includes(policy.assignment_mode)}
            onClick={() =>
              run(async () => {
                const response = await call("campaignDistribute", { pk: project.id }, {});
                setTaskIds([]);
                return response;
              }, "Automatic distribution completed.")
            }
          >
            Distribute automatically
          </button>
          <button
            type="button"
            disabled={busy || policy.is_archived || !taskIds.length || !userIds.length}
            onClick={() =>
              run(async () => {
                await call("campaignBulkAssign", { pk: project.id }, { task_ids: taskIds, user_ids: userIds });
                setTaskIds([]);
              }, "Selected tasks assigned.")
            }
          >
            Assign selected tasks
          </button>
        </div>
        <div className={campaignClass("campaign-distribution")}>
          <div>
            <h3>Choose tasks</h3>
            <div className={campaignClass("campaign-table-wrap")}>
              <table>
                <thead>
                  <tr>
                    <th>Select</th>
                    <th>Task</th>
                    <th>Source</th>
                  </tr>
                </thead>
                <tbody>
                  {(tasks.tasks ?? tasks.results ?? []).map((task) => (
                    <tr key={task.id}>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`Assign task ${task.id}`}
                          checked={taskIds.includes(task.id)}
                          onChange={(event) => setTaskIds(toggle(taskIds, task.id, event.target.checked))}
                        />
                      </td>
                      <td>#{task.id}</td>
                      <td>
                        {Object.entries(task.data ?? {})
                          .map(([key, value]) => `${key}: ${String(value).slice(0, 90)}`)
                          .join(" · ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination offset={taskOffset} count={tasks.total ?? tasks.count ?? 0} onChange={setTaskOffset} />
          </div>
          <fieldset>
            <legend>Eligible annotators</legend>
            {members.map((member) => (
              <label className={campaignClass("campaign-check")} key={member.user.id}>
                <input
                  type="checkbox"
                  checked={userIds.includes(member.user.id)}
                  onChange={(event) => setUserIds(toggle(userIds, member.user.id, event.target.checked))}
                />
                {member.user.email || `User #${member.user.id}`}
              </label>
            ))}
            {!members.length && <p>Add annotators to this organization and project before assigning tasks.</p>}
          </fieldset>
        </div>
        <h3>Current assignments</h3>
        <div className={campaignClass("campaign-table-wrap")}>
          <table>
            <thead>
              <tr>
                <th>Task</th>
                <th>Annotator</th>
                <th>Status</th>
                <th>Reassign to</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {assignments.results.map((assignment) => (
                <tr key={assignment.id}>
                  <td>#{assignment.task}</td>
                  <td>
                    {members.find((member) => member.user.id === assignment.user)?.user.email ?? `#${assignment.user}`}
                  </td>
                  <td>
                    <Status value={assignment.status} />
                  </td>
                  <td>
                    {["ASSIGNED", "IN_PROGRESS"].includes(assignment.status) && (
                      <select
                        aria-label={`Reassign task ${assignment.task}`}
                        value=""
                        disabled={busy || policy.is_archived}
                        onChange={(event) => {
                          const id = Number(event.target.value);
                          if (id && window.confirm("Reassign this task? The existing assignment will be cancelled."))
                            run(
                              () =>
                                call("campaignStaffAction", { pk: assignment.id, action: "reassign" }, { user_id: id }),
                              "Task reassigned.",
                            );
                        }}
                      >
                        <option value="">Choose annotator</option>
                        {members
                          .filter((member) => member.user.id !== assignment.user)
                          .map((member) => (
                            <option value={member.user.id} key={member.user.id}>
                              {member.user.email}
                            </option>
                          ))}
                      </select>
                    )}
                  </td>
                  <td>
                    {["ASSIGNED", "IN_PROGRESS"].includes(assignment.status) && (
                      <button
                        type="button"
                        disabled={busy || policy.is_archived}
                        onClick={() => {
                          if (window.confirm("Cancel this assignment?"))
                            run(
                              () => call("campaignStaffAction", { pk: assignment.id, action: "cancel" }, {}),
                              "Assignment cancelled.",
                            );
                        }}
                      >
                        Cancel
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <Pagination offset={assignmentOffset} count={assignments.count} onChange={setAssignmentOffset} />
      </section>

      {isAdmin && (
        <>
          <section className={campaignClass("campaign-panel")}>
            <h2>AI agents</h2>
            <p>New accounts created here are restricted to this project. Select only the actions the agent needs.</p>
            <form
              className={campaignClass("campaign-form")}
              onSubmit={(event) => {
                event.preventDefault();
                run(async () => {
                  const response = await call(
                    "campaignCreateServiceAccount",
                    {},
                    { name: accountName, scopes, project_ids: [project.id] },
                  );
                  setToken(response.token);
                  setAccountName("");
                }, "Service account created. Copy its token now.");
              }}
            >
              <label>
                Account name
                <input
                  required
                  maxLength={100}
                  value={accountName}
                  onChange={(event) => setAccountName(event.target.value)}
                />
              </label>
              <fieldset>
                <legend>Allowed actions</legend>
                {Object.entries(SCOPES).map(([scope, label]) => (
                  <label className={campaignClass("campaign-check")} key={scope}>
                    <input
                      type="checkbox"
                      checked={scopes.includes(scope)}
                      onChange={(event) => setScopes(toggle(scopes, scope, event.target.checked))}
                    />
                    {label}
                  </label>
                ))}
              </fieldset>
              <button type="submit" disabled={busy || !scopes.length}>
                Create service account
              </button>
            </form>
            {token && (
              <div className={campaignClass("campaign-token")}>
                <p>This token is shown once. Store it securely.</p>
                <input
                  aria-label="New service account token"
                  readOnly
                  value={token}
                  onFocus={(event) => event.target.select()}
                />
                <button type="button" onClick={() => setToken("")}>
                  Dismiss token
                </button>
              </div>
            )}
            <div className={campaignClass("campaign-table-wrap")}>
              <table>
                <thead>
                  <tr>
                    <th>Account</th>
                    <th>Scopes</th>
                    <th>Status</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {accounts.results.map((account) => (
                    <tr key={account.id}>
                      <td>{account.name}</td>
                      <td>{account.scopes.map((scope) => SCOPES[scope] ?? scope).join(", ")}</td>
                      <td>{account.active ? "Active" : "Revoked"}</td>
                      <td>
                        {account.active && (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => {
                              if (window.confirm(`Revoke ${account.name}? Its token will stop working immediately.`))
                                run(
                                  () => call("campaignRevokeServiceAccount", { pk: account.id }),
                                  "Service account revoked.",
                                );
                            }}
                          >
                            Revoke
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination offset={accountOffset} count={accounts.count} onChange={setAccountOffset} />
          </section>
          <section className={campaignClass("campaign-panel")}>
            <h2>Audit log</h2>
            <div className={campaignClass("campaign-table-wrap")}>
              <table>
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Actor</th>
                    <th>Action</th>
                    <th>Resource</th>
                  </tr>
                </thead>
                <tbody>
                  {audit.results.map((event) => (
                    <tr key={event.id}>
                      <td>{new Date(event.timestamp).toLocaleString()}</td>
                      <td>
                        {event.actor_type} #{event.actor ?? "—"}
                      </td>
                      <td>{event.action}</td>
                      <td>
                        {event.resource_type} #{event.resource_id}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination offset={auditOffset} count={audit.count} onChange={setAuditOffset} />
          </section>
        </>
      )}
    </>
  );
}
