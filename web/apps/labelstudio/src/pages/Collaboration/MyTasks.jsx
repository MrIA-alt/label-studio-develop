import { campaignClass } from "./classes";
import { useCallback, useEffect, useState } from "react";
import { Link, useHistory } from "react-router-dom";
import { Pagination, STATUS_LABELS, Status, useCampaignApi } from "./api";

export function MyTasks({ project, policy }) {
  const call = useCampaignApi();
  const history = useHistory();
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState({ results: [], count: 0 });
  const [feedback, setFeedback] = useState({ results: [], count: 0 });
  const [feedbackOffset, setFeedbackOffset] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [assignments, reviews] = await Promise.all([
        call("campaignMyAssignments", { project: project.id, status, offset, limit: 25 }),
        call("campaignMyReviews", { project: project.id, offset: feedbackOffset, limit: 25 }),
      ]);
      setPage(assignments);
      setFeedback(reviews);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setLoading(false);
    }
  }, [call, project.id, status, offset, feedbackOffset]);
  useEffect(() => {
    load();
  }, [load]);

  async function open(assignment) {
    setBusy(assignment.id);
    setError("");
    try {
      if (!policy.is_archived && ["ASSIGNED", "REWORK_REQUIRED"].includes(assignment.status))
        await call("campaignAssignmentAction", { pk: assignment.id, action: "start" }, {});
      history.push(`/projects/${project.id}/data?task=${assignment.task}`);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(null);
    }
  }

  async function release(assignment, action) {
    if (
      !window.confirm(
        action === "skip"
          ? "Skip this task and make it available for reassignment?"
          : "Release this task? Your saved drafts will be kept.",
      )
    )
      return;
    setBusy(assignment.id);
    setError("");
    try {
      await call("campaignAssignmentAction", { pk: assignment.id, action }, {});
      await load();
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <div className={campaignClass("campaign-toolbar")}>
        <label>
          Status{" "}
          <select
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              setOffset(0);
            }}
          >
            <option value="">All my tasks</option>
            {Object.entries(STATUS_LABELS).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <button type="button" onClick={load} disabled={loading}>
          Refresh
        </button>
        {policy.assignment_mode === "open" && (
          <Link to={`/projects/${project.id}/data?labeling=1`}>Open annotation queue</Link>
        )}
      </div>
      {error && (
        <p role="alert" className={campaignClass("campaign-error")}>
          {error}
        </p>
      )}
      {loading ? (
        <p role="status">Loading your tasks…</p>
      ) : (
        <>
          <div className={campaignClass("campaign-table-wrap")}>
            <table>
              <caption>My assigned tasks</caption>
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Status</th>
                  <th>Deadline</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {page.results.map((assignment) => (
                  <tr key={assignment.id}>
                    <td>#{assignment.task}</td>
                    <td>
                      <Status value={assignment.status} />
                    </td>
                    <td>{assignment.expires_at ? new Date(assignment.expires_at).toLocaleString() : "—"}</td>
                    <td className={campaignClass("campaign-actions")}>
                      <button
                        type="button"
                        disabled={
                          busy === assignment.id || ["EXPIRED", "CANCELLED", "SKIPPED"].includes(assignment.status)
                        }
                        onClick={() => open(assignment)}
                      >
                        {["EXPIRED", "CANCELLED", "SKIPPED"].includes(assignment.status)
                          ? "Unavailable"
                          : policy.is_archived || ["SUBMITTED", "CLOSED"].includes(assignment.status)
                            ? "View"
                            : "Annotate"}
                      </button>
                      {!policy.is_archived && assignment.status === "ASSIGNED" && (
                        <button
                          type="button"
                          disabled={busy === assignment.id}
                          onClick={() => release(assignment, "skip")}
                        >
                          Skip
                        </button>
                      )}
                      {!policy.is_archived && ["ASSIGNED", "IN_PROGRESS"].includes(assignment.status) && (
                        <button
                          type="button"
                          disabled={busy === assignment.id}
                          onClick={() => release(assignment, "release")}
                        >
                          Release
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!page.count && <p>No tasks match this status. Assigned tasks will appear here.</p>}
          <Pagination offset={offset} count={page.count} onChange={setOffset} />
        </>
      )}
      <section className={campaignClass("campaign-panel")}>
        <h2>My review feedback</h2>
        {!feedback.count && <p>Your review feedback will appear here.</p>}
        {feedback.results.map((review, index) => (
          <article
            className={campaignClass("campaign-feedback")}
            key={`${review.annotation}-${review.revision}-${index}`}
          >
            <Link to={`/projects/${project.id}/data?task=${review.task}`}>Task #{review.task}</Link>
            <strong>{review.decision.replaceAll("_", " ")}</strong>
            <span>Version {review.revision}</span>
            <p>{review.comment || "No comment."}</p>
          </article>
        ))}
        <Pagination offset={feedbackOffset} count={feedback.count} onChange={setFeedbackOffset} />
      </section>
    </>
  );
}
