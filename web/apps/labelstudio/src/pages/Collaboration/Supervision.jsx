import { campaignClass } from "./classes";
import { useCallback, useEffect, useRef, useState } from "react";
import { AnnotationCanvas } from "./AnnotationCanvas";
import { downloadJSON, Pagination, useCampaignApi } from "./api";

export function Supervision({ project, archived = false }) {
  const call = useCampaignApi();
  const [filter, setFilter] = useState("pending");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState({ results: [], count: 0 });
  const [progress, setProgress] = useState(null);
  const [detail, setDetail] = useState(null);
  const [selected, setSelected] = useState([]);
  const [comment, setComment] = useState("");
  const [editing, setEditing] = useState(null);
  const [finalResult, setFinalResult] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const finalEditor = useRef(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [queue, stats] = await Promise.all([
        call("campaignReviewQueue", { pk: project.id, status: filter, offset, limit: 25 }),
        call("campaignProgress", { pk: project.id }),
      ]);
      setPage(queue);
      setProgress(stats);
      setSelected([]);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setLoading(false);
    }
  }, [call, project.id, filter, offset]);
  useEffect(() => {
    load();
  }, [load]);

  async function open(task) {
    setBusy(true);
    setError("");
    setEditing(null);
    try {
      setDetail(await call("campaignTaskReview", { pk: task }));
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  }

  async function decide(annotation, decision) {
    if (["REJECT", "REQUEST_CORRECTION"].includes(decision) && !comment.trim()) {
      setError("Enter a reason before rejecting or requesting a correction.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const displayed = detail.annotations.find((item) => item.id === annotation);
      await call(
        "campaignReview",
        {},
        {
          annotation,
          revision: displayed.revision,
          decision,
          comment: comment.trim(),
        },
      );
      setDetail(await call("campaignTaskReview", { pk: detail.task.id }));
      setComment("");
      await load();
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  }

  async function acceptSelected() {
    if (!window.confirm(`Accept all pending annotations in ${selected.length} selected task(s)?`)) return;
    setBusy(true);
    setError("");
    try {
      for (const task of selected) {
        const view = await call("campaignTaskReview", { pk: task });
        for (const annotation of view.annotations.filter((item) => item.review_status === "pending"))
          await call(
            "campaignReview",
            {},
            {
              annotation: annotation.id,
              revision: annotation.revision,
              decision: "ACCEPT",
              comment: "",
            },
          );
      }
      await load();
      setDetail(null);
    } catch (reason) {
      setError(`Some decisions may already have been saved. Refresh before retrying. ${reason.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function saveFinal() {
    if (!window.confirm("Save this as the final arbitrated annotation? The individual annotations will be preserved."))
      return;
    setBusy(true);
    setError("");
    try {
      await call("campaignFinal", { pk: detail.task.id }, { result: finalEditor.current?.() ?? finalResult });
      setEditing(null);
      setDetail(await call("campaignTaskReview", { pk: detail.task.id }));
      await load();
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  }

  async function exportResults() {
    setBusy(true);
    setError("");
    try {
      const results = [];
      let nextOffset = 0;
      let total;
      do {
        const response = await call("campaignResults", { pk: project.id, offset: nextOffset, limit: 500 });
        results.push(...response.results);
        total = response.count;
        nextOffset += 500;
      } while (nextOffset < total);
      downloadJSON(results, `project-${project.id}-reviewed-results.json`);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {progress && (
        <div className={campaignClass("campaign-metrics")} aria-label="Project progress">
          <div>
            <strong>{Object.values(progress.tasks_by_state ?? {}).reduce((a, b) => a + b, 0)}</strong>
            <span>Tasks</span>
          </div>
          <div>
            <strong>{progress.review_ready_tasks}</strong>
            <span>Ready for review</span>
          </div>
          <div>
            <strong>{progress.annotations?.accepted ?? 0}</strong>
            <span>Accepted annotations</span>
          </div>
          <div>
            <strong>{progress.annotations?.rejected ?? 0}</strong>
            <span>Rejected annotations</span>
          </div>
          <div>
            <strong>{progress.disagreement_tasks}</strong>
            <span>Disagreements</span>
          </div>
        </div>
      )}
      <div className={campaignClass("campaign-toolbar")}>
        <label>
          Review queue{" "}
          <select
            value={filter}
            onChange={(event) => {
              setFilter(event.target.value);
              setOffset(0);
              setDetail(null);
            }}
          >
            <option value="pending">Pending</option>
            <option value="disagreement">Disagreements</option>
            <option value="accepted">Accepted</option>
            <option value="rejected">Rejected</option>
            <option value="all">All eligible tasks</option>
          </select>
        </label>
        <button type="button" disabled={loading || busy} onClick={load}>
          Refresh
        </button>
        <button type="button" disabled={!selected.length || busy || archived} onClick={acceptSelected}>
          Accept selected tasks
        </button>
        <button type="button" disabled={busy} onClick={exportResults}>
          Export reviewed results
        </button>
      </div>
      {error && (
        <p role="alert" className={campaignClass("campaign-error")}>
          {error}
        </p>
      )}
      {loading ? (
        <p role="status">Loading review queue…</p>
      ) : (
        <>
          <div className={campaignClass("campaign-table-wrap")}>
            <table>
              <caption>Tasks eligible for review</caption>
              <thead>
                <tr>
                  <th>Select</th>
                  <th>Task</th>
                  <th>Annotations</th>
                  <th>Pending</th>
                  <th>Agreement</th>
                  <th>Final annotation</th>
                </tr>
              </thead>
              <tbody>
                {page.results.map((item) => (
                  <tr key={item.task}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Select task ${item.task}`}
                        checked={selected.includes(item.task)}
                        onChange={(event) =>
                          setSelected((previous) =>
                            event.target.checked ? [...previous, item.task] : previous.filter((id) => id !== item.task),
                          )
                        }
                      />
                    </td>
                    <td>
                      <button type="button" disabled={busy} onClick={() => open(item.task)}>
                        Compare #{item.task}
                      </button>
                    </td>
                    <td>{item.annotations}</td>
                    <td>{item.pending}</td>
                    <td>
                      {item.agreement === null ? "Not available" : `${Math.round(item.agreement * 100)}%`}
                      {item.disagreement && " · disagreement"}
                    </td>
                    <td>{item.final_annotation ? `#${item.final_annotation}` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!page.count && <p>No tasks match this queue.</p>}
          <Pagination offset={offset} count={page.count} onChange={setOffset} />
        </>
      )}
      {detail && (
        <section className={campaignClass("campaign-panel")}>
          <h2>Compare task #{detail.task.id}</h2>
          <button type="button" disabled={busy} onClick={() => open(detail.task.id)}>
            Reload comparison
          </button>
          {detail.anonymous && <p>Anonymous review is enabled. Annotators are shown with task-specific aliases.</p>}
          <label className={campaignClass("campaign-reason")}>
            Review comment
            <textarea
              value={comment}
              maxLength={5000}
              onChange={(event) => setComment(event.target.value)}
              placeholder="Required for rejection or correction"
            />
          </label>
          <div className={campaignClass("campaign-comparison")}>
            {detail.annotations.map((annotation) => (
              <article key={annotation.id}>
                <h3>
                  {detail.anonymous ? annotation.annotator : `Annotator #${annotation.annotator}`}{" "}
                  <span className={campaignClass("campaign-status")}>{annotation.review_status}</span>
                </h3>
                <p>
                  Annotation #{annotation.id} · Version {annotation.revision}
                </p>
                <AnnotationCanvas project={project} task={detail.task} annotation={annotation} />
                <div className={campaignClass("campaign-actions")}>
                  <button
                    type="button"
                    disabled={busy || archived || annotation.assignment_status === "CLOSED"}
                    onClick={() => decide(annotation.id, "ACCEPT")}
                  >
                    Accept
                  </button>
                  <button
                    type="button"
                    disabled={busy || archived || annotation.assignment_status === "CLOSED"}
                    onClick={() => decide(annotation.id, "REJECT")}
                  >
                    Reject
                  </button>
                  <button
                    type="button"
                    disabled={busy || archived || annotation.assignment_status === "CLOSED"}
                    onClick={() => decide(annotation.id, "REQUEST_CORRECTION")}
                  >
                    Request correction
                  </button>
                  <button type="button" disabled={busy || archived} onClick={() => decide(annotation.id, "REFERENCE")}>
                    Use as reference
                  </button>
                  <button
                    type="button"
                    disabled={busy || archived}
                    onClick={() => {
                      setEditing(annotation);
                      setFinalResult(annotation.result);
                    }}
                  >
                    Arbitrate from this annotation
                  </button>
                </div>
                <details>
                  <summary>Review and version history</summary>
                  {annotation.reviews.map((review, index) => (
                    <p key={index}>
                      <strong>{review.decision}</strong> · {review.comment} ·{" "}
                      {new Date(review.created_at).toLocaleString()}
                    </p>
                  ))}
                  {annotation.revisions.map((revision) => (
                    <details key={revision.revision}>
                      <summary>Version {revision.revision}</summary>
                      <AnnotationCanvas
                        project={project}
                        task={detail.task}
                        annotation={{ id: `${annotation.id}-${revision.revision}`, result: revision.result }}
                      />
                    </details>
                  ))}
                </details>
              </article>
            ))}
          </div>
          {editing && !archived && (
            <section className={campaignClass("campaign-final")}>
              <h3>Final arbitrated annotation</h3>
              <p>Edit the result using the native annotation tools, then save the final version.</p>
              <AnnotationCanvas
                project={project}
                task={detail.task}
                annotation={editing}
                editable
                onChange={setFinalResult}
                onReady={(serialize) => {
                  finalEditor.current = serialize;
                }}
              />
              <div className={campaignClass("campaign-actions")}>
                <button type="button" disabled={busy || !finalResult} onClick={saveFinal}>
                  Save final annotation
                </button>
                <button type="button" onClick={() => setEditing(null)}>
                  Cancel
                </button>
              </div>
            </section>
          )}
        </section>
      )}
      {progress?.workload?.length > 0 && (
        <section className={campaignClass("campaign-panel")}>
          <h2>Progress by annotator</h2>
          <div className={campaignClass("campaign-table-wrap")}>
            <table>
              <thead>
                <tr>
                  <th>Annotator</th>
                  <th>Assigned</th>
                  <th>In progress</th>
                  <th>Submitted</th>
                  <th>Correction</th>
                  <th>Accepted</th>
                </tr>
              </thead>
              <tbody>
                {progress.workload.map((row) => (
                  <tr key={row.user}>
                    <td>#{row.user}</td>
                    {["ASSIGNED", "IN_PROGRESS", "SUBMITTED", "REWORK_REQUIRED", "CLOSED"].map((status) => (
                      <td key={status}>{row.by_status[status] ?? 0}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}
