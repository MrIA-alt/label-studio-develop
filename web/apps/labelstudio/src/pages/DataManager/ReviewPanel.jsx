import { Button } from "@humansignal/ui";
import { useCallback, useEffect, useState } from "react";
import { useAPI } from "../../providers/ApiProvider";

const REVIEW_LABELS = {
  pending: "Pending Review",
  accepted: "Accepted",
  rejected: "Rejected",
};

const getTaskIdFromUrl = () => {
  const params = new URLSearchParams(window.location.search);
  return params.get("task");
};

const panelStyle = {
  position: "fixed",
  bottom: "16px",
  right: "16px",
  width: "360px",
  maxHeight: "420px",
  overflowY: "auto",
  background: "#fff",
  border: "1px solid #ddd",
  borderRadius: "8px",
  boxShadow: "0 4px 16px rgba(0,0,0,0.15)",
  zIndex: 99999,
  fontFamily: "inherit",
};

const headerStyle = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  fontWeight: 600,
  fontSize: "14px",
  padding: "12px 16px",
  borderBottom: "1px solid #eee",
  background: "#f8f8f8",
  borderRadius: "8px 8px 0 0",
};

const bodyStyle = {
  padding: "12px 16px 16px",
};

const badgeStyle = (status) => ({
  display: "inline-block",
  padding: "3px 10px",
  borderRadius: "4px",
  fontSize: "12px",
  fontWeight: 600,
  background: status === "accepted" ? "#e6f4ea" : status === "rejected" ? "#fce8e6" : "#f0f0f0",
  color: status === "accepted" ? "#1e7e34" : status === "rejected" ? "#c62828" : "#666",
});

const dotStyle = (status) => ({
  display: "inline-block",
  width: "8px",
  height: "8px",
  borderRadius: "50%",
  background: status === "accepted" ? "#1e7e34" : status === "rejected" ? "#c62828" : "#999",
});

export const ReviewPanel = ({ projectId }) => {
  const api = useAPI();
  const [taskId, setTaskId] = useState(getTaskIdFromUrl());
  const [annotations, setAnnotations] = useState([]);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [reviewResult, setReviewResult] = useState(null);

  useEffect(() => {
    const check = () => {
      const newTaskId = getTaskIdFromUrl();
      if (newTaskId !== taskId) {
        setTaskId(newTaskId);
        setReviewResult(null);
        setComment("");
      }
    };
    const interval = setInterval(check, 500);
    return () => clearInterval(interval);
  }, [taskId]);

  useEffect(() => {
    if (!taskId) {
      setAnnotations([]);
      return;
    }
    const fetchAnnotations = async () => {
      const response = await api.callApi("annotations", {
        params: { pk: taskId },
      });
      if (Array.isArray(response)) {
        setAnnotations(response);
        setSelectedIdx(0);
        setReviewResult(null);
        setComment("");
      }
    };
    fetchAnnotations();
  }, [taskId]);

  const annotation = annotations[selectedIdx] || null;

  const submitReview = useCallback(
    async (reviewStatus) => {
      if (!annotation?.id) return;
      if (reviewStatus === "rejected" && !comment.trim()) {
        alert("Please provide a comment when rejecting an annotation.");
        return;
      }
      setSubmitting(true);
      try {
        const response = await api.callApi("reviewAnnotation", {
          params: { pk: annotation.id },
          body: { status: reviewStatus, comment },
        });
        if (response?.review_status) {
          setReviewResult(response);
          setAnnotations((prev) =>
            prev.map((a) =>
              a.id === annotation.id
                ? { ...a, review_status: response.review_status, review_comment: response.review_comment }
                : a,
            ),
          );
        }
      } finally {
        setSubmitting(false);
      }
    },
    [annotation?.id, comment],
  );

  if (!taskId || annotations.length === 0) {
    return null;
  }

  return (
    <div style={panelStyle}>
      <div style={headerStyle}>
        <span>Review — Task #{taskId}</span>
        {annotations.length > 1 && (
          <div style={{ display: "flex", gap: "4px" }}>
            {annotations.map((a, idx) => (
              <button
                key={a.id}
                onClick={() => { setSelectedIdx(idx); setReviewResult(null); setComment(""); }}
                style={{
                  display: "flex", alignItems: "center", gap: "4px",
                  padding: "2px 8px", border: "1px solid #ddd", borderRadius: "4px",
                  background: idx === selectedIdx ? "#eee" : "transparent",
                  fontWeight: idx === selectedIdx ? 600 : 400,
                  cursor: "pointer", fontSize: "12px",
                }}
              >
                #{a.id}
                <span style={dotStyle(a.review_status || "pending")} />
              </button>
            ))}
          </div>
        )}
      </div>

      <div style={bodyStyle}>
        <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "10px" }}>
          <span style={badgeStyle(annotation.review_status || "pending")}>
            {REVIEW_LABELS[annotation.review_status || "pending"]}
          </span>
          {annotation.completed_by && (
            <span style={{ fontSize: "12px", color: "#999" }}>
              by User #{annotation.completed_by}
            </span>
          )}
        </div>

        {annotation.review_comment && !reviewResult && (
          <div style={{
            fontSize: "13px", color: "#555", marginBottom: "10px",
            padding: "8px", background: "#f5f5f5", borderRadius: "4px", lineHeight: "1.4",
          }}>
            <strong>Previous comment:</strong> {annotation.review_comment}
          </div>
        )}

        {reviewResult && (
          <div style={{
            fontSize: "13px", fontWeight: 500, padding: "8px 10px", borderRadius: "4px", marginBottom: "10px",
            background: reviewResult.review_status === "accepted" ? "#e6f4ea" : "#fce8e6",
            color: reviewResult.review_status === "accepted" ? "#1e7e34" : "#c62828",
          }}>
            Review saved: {REVIEW_LABELS[reviewResult.review_status]}
          </div>
        )}

        <div style={{ marginBottom: "12px" }}>
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="Add a comment (required for rejection)..."
            rows={2}
            style={{
              width: "100%", boxSizing: "border-box", border: "1px solid #ddd",
              borderRadius: "4px", padding: "8px", fontSize: "13px",
              fontFamily: "inherit", resize: "vertical",
            }}
          />
        </div>

        <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
          <button
            disabled={submitting}
            onClick={() => submitReview("rejected")}
            style={{
              padding: "6px 20px", borderRadius: "4px", border: "none", cursor: "pointer",
              fontSize: "13px", fontWeight: 600, color: "#fff", background: "#c62828",
              opacity: submitting ? 0.6 : 1,
            }}
          >
            Reject
          </button>
          <Button look="primary" size="small" waiting={submitting} onClick={() => submitReview("accepted")}>
            Accept
          </Button>
        </div>
      </div>
    </div>
  );
};
